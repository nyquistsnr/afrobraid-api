# Braider Onboarding — Payment Setup: Dashboard Link

`POST /api/v1/braiders/onboarding/payment-setup/dashboard-link`

Returns a fresh, single-use link to the braider's Stripe Express Dashboard (balance, payouts, transaction history). Part of the `PAYMENT_SETUP` onboarding step (Stripe Connect).

## Auth

Requires a Bearer JWT for a user with role `BRAIDER`.

```
Authorization: Bearer <access_token>
```

- Missing/invalid/expired token → `401 INVALID_ACCESS_TOKEN`
- Wrong role → `403 FORBIDDEN`

## Request

No path/query params, no body.

## Response `200`

```json
{
  "status": "success",
  "status_label": "Success",
  "data": {
    "dashboard_url": "https://connect.stripe.com/express/abcd1234..."
  },
  "error": null
}
```

**Accounts without an Express Dashboard** (Custom accounts - currently only the seeded test braiders, whose KYC is completed over the API): Stripe can't issue a dashboard login for these, so `dashboard_url` is instead a Stripe-hosted *account update* page (payout bank account and personal details), returning to `STRIPE_CONNECT_RETURN_URL`. Same response shape - the frontend just opens the URL either way.

`dashboard_url` is single-use and short-lived (Stripe's standard login-link expiry) — fetch a new one each time rather than caching it. Works even if Stripe onboarding isn't fully finished yet: Stripe routes the braider through any outstanding requirements first before showing the dashboard itself, so this can be safely offered as soon as a Stripe Express account exists, not only once `is_complete` (see the `/status` endpoint) is `true`.

## Errors

| Code | HTTP status | When |
|---|---|---|
| `STRIPE_ACCOUNT_NOT_FOUND` | 404 | The braider hasn't started Stripe Connect onboarding yet (no Stripe Express account exists) — call `POST .../payment-setup/account-link` first. |
| `STRIPE_API_UNAVAILABLE` | 502 | Stripe's API errored or was unreachable when generating the link. |
| `INVALID_ACCESS_TOKEN` | 401 | missing/invalid/expired Bearer token |
| `FORBIDDEN` | 403 | caller isn't a `BRAIDER` |

All error responses share the standard envelope:

```json
{
  "status": "error",
  "status_label": "Error",
  "data": null,
  "error": { "code": "STRIPE_ACCOUNT_NOT_FOUND", "message": "..." }
}
```

## Related endpoints in this module

For context — same `/api/v1/braiders/onboarding/payment-setup` prefix:

| Method | Path | Purpose |
|---|---|---|
| POST | `/account-link` | Create (or resume) the Stripe Express account and get a hosted onboarding link |
| POST | `/dashboard-link` | *(this endpoint)* |
| GET | `/status` | Current Stripe account status (`charges_enabled`, `payouts_enabled`, `requirements_currently_due`, `is_complete`, etc.) |
| POST | `/refresh` | Re-check status with Stripe after returning from the onboarding flow |
