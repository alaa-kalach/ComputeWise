"""OpenRouter adapter against a fake server (no network, no key)."""

import json
from decimal import Decimal as D

import httpx
import pytest

from app.adapters import (
    AdapterError,
    IssueRequest,
    ProviderAdapter,
    ProviderUnavailable,
    ResourceNotFound,
    ResourceState,
    get_adapter,
)
from app.adapters.openrouter import AuditedAdapter, OpenRouterAdapter
from app.models import Provider

SECRET = "sk-or-v1-THE-STUDENT-KEY"
MGMT = "sk-or-v1-MANAGEMENT"


def key_json(**over):
    base = {"hash": "h1", "name": "alice", "disabled": False, "limit": 10, "limit_remaining": 10,
            "limit_reset": None, "usage": 0, "usage_daily": 0, "usage_weekly": 0, "usage_monthly": 0}
    base.update(over)
    return base


class Fake:
    """Records every request and answers like OpenRouter's key API."""

    def __init__(self, keys=None):
        self.calls = []
        self.keys = keys if keys is not None else {"h1": key_json()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, request.headers["authorization"]))
        path = request.url.path.removeprefix("/api/v1")
        if path == "/keys" and request.method == "POST":
            self.keys["h1"] = key_json(name=body["name"], limit=body.get("limit"))
            return httpx.Response(201, json={"data": self.keys["h1"], "key": SECRET})
        if path == "/keys" and request.method == "GET":
            # like the real API: the list leaves out disabled keys
            live = [k for k in self.keys.values() if not k["disabled"]]
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json={"data": live[offset:offset + 100]})
        h = path.removeprefix("/keys/")
        if h not in self.keys:
            return httpx.Response(404, json={"error": {"message": "API key not found", "code": 404}})
        if request.method == "PATCH":
            self.keys[h].update(body)
            return httpx.Response(200, json={"data": self.keys[h]})
        if request.method == "DELETE":
            del self.keys[h]
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(200, json={"data": self.keys[h]})


def make(fake=None):
    fake = fake or Fake()
    return OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(fake))), fake


def req(**kw):
    return IssueRequest(name="alice", user_id="u", group_id="g", **kw)


def test_it_is_a_provider_adapter():
    ad, _ = make()
    assert isinstance(ad, ProviderAdapter) and ad.provider is Provider.openrouter and ad.supports_limits


def test_get_adapter_real_mode_returns_it():
    assert isinstance(get_adapter(Provider.openrouter, MGMT, mode="real"), OpenRouterAdapter)


def test_issue_returns_secret_once_and_hash_is_the_reference():
    ad, fake = make()
    issued = ad.issue(req(limit_usd=D("10.50")))
    assert issued.secret == SECRET and issued.external_ref == "h1"
    method, path, body, auth = fake.calls[0]
    assert (method, path, body) == ("POST", "/api/v1/keys", {"name": "alice", "limit": 10.5})
    assert auth == f"Bearer {MGMT}"


def test_issue_without_limit_sends_no_limit():
    ad, fake = make()
    ad.issue(req())
    assert fake.calls[0][2] == {"name": "alice"}


def test_limit_reset_is_never_sent():
    ad, fake = make()
    ref = ad.issue(req(limit_usd=D(10))).external_ref
    ad.set_limit(ref, D(25))
    ad.stop(ref)
    ad.resume(ref)
    assert all("limit_reset" not in (c[2] or {}) for c in fake.calls)


def test_set_limit_sends_only_the_limit_and_stop_only_disabled():
    ad, fake = make()
    ad.set_limit("h1", D("42.00"))
    assert fake.calls[-1][2] == {"limit": 42.0}
    ad.stop("h1")
    assert fake.calls[-1][2] == {"disabled": True}
    assert ad.fetch_usage(["h1"])["h1"].state is ResourceState.disabled
    ad.resume("h1")
    assert ad.fetch_usage(["h1"])["h1"].state is ResourceState.active


def test_fetch_usage_is_decimal_total_and_unknown_is_gone():
    ad, fake = make()
    fake.keys["h1"]["usage"] = 4.25
    out = ad.fetch_usage(["h1", "nope"])
    assert out["h1"].cumulative_usd == D("4.25") and out["h1"].limit_usd == D("10")
    assert out["nope"].state is ResourceState.gone and out["nope"].cumulative_usd == 0


def test_a_disabled_key_is_disabled_not_gone():
    ad, _ = make()
    ad.stop("h1")  # the real key LIST hides disabled keys; usage must still be found
    assert ad.fetch_usage(["h1"])["h1"].state is ResourceState.disabled


def test_fetch_usage_many_keys():
    keys = {f"h{i}": key_json(hash=f"h{i}", usage=i) for i in range(230)}
    ad, _ = make(Fake(keys))
    out = ad.fetch_usage(["h3", "h150", "h229"])
    assert [out[r].cumulative_usd for r in ("h3", "h150", "h229")] == [3, 150, 229]


def test_revoke_deletes_and_a_missing_key_is_fine():
    ad, fake = make()
    ad.revoke("h1")
    assert "h1" not in fake.keys
    ad.revoke("h1")  # already gone: no error


def test_unknown_key_on_stop_is_resource_not_found():
    ad, _ = make()
    with pytest.raises(ResourceNotFound):
        ad.stop("nope")


def test_bad_management_key_is_an_adapter_error_without_the_key_in_it():
    def boom(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API key", "code": 401}})
    ad = OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(AdapterError) as e:
        ad.fetch_usage(["h1"])
    assert not isinstance(e.value, (ProviderUnavailable, ResourceNotFound)) and MGMT not in str(e.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_server_errors_are_retryable(status):
    ad = OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={}))))
    with pytest.raises(ProviderUnavailable):
        ad.issue(req())


def test_network_failure_is_retryable_and_hides_the_key():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    ad = OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(ProviderUnavailable) as e:
        ad.issue(req())
    assert MGMT not in str(e.value)


def test_missing_secret_in_response_is_an_error():
    ad = OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(201, json={"data": key_json()}))))
    with pytest.raises(AdapterError):
        ad.issue(req())


def test_empty_management_key_refused():
    with pytest.raises(ValueError):
        OpenRouterAdapter("")


def test_every_issue_limit_change_and_disable_is_audited_without_the_secret():
    ad, _ = make()
    log = []
    audited = AuditedAdapter(ad, lambda action, target, details: log.append((action, target, details)))
    issued = audited.issue(req(limit_usd=D(10)))
    audited.set_limit("h1", D(20))
    audited.stop("h1")
    audited.resume("h1")
    audited.fetch_usage(["h1"])  # a read is not audited
    audited.revoke("h1")
    assert [a for a, _, _ in log] == ["key.issue", "key.set_limit", "key.revoke", "key.enable", "key.revoke"]
    assert all(t == "h1" for _, t, _ in log)
    assert SECRET not in json.dumps(log, default=str) and issued.secret == SECRET
    assert audited.provider is Provider.openrouter and audited.supports_limits


def test_a_failed_call_writes_no_audit_entry():
    ad, _ = make()
    log = []
    audited = AuditedAdapter(ad, lambda *a: log.append(a))
    with pytest.raises(ResourceNotFound):
        audited.stop("nope")
    assert log == []
