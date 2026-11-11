"""The provider adapter interface. Every provider (OpenRouter, RunPod) sits behind this,
so the rest of the system never talks to a provider directly.

Who implements what:
    base.py, mock.py   Person 2
    runpod.py          Person 1
    openrouter.py      Person 3

Rules for every adapter:
- Money is Decimal USD, never float.
- Usage is the TOTAL since the resource was created; the sync job works out new spend.
- An adapter is built for one credential (the decrypted secret) and never stores or logs it.
- An adapter does no database work and no audit logging; the caller does both.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models import Provider


class AdapterError(Exception):
    """Base class for every adapter failure."""


class ResourceNotFound(AdapterError):
    """The key or pod no longer exists at the provider."""


class NotSupported(AdapterError):
    """This provider can't do that (e.g. set_limit on RunPod: there is no per-pod cap)."""


class ProviderUnavailable(AdapterError):
    """Network error, 5xx or rate limit. Safe to retry on the next sync."""


class ResourceState(str, enum.Enum):
    active = "active"
    disabled = "disabled"
    gone = "gone"


@dataclass(frozen=True)
class IssueRequest:
    name: str
    user_id: str
    group_id: str
    limit_usd: Decimal | None = None
    spec: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IssuedAccess:
    external_ref: str
    secret: str | None = None
    connection: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageReading:
    external_ref: str
    cumulative_usd: Decimal
    state: ResourceState
    limit_usd: Decimal | None = None


class ProviderAdapter(abc.ABC):
    provider: Provider
    supports_limits: bool = False

    @abc.abstractmethod
    def issue(self, request: IssueRequest) -> IssuedAccess:
        """Create a key / launch a pod."""

    @abc.abstractmethod
    def revoke(self, external_ref: str) -> None:
        """Delete the key / terminate the pod for good. Revoking something already gone is fine."""

    @abc.abstractmethod
    def fetch_usage(self, external_refs: list[str]) -> dict[str, UsageReading]:
        """Total USD spent per resource. Unknown refs come back with state=gone, never dropped."""

    @abc.abstractmethod
    def stop(self, external_ref: str) -> None:
        """Pause: disable the key / stop the pod (disk kept, may still cost storage)."""

    @abc.abstractmethod
    def resume(self, external_ref: str) -> None:
        """Undo stop(): re-enable the key / start the pod again."""

    def set_limit(self, external_ref: str, limit_usd: Decimal) -> None:
        """Set a key's spending limit. Providers without per-resource limits raise NotSupported."""
        raise NotSupported(f"{self.provider.value} has no per-resource spend limit")

    def estimate_hourly_cost(self, spec: dict[str, Any]) -> Decimal | None:
        """Pre-launch check: expected USD per hour for a pod, or None when cost isn't time-based."""
        return None