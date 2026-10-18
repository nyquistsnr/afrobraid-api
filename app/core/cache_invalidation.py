"""Automatic query-cache invalidation, driven by what a session actually
writes.

SQLAlchemy events record the cache tags (`app.core.cache_tags`) for every
row flushed and every bulk INSERT/UPDATE/DELETE executed in a session.
Rolled-back work is dropped; committed work is handed to
`CacheInvalidatingSession.commit`, which bumps the tags right after the
commit returns - before the request carries on and before any other request
could read the new data back through a stale entry for more than an instant.
Because this hooks the session itself, API requests, arq tasks and crons all
invalidate the same way with no per-service code.
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session

from app.core import query_cache
from app.core.cache_tags import tags_for

_PENDING = "query_cache_pending_tags"
_COMMITTED = "query_cache_committed_tags"


def _pending(session: Session) -> set[str]:
    return session.info.setdefault(_PENDING, set())


@event.listens_for(Session, "after_flush")
def _collect_flushed(session: Session, _flush_context: Any) -> None:
    tags = _pending(session)
    for op, objs in (("insert", session.new), ("delete", session.deleted)):
        for obj in objs:
            tags |= tags_for(session, obj.__table__.name, obj, op)
    for obj in session.dirty:
        if session.is_modified(obj, include_collections=False):
            tags |= tags_for(session, obj.__table__.name, obj, "update")


@event.listens_for(Session, "do_orm_execute")
def _collect_bulk(state: ORMExecuteState) -> None:
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    table = getattr(state.statement, "table", None)
    if table is None:
        return
    op = "insert" if state.is_insert else "update" if state.is_update else "delete"
    _pending(state.session).update(tags_for(state.session, table.name, None, op))


@event.listens_for(Session, "after_commit")
def _promote_committed(session: Session) -> None:
    pending = session.info.pop(_PENDING, None)
    if pending:
        session.info.setdefault(_COMMITTED, set()).update(pending)


@event.listens_for(Session, "after_rollback")
def _discard_rolled_back(session: Session) -> None:
    session.info.pop(_PENDING, None)


class CacheInvalidatingSession(AsyncSession):
    async def commit(self) -> None:
        await super().commit()
        await self._bump_committed()

    async def close(self) -> None:
        # Catches commits that didn't go through `commit()` above (e.g. an
        # `async with session.begin():` block).
        await self._bump_committed()
        await super().close()

    async def _bump_committed(self) -> None:
        tags = self.sync_session.info.pop(_COMMITTED, None)
        if tags:
            await query_cache.bump_tags(tags)
