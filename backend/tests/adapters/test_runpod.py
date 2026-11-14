"""RunPod adapter (B4) against the REAL responses recorded by scripts/runpod_probe.py
(docs/notes/runpod_samples). No network, no key, no money."""

import json
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from app.adapters import (
    AdapterError,
    IssueRequest,
    ProviderUnavailable,
    ResourceNotFound,
    ResourceState,
)
from app.adapters.runpod import (
    InsufficientBalance,
    NoCapacity,
    RunPodAdapter,
    hourly_rates,
    parse_pod,
)

SAMPLES = Path(__file__).resolve().parents[3] / "docs" / "notes" / "runpod_samples"
KEY = "rpa_THE-SECRET-RUNPOD-KEY"
POD = "s4gxa09xywsjml"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


def reply(name: str) -> httpx.Response:
    s = sample(name)
    if s["body"] == "":
        return httpx.Response(s["status_code"])
    return httpx.Response(s["status_code"], json=s["body"])


class FakeRunPod:
    """Answers like RunPod v2 by replaying the recorded samples, and records every request."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.override: dict[tuple[str, str], httpx.Response] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.headers["Authorization"] == f"Bearer {KEY}"
        assert str(req.url).startswith("https://api.runpod.io/v2/")  # v2 only (D14)
        path, method = req.url.path.removeprefix("/v2"), req.method
        if (method, path) in self.override:
            return self.override[(method, path)]
        if method == "GET" and path == "/catalog/gpus":
            cloud = req.url.params["cloud"]
            return reply({"COMMUNITY": "04_catalog_gpus_community.json", "SECURE": "03_catalog_gpus_secure.json"}[cloud])
        if method == "POST" and path == "/pods":
            return reply("06_create_pod.json")
        if method == "GET" and path == f"/pods/{POD}":
            return reply("08_pod_running_after_30s.json")
        if method == "GET" and path.startswith("/pods/"):
            return reply("20_error_unknown_pod.json")
        if method == "POST" and path == f"/pods/{POD}/action":
            action = json.loads(req.content)["action"]
            return reply({"stop": "10_action_stop.json", "start": "13_action_start.json",
                          "terminate": "15_action_terminate.json"}[action])
        if method == "GET" and path == "/billing/pods":
            return reply("92_billing_later_20261111T1841_pod_daily.json")
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def fake():
    return FakeRunPod()


@pytest.fixture
def rp(fake):
    return RunPodAdapter(KEY, client=httpx.Client(transport=httpx.MockTransport(fake)))


# ---------------------------------------------------------------- catalog
def test_list_gpus_community(rp, fake):
    offers = rp.list_gpus("community")
    q = fake.calls[0].url.params
    assert (q["include"], q["product"], q["cloud"]) == ("AVAILABILITY", "POD", "COMMUNITY")
    by_id = {o.gpu_type_id: o for o in offers}
    a2000 = by_id["NVIDIA RTX A2000"]
    assert (a2000.price_per_hour, a2000.availability, a2000.cloud, a2000.vram_gb) == (D("0.12"), "low", "community", 6)
    assert by_id["NVIDIA RTX A4000"].availability == "none"  # kept, shown as not launchable
    assert "AMD Instinct MI300X OAM" not in by_id  # not offered on community (maxCount 0)
    assert [o.price_per_hour for o in offers] == sorted(o.price_per_hour for o in offers)


def test_list_gpus_secure_uses_secure_price(rp):
    by_id = {o.gpu_type_id: o for o in rp.list_gpus("secure")}
    assert by_id["NVIDIA RTX A4000"].price_per_hour == D("0.25")
    assert "NVIDIA RTX A2000" not in by_id  # secure maxCount 0


# ---------------------------------------------------------------- launch
def test_issue_sends_exactly_the_body_the_probe_used(rp, fake):
    spec = {"gpu_type_id": "NVIDIA RTX A2000", "cloud": "community", "gpu_count": 1,
            "container_disk_gb": 10, "volume_gb": 10, "jupyter": True}
    out = rp.issue(IssueRequest(name="computewise-probe", user_id="u", group_id="g", spec=spec))
    sent = json.loads(fake.calls[0].content)
    assert sent == sample("06_create_pod.json")["request"]["json"]  # RunPod accepted this exact body
    assert out.external_ref == POD
    assert out.secret is None and out.connection == {}  # not ready yet: RUNNING without runtime
    assert out.details["runpod_cost_per_hr"] == "0.1200"
    assert "JUPYTER_PASSWORD" not in json.dumps(out.details)


def test_issue_without_jupyter_exposes_only_ssh(rp, fake):
    rp.issue(IssueRequest(name="n", user_id="u", group_id="g",
                          spec={"gpu_type_id": "NVIDIA RTX A2000", "cloud": "secure", "jupyter": False}))
    sent = json.loads(fake.calls[0].content)
    assert sent["ports"] == ["22/tcp"] and sent["startJupyter"] is False and sent["cloud"] == "SECURE"


# ---------------------------------------------------------------- pod status
def test_new_pod_is_running_but_not_ready():
    s = parse_pod(sample("06_create_pod.json")["body"])
    assert s.status == "RUNNING" and s.ready is False
    assert s.ssh_proxy == "ssh s4gxa09xywsjml-64410f89@ssh.runpod.io"
    assert s.ssh_direct is None and s.jupyter_url is None


def test_ready_pod_has_connection_details(rp):
    s = rp.pod_status(POD)
    assert s.ready is True and s.gpu_util_percent == 0
    assert s.ssh_direct == "ssh root@194.26.196.170 -p 22796"
    assert s.jupyter_url == "https://s4gxa09xywsjml-8888.proxy.runpod.net"
    assert s.started_at.isoformat() == "2026-11-11T11:24:14.368000+00:00"
    assert s.cost_per_hr == D("0.12")
    assert "jupyter_password" not in repr(s)  # never ends up in a log line


def test_stopped_pod():
    s = parse_pod(sample("12_pod_stopped_after_20s.json")["body"])
    assert s.status == "EXITED" and s.ready is False and s.jupyter_url is None


# ---------------------------------------------------------------- actions
def test_stop_resume_terminate(rp, fake):
    rp.stop(POD)
    rp.resume(POD)
    rp.revoke(POD)
    assert [json.loads(c.content)["action"] for c in fake.calls] == ["stop", "start", "terminate"]
    assert all(c.url.path == f"/v2/pods/{POD}/action" for c in fake.calls)


def test_terminating_a_pod_that_is_already_gone_is_fine(rp, fake):
    fake.override[("POST", "/pods/gone123/action")] = reply("20_error_unknown_pod.json")
    rp.revoke("gone123")


def test_stopping_an_unknown_pod_is_resource_not_found(rp, fake):
    fake.override[("POST", "/pods/gone123/action")] = reply("20_error_unknown_pod.json")
    with pytest.raises(ResourceNotFound):
        rp.stop("gone123")


def test_set_limit_is_not_supported(rp):
    from app.adapters import NotSupported

    with pytest.raises(NotSupported):
        rp.set_limit(POD, D("5"))


# ---------------------------------------------------------------- usage (billed, lagging)
def test_fetch_usage_reads_billing_and_state(rp, fake):
    out = rp.fetch_usage([POD, "gone123"])
    assert out[POD].cumulative_usd == D("0.0014") and out[POD].state == ResourceState.active
    assert out["gone123"].state == ResourceState.gone and out["gone123"].cumulative_usd == 0
    billing = next(c for c in fake.calls if c.url.path == "/v2/billing/pods")
    assert billing.url.params["bucketSize"] == "day"


def test_fetch_usage_of_nothing_makes_no_call(rp, fake):
    assert rp.fetch_usage([]) == {} and fake.calls == []


# ---------------------------------------------------------------- errors never leak the key
@pytest.mark.parametrize(
    "status, exc",
    [(400, NoCapacity), (402, InsufficientBalance), (429, ProviderUnavailable), (503, ProviderUnavailable),
     (401, AdapterError), (403, AdapterError), (422, AdapterError)],
)
def test_error_mapping(fake, rp, status, exc):
    fake.override[("POST", "/pods")] = httpx.Response(status, json={"detail": f"nope {KEY}"[:5], "status": status})
    with pytest.raises(exc) as info:
        rp.issue(IssueRequest(name="n", user_id="u", group_id="g", spec={"gpu_type_id": "x"}))
    assert KEY not in str(info.value)


def test_bad_key_is_an_adapter_error(fake, rp):
    fake.override[("GET", "/catalog/gpus")] = reply("22_error_bad_key.json")
    with pytest.raises(AdapterError, match="Subject token is invalid"):
        rp.list_gpus("community")


def test_network_error_is_provider_unavailable():
    def boom(req):
        raise httpx.ConnectError("down")

    rp = RunPodAdapter(KEY, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(ProviderUnavailable) as info:
        rp.pod_status(POD)
    assert KEY not in str(info.value)


def test_empty_key_refused():
    with pytest.raises(ValueError):
        RunPodAdapter("")


# ---------------------------------------------------------------- cost per hour
def test_hourly_rates_match_the_notes():
    r = hourly_rates(D("0.12"), 1, 10, 10)
    assert r.gpu == D("0.12")
    # 10 GB container + 10 GB volume at $0.10/GB/month while running; volume at $0.20 stopped.
    assert r.running_storage == D("0.002740")
    assert r.stopped_storage == D("0.002740")  # notes: "10 GB volume ~ $0.0027/h"
    assert r.running == D("0.122740")


def test_estimate_hourly_cost_from_live_catalog(rp):
    est = rp.estimate_hourly_cost({"gpu_type_id": "NVIDIA RTX A2000", "cloud": "community"})
    assert est == D("0.122740")
    assert rp.estimate_hourly_cost({"gpu_type_id": "nope", "cloud": "community"}) is None
