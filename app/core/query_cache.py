"""Read-through Redis cache for expensive, read-heavy service calls.

Decorate a service function with `@cached(...)` and its return value is
stored in Redis, keyed by its arguments (minus the DB session). What makes
it safe to cache aggressively:

* **Tag-versioned keys, auto-invalidated on commit.** Each entry declares the
  tags it depends on (`app.core.cache_tags`). Every tag has a random version
  token in Redis, and the entry's key embeds the current tokens. Committing a
  write bumps the tokens of whatever tags its rows map to (see
  `app.core.cache_invalidation`), so every dependent entry is orphaned in one
  O(1) write - no SCAN, no key bookkeeping, and no service has to remember
  to invalidate. Orphans just age out via their TTL.
* **No resurrection after eviction.** A missing tag gets a *fresh* random
  token rather than restarting at 0, so if Redis evicts a tag key under
  memory pressure, entries built on the old token can never match again.
* **Stampede protection.** Concurrent misses for the same key in this
  process share a single DB query instead of all hitting Postgres at once.
* **TTL jitter** so entries written together don't all expire together.
* **Fail-open.** If Redis is down or slow to respond, the call just goes to
  the DB - the cache can make things faster, never break them.

Only cache read paths whose output is a pure function of their arguments
and DB state, and whose every source table has a rule in `cache_tags.RULES`.
"""

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import random
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ParamSpec, TypeVar, get_type_hints
from uuid import UUID

from pydantic import TypeAdapter
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.pagination import PaginationParams
from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

_KEY_PREFIX = "cache:q:v1"
_TAG_PREFIX = "cache:tag:v1"
# Must outlive the longest entry TTL, so an entry never outlives its tag.
_TAG_TTL_SECONDS = 60 * 60 * 24 * 7
_TTL_JITTER = 0.1
# Arguments that aren't part of the cache key (the DB session).
_IGNORED_ARGS = frozenset({"db"})

_REDIS_ERRORS = (RedisError, OSError, asyncio.TimeoutError)

# Concurrent misses on the same key in this process await one shared load.
_in_flight: dict[str, asyncio.Future[Any]] = {}


def _tag_key(tag: str) -> str:
    return f"{_TAG_PREFIX}:{tag}"


def _encode_arg(value: Any) -> Any:
    if isinstance(value, PaginationParams):
        return {"page": value.page, "page_size": value.page_size}
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Can't build a cache key from {type(value).__name__}")


async def _tag_tokens(redis: Any, tags: list[str]) -> list[str]:
    keys = [_tag_key(t) for t in tags]
    tokens = await redis.mget(keys)
    missing = [k for k, tok in zip(keys, tokens, strict=True) if tok is None]
    if missing:
        async with redis.pipeline(transaction=False) as pipe:
            for key in missing:
                pipe.set(key, secrets.token_hex(8), nx=True, ex=_TAG_TTL_SECONDS)
            await pipe.execute()
        tokens = await redis.mget(keys)
    return tokens


async def bump_tags(tags: Iterable[str]) -> None:
    """Orphans every cache entry depending on any of `tags`. Called after a
    commit; failures are logged and swallowed, since the TTL still bounds
    how stale anything can get."""
    tags = sorted(set(tags))
    if not tags:
        return
    redis = get_redis_client()
    try:
        async with redis.pipeline(transaction=False) as pipe:
            for tag in tags:
                pipe.set(_tag_key(tag), secrets.token_hex(8), ex=_TAG_TTL_SECONDS)
            await pipe.execute()
    except _REDIS_ERRORS:
        logger.warning("query cache: failed to bump tags %s", tags, exc_info=True)


def cached(
    *,
    name: str,
    ttl_seconds: int,
    tags: Callable[[Mapping[str, Any]], Iterable[str]],
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """`tags` gets the call's bound arguments by name, e.g.
    `tags=lambda a: [CATALOG, braider_tag(a["braider_id"])]`."""

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        signature = inspect.signature(fn)
        adapter: TypeAdapter[Any] | None = None

        def get_adapter() -> TypeAdapter[Any]:
            nonlocal adapter
            if adapter is None:
                adapter = TypeAdapter(get_type_hints(fn)["return"])
            return adapter

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if not get_settings().query_cache_enabled:
                return await fn(*args, **kwargs)

            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = {k: v for k, v in bound.arguments.items() if k not in _IGNORED_ARGS}
            entry_tags = sorted(set(tags(arguments)))

            redis = get_redis_client()
            try:
                tokens = await _tag_tokens(redis, entry_tags)
                raw_key = json.dumps(
                    [arguments, entry_tags, tokens], sort_keys=True, default=_encode_arg
                )
                key = f"{_KEY_PREFIX}:{name}:{hashlib.sha256(raw_key.encode()).hexdigest()[:32]}"
                hit = await redis.get(key)
            except _REDIS_ERRORS:
                logger.warning("query cache: read failed for %s, using DB", name, exc_info=True)
                return await fn(*args, **kwargs)

            if hit is not None:
                return get_adapter().validate_json(hit)

            pending = _in_flight.get(key)
            if pending is not None:
                return await asyncio.shield(pending)

            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            _in_flight[key] = future
            # The slot is held until the entry is in Redis, so a request that
            # misses in the gap between "loaded" and "written" still joins.
            try:
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                    future.exception()  # mark retrieved, so an unawaited one isn't logged
                    raise
                future.set_result(result)

                ttl = max(1, int(ttl_seconds * random.uniform(1 - _TTL_JITTER, 1)))
                try:
                    await redis.set(key, get_adapter().dump_json(result), ex=ttl)
                except _REDIS_ERRORS:
                    logger.warning("query cache: write failed for %s", name, exc_info=True)
            finally:
                _in_flight.pop(key, None)
            return result

        return wrapper

    return decorator
