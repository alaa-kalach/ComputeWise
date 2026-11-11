"""B1: the mock adapters behave like the real providers where it matters to enforcement."""

from decimal import Decimal as D

import pytest

from app.adapters import (
    IssueRequest,
    NotSupported,
    ProviderUnavailable,
    ResourceNotFound,
    ResourceState,
    get_adapter,
)
from app.adapters.mock import MockOpenRouterAdapter, MockRunPodAdapter, MockWorld
from app.models import Provider


@pytest.fixture
def world():
    return MockWorld()


@pytest.fixture
def ork(world):
    return MockOpenRouterAdapter("secret", world)


@pytest.fixture
def rp(world):
    return MockRunPodAdapter("secret", world)


def req(**kw):
    return IssueRequest(name="t", user_id="u", group_id="g", **kw)


def usage(adapter, ref):
    return adapter.fetch_usage([ref])[ref]


def test_issue_key_spend_read_back(ork, world):
    """The plan's 'done when': issue a mock key, generate usage, read it back."""
    key = ork.issue(req(limit_usd=D("20")))
    assert key.secret.startswith("sk-or-v1-mock-")
    assert key.secret not in key.external_ref
    world.spend(key.external_ref, D("5"))
    u = usage(ork, key.external_ref)
    assert (u.cumulative_usd, u.state, u.limit_usd) == (D("5"), ResourceState.active, D("20"))


def test_key_cannot_pass_its_limit_or_spend_when_disabled(ork, world):
    key = ork.issue(req(limit_usd=D("10")))
    assert world.spend(key.external_ref, D("25")) == D("10")
    ork.set_limit(key.external_ref, D("12"))
    assert world.spend(key.external_ref, D("5")) == D("2")
    ork.stop(key.external_ref)
    assert world.spend(key.external_ref, D("1")) == 0
    assert usage(ork, key.external_ref).state is ResourceState.disabled
    ork.resume(key.external_ref)
    assert usage(ork, key.external_ref).state is ResourceState.active


def test_pod_bills_gpu_while_running_and_storage_while_stopped(rp, world):
    pod = rp.issue(req(spec={"gpu_type": "RTX A4000", "disk_gb": 73}))
    assert pod.secret is None and "ssh" in pod.connection
    assert D(pod.details["storage_cost_per_hr"]) == D("0.0100")
    world.advance(hours=10)
    assert usage(rp, pod.external_ref).cumulative_usd == D("1.70")
    rp.stop(pod.external_ref)
    world.advance(hours=100)
    assert usage(rp, pod.external_ref).cumulative_usd == D("2.70")
    rp.revoke(pod.external_ref)
    world.advance(hours=100)
    u = usage(rp, pod.external_ref)
    assert u.cumulative_usd == D("2.70") and u.state is ResourceState.gone


def test_pre_launch_estimate(rp):
    assert rp.estimate_hourly_cost({"gpu_type": "A100 PCIe", "gpu_count": 2}) == D("3.28")


def test_runpod_has_no_per_pod_limit(rp):
    pod = rp.issue(req())
    with pytest.raises(NotSupported):
        rp.set_limit(pod.external_ref, D("5"))


def test_unknown_refs_come_back_gone_not_dropped(ork, rp):
    pod = rp.issue(req())
    out = ork.fetch_usage([pod.external_ref, "nope"])
    assert {r.state for r in out.values()} == {ResourceState.gone}
    with pytest.raises(ResourceNotFound):
        ork.stop(pod.external_ref)


def test_revoke_is_idempotent(ork):
    key = ork.issue(req())
    ork.revoke(key.external_ref)
    ork.revoke(key.external_ref)
    ork.revoke("never-existed")
    with pytest.raises(ResourceNotFound):
        ork.resume(key.external_ref)


def test_outage(ork, world):
    world.outage = True
    with pytest.raises(ProviderUnavailable):
        ork.fetch_usage([])


def test_factory():
    assert get_adapter(Provider.openrouter, "s", mode="mock").supports_limits
    assert not get_adapter(Provider.runpod, "s", mode="mock").supports_limits
    with pytest.raises(NotSupported):  # RunPod's real adapter is not written yet (the OpenRouter one is)
        get_adapter(Provider.runpod, "s", mode="real")
    with pytest.raises(ValueError):
        get_adapter(Provider.runpod, "", mode="mock")