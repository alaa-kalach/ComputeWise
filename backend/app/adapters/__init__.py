"""Get the adapter for a provider: fake or real, chosen in one place.

    adapter = get_adapter(Provider.openrouter, decrypted_secret)

ADAPTER_MODE in .env picks the implementation:
    mock  (default) the fakes in mock.py: no network, no money
    real            openrouter.py (Person 3) and runpod.py (Person 1)

Tests can pass mode="mock" directly. Never log or store the secret you pass in.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.adapters.base import (
    AdapterError,
    IssuedAccess,
    IssueRequest,
    NotSupported,
    ProviderAdapter,
    ProviderUnavailable,
    ResourceNotFound,
    ResourceState,
    UsageReading,
)
from app.models import Provider

Mode = Literal["mock", "real"]


class _AdapterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")
    adapter_mode: Mode = "mock"


@lru_cache
def adapter_mode() -> Mode:
    return _AdapterSettings().adapter_mode


def get_adapter(provider: Provider, secret: str, *, mode: Mode | None = None) -> ProviderAdapter:
    mode = mode or adapter_mode()
    if mode == "mock":
        from app.adapters.mock import MockOpenRouterAdapter, MockRunPodAdapter

        cls: type[ProviderAdapter] = MockOpenRouterAdapter if provider is Provider.openrouter else MockRunPodAdapter
        return cls(secret)  # type: ignore[call-arg]

    if provider is Provider.openrouter:
        from app.adapters import openrouter as mod

        name = "OpenRouterAdapter"
    else:
        from app.adapters import runpod as mod

        name = "RunPodAdapter"
    real = getattr(mod, name, None)
    if real is None:
        raise NotSupported(f"{mod.__name__}.{name} is not implemented yet; use ADAPTER_MODE=mock")
    return real(secret)


__all__ = [
    "AdapterError",
    "IssueRequest",
    "IssuedAccess",
    "NotSupported",
    "ProviderAdapter",
    "ProviderUnavailable",
    "ResourceNotFound",
    "ResourceState",
    "UsageReading",
    "adapter_mode",
    "get_adapter",
]