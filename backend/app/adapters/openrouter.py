"""OpenRouter adapter (B3): per-student keys with a credit limit, through the management API.

Implements the shared ProviderAdapter interface (base.py). The credential is an OpenRouter
*management key*: it can only manage keys, it cannot call models. Rules from the design:
  * A key is created with a credit limit. The secret comes back ONCE, at creation, in
    IssuedAccess.secret. The caller shows it to the student and must not store it. Only the
    key hash is kept (it is the external_ref).
  * `limit_reset` is never sent. OpenRouter's reset goes back to zero each period, which breaks
    rollover. The system recalculates limits itself (key limit = usage so far + team balance).
  * Every key issue, limit change and disable is audited: wrap this adapter in
    AuditedAdapter (below) at the place where the database change happens.

Plain HTTP and Decimal money, no database. The real API shapes are in docs/notes/openrouter.md.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

from app.adapters.base import (
    AdapterError,
    IssuedAccess,
    IssueRequest,
    ProviderAdapter,
    ProviderUnavailable,
    ResourceNotFound,
    ResourceState,
    UsageReading,
)
from app.models import Provider

BASE_URL = "https://openrouter.ai/api/v1"


def _usd(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class OpenRouterAdapter(ProviderAdapter):
    provider = Provider.openrouter
    supports_limits = True

    def __init__(self, secret: str, *, base_url: str = BASE_URL, client: httpx.Client | None = None):
        if not secret:
            raise ValueError("OpenRouter management key is missing")
        self._client = client or httpx.Client(timeout=30)
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}

    # -- the only place that talks to the network, so errors never leak the key -----------
    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = self._client.request(method, self._base + path, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"could not reach OpenRouter ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200]
            message = f"OpenRouter {resp.status_code}: {detail}"
            if resp.status_code == 404:
                raise ResourceNotFound(message)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderUnavailable(message)
            raise AdapterError(message)  # 400, 401 (bad or expired management key), ...
        return resp.json() if resp.content else {}

    # -- ProviderAdapter -----------------------------------------------------------------
    def issue(self, request: IssueRequest) -> IssuedAccess:
        """Create a key with a credit limit (no limit if limit_usd is None). Never sends limit_reset."""
        body: dict[str, Any] = {"name": request.name}
        if request.limit_usd is not None:
            body["limit"] = float(request.limit_usd)
        out = self._call("POST", "/keys", body)
        secret = out.get("key") or (out.get("data") or {}).get("key")
        if not secret:
            raise AdapterError("OpenRouter did not return the key secret")
        return IssuedAccess(external_ref=out["data"]["hash"], secret=secret, details={"name": request.name})

    def revoke(self, external_ref: str) -> None:
        """Delete the key for good. A key that is already gone is fine."""
        try:
            self._call("DELETE", f"/keys/{external_ref}")
        except ResourceNotFound:
            pass

    def fetch_usage(self, external_refs: list[str]) -> dict[str, UsageReading]:
        """Total USD spent per key since it was created. Unknown keys come back as gone.

        One GET per key, on purpose: OpenRouter's key LIST leaves out disabled keys (the real
        probe showed a disabled key vanish from it), so a list would wrongly call them gone.
        """
        out: dict[str, UsageReading] = {}
        for ref in external_refs:
            try:
                k = self._call("GET", f"/keys/{ref}")["data"]
            except ResourceNotFound:
                out[ref] = UsageReading(external_ref=ref, cumulative_usd=Decimal(0), state=ResourceState.gone)
                continue
            out[ref] = UsageReading(
                external_ref=ref,
                cumulative_usd=_usd(k.get("usage", 0)) or Decimal(0),
                state=ResourceState.disabled if k.get("disabled") else ResourceState.active,
                limit_usd=_usd(k.get("limit")),
            )
        return out

    def stop(self, external_ref: str) -> None:
        """Disable the key (it keeps its usage and limit)."""
        self._call("PATCH", f"/keys/{external_ref}", {"disabled": True})

    def resume(self, external_ref: str) -> None:
        self._call("PATCH", f"/keys/{external_ref}", {"disabled": False})

    def set_limit(self, external_ref: str, limit_usd: Decimal) -> None:
        """Change the credit limit. Only `limit` is sent, so nothing else on the key changes."""
        self._call("PATCH", f"/keys/{external_ref}", {"limit": float(limit_usd)})


# (action, key_hash, details) -> None. In the app: lambda a, t, d: write_audit(session, actor, a, "key", t, d)
AuditFn = Callable[[str, str, dict[str, Any]], None]


class AuditedAdapter(ProviderAdapter):
    """Wraps any adapter and writes an audit entry for every key issue, limit change and disable.

    Use it where the database change happens, so both are saved in one transaction (see
    app/core/audit.py). The entry holds the key hash, never the secret. Reads are not audited.
    """

    def __init__(self, inner: ProviderAdapter, audit: AuditFn):
        self._inner = inner
        self._audit = audit
        self.provider = inner.provider
        self.supports_limits = inner.supports_limits

    def issue(self, request: IssueRequest) -> IssuedAccess:
        issued = self._inner.issue(request)
        self._audit("key.issue", issued.external_ref,
                    {"provider": self.provider.value, "name": request.name, "limit_usd": request.limit_usd})
        return issued

    def revoke(self, external_ref: str) -> None:
        self._inner.revoke(external_ref)
        self._audit("key.revoke", external_ref, {"provider": self.provider.value, "how": "deleted"})

    def fetch_usage(self, external_refs: list[str]) -> dict[str, UsageReading]:
        return self._inner.fetch_usage(external_refs)

    def stop(self, external_ref: str) -> None:
        self._inner.stop(external_ref)
        self._audit("key.revoke", external_ref, {"provider": self.provider.value, "how": "disabled"})

    def resume(self, external_ref: str) -> None:
        self._inner.resume(external_ref)
        self._audit("key.enable", external_ref, {"provider": self.provider.value})

    def set_limit(self, external_ref: str, limit_usd: Decimal) -> None:
        self._inner.set_limit(external_ref, limit_usd)
        self._audit("key.set_limit", external_ref, {"provider": self.provider.value, "limit_usd": limit_usd})
