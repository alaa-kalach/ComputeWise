"""OpenRouter adapter (B3): per-student keys with a credit limit, through the management API.

The owning credential is an OpenRouter *management key*. It can only manage keys, it cannot
call models. Rules from the design:
  * A key is created with a credit limit. The secret comes back ONCE, at creation. The caller
    shows it to the student and must not store it. Only the key hash is kept (external_ref).
  * `limit_reset` is never sent. OpenRouter's reset goes back to zero each period, which breaks
    rollover. The system recalculates limits itself (key limit = usage so far + team balance).
  * Every key issue, limit change and disable is written to the audit log (AuditedOpenRouter).

Plain HTTP and plain numbers, no database. The API shapes are in docs/notes/openrouter.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterError(Exception):
    """A failed OpenRouter call. The message never contains a key."""

    def __init__(self, status: int | None, message: str):
        super().__init__(f"OpenRouter {status}: {message}" if status else f"OpenRouter: {message}")
        self.status = status


@dataclass(frozen=True)
class KeyInfo:
    """What OpenRouter reports about one key. Money is in USD."""

    hash: str
    name: str
    disabled: bool
    limit_usd: Decimal | None  # None = no limit
    limit_remaining_usd: Decimal | None
    usage_usd: Decimal  # all-time spend of the key
    usage_daily_usd: Decimal
    usage_weekly_usd: Decimal
    usage_monthly_usd: Decimal


@dataclass(frozen=True)
class IssuedKey:
    """Returned once, by issue_key(). The secret is hidden from repr and logs on purpose."""

    info: KeyInfo
    secret: str = field(repr=False)

    @property
    def external_ref(self) -> str:
        return self.info.hash  # what the system stores (credentials/resources table)


def _usd(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _info(data: dict[str, Any]) -> KeyInfo:
    return KeyInfo(
        hash=data["hash"],
        name=data.get("name") or "",
        disabled=bool(data.get("disabled", False)),
        limit_usd=_usd(data.get("limit")),
        limit_remaining_usd=_usd(data.get("limit_remaining")),
        usage_usd=_usd(data.get("usage", 0)) or Decimal(0),
        usage_daily_usd=_usd(data.get("usage_daily", 0)) or Decimal(0),
        usage_weekly_usd=_usd(data.get("usage_weekly", 0)) or Decimal(0),
        usage_monthly_usd=_usd(data.get("usage_monthly", 0)) or Decimal(0),
    )


class OpenRouterAdapter:
    def __init__(
        self,
        management_key: str,
        *,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
    ):
        if not management_key:
            raise ValueError("OpenRouter management key is missing")
        self._client = client or httpx.Client(timeout=30)
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {management_key}", "Content-Type": "application/json"}

    # -- one place that talks to the network, so errors never leak the key ----------------
    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = self._client.request(method, self._base + path, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise OpenRouterError(None, f"could not reach OpenRouter ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200]
            raise OpenRouterError(resp.status_code, str(detail))
        return resp.json() if resp.content else {}

    # -- the four things the design asks of an adapter -----------------------------------
    def issue_key(self, name: str, limit_usd: Decimal | float | int) -> IssuedKey:
        """Create a key with a credit limit. Never sends limit_reset."""
        out = self._call("POST", "/keys", {"name": name, "limit": float(limit_usd)})
        secret = out.get("key") or (out.get("data") or {}).get("key")
        if not secret:
            raise OpenRouterError(None, "OpenRouter did not return the key secret")
        return IssuedKey(info=_info(out["data"]), secret=secret)

    def set_limit(self, key_hash: str, limit_usd: Decimal | float | int) -> KeyInfo:
        """Change the credit limit. Only `limit` is sent, so nothing else is touched."""
        out = self._call("PATCH", f"/keys/{key_hash}", {"limit": float(limit_usd)})
        return _info(out["data"])

    def disable_key(self, key_hash: str) -> KeyInfo:
        out = self._call("PATCH", f"/keys/{key_hash}", {"disabled": True})
        return _info(out["data"])

    def enable_key(self, key_hash: str) -> KeyInfo:
        out = self._call("PATCH", f"/keys/{key_hash}", {"disabled": False})
        return _info(out["data"])

    def fetch_usage(self, key_hash: str) -> KeyInfo:
        """Usage and limit of one key."""
        return _info(self._call("GET", f"/keys/{key_hash}")["data"])

    def list_keys(self) -> list[KeyInfo]:
        """Every key on the account (pages of 100)."""
        keys: list[KeyInfo] = []
        offset = 0
        while True:
            page = self._call("GET", f"/keys?offset={offset}").get("data", [])
            keys += [_info(d) for d in page]
            if len(page) < 100:
                return keys
            offset += 100

    def delete_key(self, key_hash: str) -> None:
        self._call("DELETE", f"/keys/{key_hash}")


# (action, key_hash, details) -> None. In the app: lambda a, t, d: write_audit(session, actor, a, "key", t, d)
AuditFn = Callable[[str, str, dict[str, Any]], None]


class AuditedOpenRouter:
    """The adapter plus an audit entry for every key issue, limit change and disable.

    Call it inside the same transaction as the database change, so both are saved together
    (see app/core/audit.py). The entry holds the key hash, never the secret.
    """

    def __init__(self, adapter: OpenRouterAdapter, audit: AuditFn):
        self._adapter = adapter
        self._audit = audit

    def issue_key(self, name: str, limit_usd: Decimal | float | int) -> IssuedKey:
        issued = self._adapter.issue_key(name, limit_usd)
        self._audit("key.issue", issued.info.hash, {"provider": "openrouter", "name": name, "limit_usd": limit_usd})
        return issued

    def set_limit(self, key_hash: str, limit_usd: Decimal | float | int) -> KeyInfo:
        info = self._adapter.set_limit(key_hash, limit_usd)
        self._audit("key.set_limit", key_hash, {"provider": "openrouter", "limit_usd": limit_usd})
        return info

    def disable_key(self, key_hash: str) -> KeyInfo:
        info = self._adapter.disable_key(key_hash)
        self._audit("key.revoke", key_hash, {"provider": "openrouter", "how": "disabled"})
        return info

    def fetch_usage(self, key_hash: str) -> KeyInfo:
        return self._adapter.fetch_usage(key_hash)
