# OpenRouter key management: real API notes (B3)

Recorded on 2026-11-11 with `python -m scripts.openrouter_probe` against a real management key.
The raw responses (secrets and account ids removed) are in `openrouter_samples/`.

## Basics
- Base URL `https://openrouter.ai/api/v1`, header `Authorization: Bearer <management key>`.
- A management key can only manage keys. It cannot call models. A normal key is rejected on `/keys`.
- Creating a management key and creating or deleting keys costs nothing. Only the spend of the
  keys counts, and it comes out of the account's credit.

## Calls we use
| What | Call | Body | Response |
|---|---|---|---|
| List keys | `GET /keys?offset=0` | none | `{"data": [key, ...]}`, 100 per page, `offset=100` for the next |
| Create key | `POST /keys` | `{"name": "...", "limit": 10.0}` | 201 `{"data": key, "key": "sk-or-v1-..."}` |
| Read one key | `GET /keys/{hash}` | none | `{"data": key}` |
| Change limit | `PATCH /keys/{hash}` | `{"limit": 20.0}` | `{"data": key}` |
| Disable / enable | `PATCH /keys/{hash}` | `{"disabled": true}` | `{"data": key}` |
| Delete | `DELETE /keys/{hash}` | none | `{"deleted": true}` |

Each call is one method in `app/adapters/openrouter.py` (`issue_key`, `set_limit`, `disable_key`,
`enable_key`, `fetch_usage`, `list_keys`, `delete_key`).

## The key object (`data`)
`hash`, `name`, `label` (masked, like `sk-or-v1-abc...3f9`), `disabled`, `limit` (USD, null = none),
`limit_remaining`, `limit_reset`, `include_byok_in_limit`, `usage`, `usage_daily`, `usage_weekly`,
`usage_monthly` (all USD), `byok_usage*`, `created_at`, `updated_at` (null until changed),
`expires_at`, `last_used_at`, `external_user`, `creator_user_id`, `workspace_id`.

## Rules we follow (design document)
- **The secret comes back once**, in the top-level `key` field of the create response. It is never
  returned again (`label` is only a masked hint). We show it to the student and store only the
  `hash`, which is the `external_ref` of the issued resource.
- **Never send `limit_reset`.** OpenRouter would reset the limit to zero each period (daily, weekly or
  monthly, at midnight UTC), which breaks rollover. We send only `limit` and recalculate it ourselves
  after every sync: key limit = key usage so far + team remaining balance. New keys come back with
  `limit_reset: null`, and we keep it that way.
- A limit change sends only `{"limit": n}`, so nothing else on the key changes.
- Every key issue, limit change and disable writes an audit entry (`key.issue`, `key.set_limit`,
  `key.revoke`) holding the hash, never the secret.

## Errors
| Case | Status | Body |
|---|---|---|
| Unknown hash | 404 | `{"error": {"message": "API key not found", "code": 404}}` |
| Bad or non-management key | 401 | `{"error": {"message": "Invalid API key", "code": 401}}` |
| Expired management key | 401 | message `API key expired` (from the docs, not tried) |
| Too many requests | 429 | from the docs, not tried |

The adapter turns these into `OpenRouterError(status, message)`. The message never contains a key.

## Things to know
- **The key LIST hides disabled keys.** Found by the probe: after a key was disabled it no longer
  showed up in `GET /keys`, but `GET /keys/{hash}` still returns it with `disabled: true`. So the
  adapter reads usage with one GET per key. Only a 404 on that call means the key is gone.
- Money comes back as plain numbers, so the adapter reads them as `Decimal`.
- `usage` is all-time spend of that key. `usage_monthly` is the calendar month (UTC).
- The page does not say what `limit: null` does on a PATCH. We never send it.
- Sources: OpenRouter docs "Management API Keys" and "Update keys".
