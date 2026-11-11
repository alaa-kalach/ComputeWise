"""OpenRouter adapter against a fake server (no network, no key)."""

import json
from decimal import Decimal

import httpx
import pytest

from app.adapters.openrouter import AuditedOpenRouter, OpenRouterAdapter, OpenRouterError

SECRET = "sk-or-v1-THE-STUDENT-KEY"
MGMT = "sk-or-v1-MANAGEMENT"


def key_json(**over):
    base = {"hash": "h1", "name": "alice", "disabled": False, "limit": 10, "limit_remaining": 10,
            "limit_reset": None, "usage": 0, "usage_daily": 0, "usage_weekly": 0, "usage_monthly": 0}
    base.update(over)
    return base


class Fake:
    """Records every request and answers like OpenRouter's key API."""

    def __init__(self):
        self.calls = []
        self.state = key_json()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, request.headers["authorization"]))
        if request.url.path == "/api/v1/keys" and request.method == "POST":
            self.state = key_json(name=body["name"], limit=body.get("limit"), limit_remaining=body.get("limit"))
            return httpx.Response(201, json={"data": self.state, "key": SECRET})
        if request.method == "PATCH":
            self.state.update(body)
            return httpx.Response(200, json={"data": self.state})
        if request.method == "GET" and request.url.path == "/api/v1/keys":
            return httpx.Response(200, json={"data": [key_json(hash=f"h{i}") for i in range(100 if request.url.params["offset"] == "0" else 3)]})
        if request.method == "GET":
            self.state.update(usage=4.25, limit_remaining=5.75)
            return httpx.Response(200, json={"data": self.state})
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"error": {"message": "nope"}})


def make(fake=None):
    fake = fake or Fake()
    return OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(fake))), fake


def test_issue_key_returns_secret_once_and_hash_is_the_reference():
    ad, fake = make()
    issued = ad.issue_key("alice", Decimal("10.50"))
    assert issued.secret == SECRET
    assert issued.external_ref == "h1"
    assert SECRET not in repr(issued)  # the secret never shows up in logs
    method, path, body, auth = fake.calls[0]
    assert (method, path, body) == ("POST", "/api/v1/keys", {"name": "alice", "limit": 10.5})
    assert auth == f"Bearer {MGMT}"


def test_limit_reset_is_never_sent():
    ad, fake = make()
    issued = ad.issue_key("alice", 10)
    ad.set_limit(issued.external_ref, 25)
    ad.disable_key("h1")
    ad.enable_key("h1")
    assert all("limit_reset" not in (c[2] or {}) for c in fake.calls)


def test_set_limit_sends_only_the_limit_and_disable_only_disabled():
    ad, fake = make()
    ad.set_limit("h1", Decimal("42.00"))
    assert fake.calls[-1][2] == {"limit": 42.0}
    assert ad.disable_key("h1").disabled is True
    assert fake.calls[-1][2] == {"disabled": True}


def test_fetch_usage_reads_money_as_decimal():
    ad, _ = make()
    info = ad.fetch_usage("h1")
    assert info.usage_usd == Decimal("4.25") and info.limit_remaining_usd == Decimal("5.75")


def test_list_keys_follows_pages():
    ad, _ = make()
    assert len(ad.list_keys()) == 103


def test_errors_never_contain_the_management_key():
    def boom(request):
        return httpx.Response(401, json={"error": {"message": "invalid credentials"}})
    ad = OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(OpenRouterError) as e:
        ad.fetch_usage("h1")
    assert e.value.status == 401 and MGMT not in str(e.value)


def test_network_failure_is_a_clean_error():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    ad = OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(OpenRouterError) as e:
        ad.issue_key("alice", 1)
    assert MGMT not in str(e.value)


def test_missing_secret_in_response_is_an_error():
    def odd(request):
        return httpx.Response(201, json={"data": key_json()})
    ad = OpenRouterAdapter(MGMT, client=httpx.Client(transport=httpx.MockTransport(odd)))
    with pytest.raises(OpenRouterError):
        ad.issue_key("alice", 1)


def test_empty_management_key_refused():
    with pytest.raises(ValueError):
        OpenRouterAdapter("")


def test_every_issue_limit_change_and_disable_is_audited_without_the_secret():
    ad, _ = make()
    log = []
    audited = AuditedOpenRouter(ad, lambda action, target, details: log.append((action, target, details)))
    issued = audited.issue_key("alice", 10)
    audited.set_limit("h1", 20)
    audited.disable_key("h1")
    audited.fetch_usage("h1")  # a read is not audited
    assert [a for a, _, _ in log] == ["key.issue", "key.set_limit", "key.revoke"]
    assert all(t == "h1" for _, t, _ in log)
    assert SECRET not in json.dumps(log, default=str) and issued.secret == SECRET
