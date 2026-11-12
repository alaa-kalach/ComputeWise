"""A3: credentials, encryption at rest, department grants with ceilings, one binding per
provider per professor, and the revoke hook.

The Week 2 "done when": a professor uses his own or a granted credential and the secret
never leaves the backend.
"""

from decimal import Decimal

import pytest
from sqlmodel import select

from app.access import credentials as creds_mod
from app.access.credentials import (
    CredentialRevoked,
    CredentialUnavailable,
    on_credential_revoked,
    resolve_credential,
)
from app.adapters import AdapterError, ProviderUnavailable
from app.core.crypto import decrypt
from app.models import (
    AuditLog,
    CredentialGrant,
    CredentialStatus,
    GrantStatus,
    ProfessorProviderBinding,
    Provider,
    ProviderCredential,
)
from tests.conftest import auth, seed_id

DEPT_OR, DEPT_RP, PROFB_OR = seed_id("cred_dept_or"), seed_id("cred_dept_rp"), seed_id("cred_profb_or")
PROF_A, PROF_B = seed_id("prof_a"), seed_id("prof_b")
SEED_SECRETS = (
    "sk-or-v1-FAKE-seed-dept-management-key",
    "rpa_FAKE_seed_dept_runpod_key",
    "sk-or-v1-FAKE-seed-khoury-management-key",
)
NEW_SECRET = "sk-or-v1-NEW-test-secret-that-must-never-come-back"


def no_secret_in(text: str, *secrets: str) -> bool:
    return all(s not in text for s in (*SEED_SECRETS, NEW_SECRET, *secrets)) and "secret" not in text


def audits(session, action: str) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def binding(session, professor_id, provider) -> ProfessorProviderBinding | None:
    return session.exec(
        select(ProfessorProviderBinding).where(
            ProfessorProviderBinding.professor_id == professor_id, ProfessorProviderBinding.provider == provider
        )
    ).first()


@pytest.fixture
def hook_calls():
    """Record revoke-hook calls for one test, then unregister."""
    calls: list[CredentialRevoked] = []

    def hook(session, actor, event):
        calls.append(event)

    on_credential_revoked(hook)
    yield calls
    creds_mod._REVOKE_HOOKS.remove(hook)


@pytest.fixture
def provider_says(monkeypatch):
    """provider_says(exc) makes the next secret check raise exc (None = accept)."""

    def set_(exc):
        class Fake:
            def fetch_usage(self, refs):
                if exc:
                    raise exc
                return {}

        monkeypatch.setattr(creds_mod, "get_adapter", lambda provider, secret: Fake())

    return set_


# ---------------------------------------------------------------- listing
def test_listing_is_scoped_and_never_contains_a_secret(client):
    r = client.get("/credentials", headers=auth("karim"))
    assert r.status_code == 200 and no_secret_in(r.text)
    body = {c["label"]: c for c in r.json()}
    assert set(body) == {"CS Department OpenRouter", "CS Department RunPod"}
    # A professor sees only his own grant of a department credential.
    assert [g["professor_id"] for g in body["CS Department RunPod"]["grants"]] == [str(PROF_A)]
    assert body["CS Department RunPod"]["grants"][0]["ceiling_usd"] == "300.0000"

    rania = {c["label"]: c for c in client.get("/credentials", headers=auth("rania")).json()}
    assert set(rania) == {"Khoury lab OpenRouter", "CS Department RunPod"}
    assert rania["Khoury lab OpenRouter"]["grants"] == []

    dana = client.get("/credentials", headers=auth("dana"))
    assert no_secret_in(dana.text)
    rp = next(c for c in dana.json() if c["id"] == str(DEPT_RP))
    assert {g["professor_name"] for g in rp["grants"]} == {"Prof. Karim Nassar", "Prof. Rania Khoury"}

    r = client.get("/credentials", headers=auth("sara"))
    assert r.status_code == 200 and r.json() == []


# ---------------------------------------------------------------- adding
def test_professor_adds_own_credential_encrypted_and_never_returned(client, session):
    r = client.post(
        "/credentials",
        headers=auth("karim"),
        json={"owner": "professor", "provider": "openrouter", "label": "Nassar lab", "secret": NEW_SECRET},
    )
    assert r.status_code == 201, r.text
    assert no_secret_in(r.text)
    out = r.json()
    assert out["owner_type"] == "professor" and out["owner_id"] == str(PROF_A) and out["status"] == "active"

    row = session.get(ProviderCredential, out["id"])
    assert row.secret_encrypted != NEW_SECRET and NEW_SECRET not in row.secret_encrypted
    assert decrypt(row.secret_encrypted) == NEW_SECRET

    (entry,) = [a for a in audits(session, "credential.add") if a.target_id == out["id"]]
    assert NEW_SECRET not in str(entry.details)

    # He already uses the department's OpenRouter key: adding his own doesn't switch it.
    assert binding(session, PROF_A, Provider.openrouter).credential_id == DEPT_OR


def test_first_credential_for_a_provider_is_bound_automatically(client, session):
    session.delete(binding(session, PROF_B, Provider.runpod))
    session.flush()
    r = client.post(
        "/credentials",
        headers=auth("rania"),
        json={"owner": "professor", "provider": "runpod", "label": "Khoury RunPod", "secret": "rpa_test"},
    )
    assert r.status_code == 201
    assert str(binding(session, PROF_B, Provider.runpod).credential_id) == r.json()["id"]
    assert audits(session, "binding.set")


def test_department_admin_adds_department_credential(client):
    r = client.post(
        "/credentials",
        headers=auth("dana"),
        json={"owner": "department", "provider": "runpod", "label": "CS RunPod 2", "secret": "rpa_dept_2"},
    )
    assert r.status_code == 201
    assert r.json()["owner_type"] == "department" and r.json()["owner_id"] == str(seed_id("dept"))


@pytest.mark.parametrize(
    "who, owner",
    [("karim", "department"), ("sara", "department"), ("sara", "professor"), ("dana", "professor")],
)
def test_wrong_role_cannot_add(client, who, owner):
    r = client.post(
        "/credentials",
        headers=auth(who),
        json={"owner": owner, "provider": "openrouter", "label": "x", "secret": "sk-x"},
    )
    assert r.status_code == 403


def test_rejected_key_is_422_and_not_echoed(client, session, provider_says):
    provider_says(AdapterError(f"OpenRouter 401: Invalid API key {NEW_SECRET}"))
    r = client.post(
        "/credentials",
        headers=auth("karim"),
        json={"owner": "professor", "provider": "openrouter", "label": "bad", "secret": NEW_SECRET},
    )
    assert r.status_code == 422 and no_secret_in(r.text)
    assert session.exec(select(ProviderCredential).where(ProviderCredential.label == "bad")).first() is None


def test_provider_down_is_503(client, provider_says):
    provider_says(ProviderUnavailable("timeout"))
    r = client.post(
        "/credentials",
        headers=auth("karim"),
        json={"owner": "professor", "provider": "openrouter", "label": "x", "secret": "sk-x"},
    )
    assert r.status_code == 503


def test_valid_key_check_passes_through_the_adapter(client, provider_says):
    provider_says(None)
    r = client.post(
        "/credentials",
        headers=auth("karim"),
        json={"owner": "professor", "provider": "runpod", "label": "ok", "secret": "rpa_ok"},
    )
    assert r.status_code == 201


# ---------------------------------------------------------------- revoking
def test_owner_revokes_own_credential(client, session, hook_calls):
    r = client.delete(f"/credentials/{PROFB_OR}", headers=auth("rania"))
    assert r.status_code == 204
    session.expire_all()
    assert session.get(ProviderCredential, PROFB_OR).status == CredentialStatus.revoked
    assert binding(session, PROF_B, Provider.openrouter) is None
    assert hook_calls == [CredentialRevoked(PROFB_OR, Provider.openrouter, None, "credential.revoke")]
    assert len(audits(session, "credential.revoke")) == 1

    # Revoking again is a no-op: no second audit, no second hook call.
    assert client.delete(f"/credentials/{PROFB_OR}", headers=auth("rania")).status_code == 204
    assert len(audits(session, "credential.revoke")) == 1 and len(hook_calls) == 1

    with pytest.raises(CredentialUnavailable):
        resolve_credential(session, PROF_B, Provider.openrouter)


def test_revoking_a_department_credential_cuts_off_every_grant(client, session, hook_calls):
    assert client.delete(f"/credentials/{DEPT_RP}", headers=auth("dana")).status_code == 204
    session.expire_all()
    grants = session.exec(select(CredentialGrant).where(CredentialGrant.credential_id == DEPT_RP)).all()
    assert grants and all(g.status == GrantStatus.revoked for g in grants)
    assert binding(session, PROF_A, Provider.runpod) is None
    assert binding(session, PROF_B, Provider.runpod) is None
    assert hook_calls == [CredentialRevoked(DEPT_RP, Provider.runpod, None, "credential.revoke")]


@pytest.mark.parametrize(
    "who, cred, code",
    [
        ("karim", DEPT_RP, 403),  # granted to him, not his
        ("rania", DEPT_RP, 403),
        ("dana", PROFB_OR, 403),  # the department sees a professor's own key but can't revoke it
        ("karim", PROFB_OR, 404),  # another professor's key is invisible
        ("sara", DEPT_RP, 404),
    ],
)
def test_only_the_owner_revokes(client, who, cred, code, hook_calls):
    assert client.delete(f"/credentials/{cred}", headers=auth(who)).status_code == code
    assert hook_calls == []


# ---------------------------------------------------------------- grants
def test_grant_lifecycle(client, session, hook_calls):
    # Rania has her own OpenRouter key; the department also grants hers, with a ceiling.
    r = client.post(f"/credentials/{DEPT_OR}/grants", headers=auth("dana"),
                    json={"professor_id": str(PROF_B), "ceiling_usd": "50"})
    assert r.status_code == 201, r.text
    grant = r.json()
    assert grant["ceiling_usd"] == "50.0000" and grant["professor_name"] == "Prof. Rania Khoury"
    # She keeps using her own key until she switches.
    assert binding(session, PROF_B, Provider.openrouter).credential_id == PROFB_OR

    again = client.post(f"/credentials/{DEPT_OR}/grants", headers=auth("dana"), json={"professor_id": str(PROF_B)})
    assert again.status_code == 409

    url = f"/credentials/{DEPT_OR}/grants/{grant['id']}"
    r = client.patch(url, headers=auth("dana"), json={"ceiling_usd": "75.5"})
    assert r.status_code == 200 and r.json()["ceiling_usd"] == "75.5000"
    r = client.patch(url, headers=auth("dana"), json={"ceiling_usd": None})
    assert r.status_code == 200 and r.json()["ceiling_usd"] is None
    assert len(audits(session, "grant.update")) == 2

    # She switches to it, then the department revokes the grant: she loses the binding.
    assert client.put("/bindings/openrouter", headers=auth("rania"),
                      json={"credential_id": str(DEPT_OR)}).json()["is_department"] is True
    assert client.delete(url, headers=auth("dana")).status_code == 204
    assert binding(session, PROF_B, Provider.openrouter) is None
    assert hook_calls == [CredentialRevoked(DEPT_OR, Provider.openrouter, PROF_B, "grant.revoke")]
    # ... and Karim, who holds his own grant of the same credential, is untouched.
    assert binding(session, PROF_A, Provider.openrouter).credential_id == DEPT_OR
    assert client.patch(url, headers=auth("dana"), json={"ceiling_usd": "1"}).status_code == 409


def test_grant_is_bound_when_professor_had_nothing(client, session):
    session.delete(binding(session, PROF_B, Provider.openrouter))
    session.flush()
    r = client.post(f"/credentials/{DEPT_OR}/grants", headers=auth("dana"), json={"professor_id": str(PROF_B)})
    assert r.status_code == 201 and r.json()["ceiling_usd"] is None
    assert binding(session, PROF_B, Provider.openrouter).credential_id == DEPT_OR


@pytest.mark.parametrize(
    "who, cred, code",
    [
        ("karim", DEPT_OR, 403),  # professors don't grant
        ("dana", PROFB_OR, 403),  # a professor's own key can't be granted
        ("sara", DEPT_OR, 404),
    ],
)
def test_only_department_admin_grants_department_credentials(client, who, cred, code):
    r = client.post(f"/credentials/{cred}/grants", headers=auth(who), json={"professor_id": str(PROF_B)})
    assert r.status_code == code


@pytest.mark.parametrize("bad", ["-1", "abc", 12.5, "1.23456"])
def test_ceiling_must_be_a_non_negative_money_string(client, bad):
    r = client.post(f"/credentials/{DEPT_OR}/grants", headers=auth("dana"),
                    json={"professor_id": str(PROF_B), "ceiling_usd": bad})
    assert r.status_code == 422


def test_cannot_grant_a_revoked_credential(client):
    client.delete(f"/credentials/{DEPT_OR}", headers=auth("dana"))
    r = client.post(f"/credentials/{DEPT_OR}/grants", headers=auth("dana"), json={"professor_id": str(PROF_B)})
    assert r.status_code == 409


def test_unknown_grant_is_404(client):
    r = client.patch(f"/credentials/{DEPT_OR}/grants/{seed_id('nope')}", headers=auth("dana"),
                     json={"ceiling_usd": "1"})
    assert r.status_code == 404


# ---------------------------------------------------------------- bindings
def test_bindings_list(client):
    r = client.get("/bindings", headers=auth("rania"))
    assert r.status_code == 200 and no_secret_in(r.text)
    assert {b["provider"]: b["is_department"] for b in r.json()} == {"openrouter": False, "runpod": True}


@pytest.mark.parametrize("who", ["dana", "sara"])
def test_only_professors_have_bindings(client, who):
    assert client.get("/bindings", headers=auth(who)).status_code == 403
    r = client.put("/bindings/runpod", headers=auth(who), json={"credential_id": str(DEPT_RP)})
    assert r.status_code == 403


def test_switching_to_own_credential(client, session):
    new = client.post(
        "/credentials",
        headers=auth("rania"),
        json={"owner": "professor", "provider": "runpod", "label": "Khoury RunPod", "secret": "rpa_k"},
    ).json()
    r = client.put("/bindings/runpod", headers=auth("rania"), json={"credential_id": new["id"]})
    assert r.status_code == 200
    assert r.json() == {"provider": "runpod", "credential_id": new["id"], "credential_label": "Khoury RunPod",
                        "is_department": False}
    (entry,) = [a for a in audits(session, "binding.set") if a.details.get("credential_id") == new["id"]]
    assert entry.details["previous_credential_id"] == str(DEPT_RP)

    # Same choice again: no new audit entry.
    client.put("/bindings/runpod", headers=auth("rania"), json={"credential_id": new["id"]})
    assert len([a for a in audits(session, "binding.set") if a.details.get("credential_id") == new["id"]]) == 1


def test_binding_rules(client):
    # Wrong provider.
    r = client.put("/bindings/runpod", headers=auth("rania"), json={"credential_id": str(PROFB_OR)})
    assert r.status_code == 422
    # Another professor's own key is invisible.
    r = client.put("/bindings/openrouter", headers=auth("karim"), json={"credential_id": str(PROFB_OR)})
    assert r.status_code == 404
    # A department key not granted to him: the department OpenRouter is not granted to Rania.
    r = client.put("/bindings/openrouter", headers=auth("rania"), json={"credential_id": str(DEPT_OR)})
    assert r.status_code == 404


def test_admin_who_is_also_professor_needs_a_grant_to_bind(client, session):
    """Roles combine (D21): Dana also becomes a professor. She sees the department's keys as
    admin, but uses one only once it is granted to her as a professor."""
    from app.models import Professor

    session.add(Professor(id=seed_id("prof_dana"), department_id=seed_id("dept"), user_id=seed_id("u_dana")))
    session.flush()
    r = client.put("/bindings/openrouter", headers=auth("dana"), json={"credential_id": str(DEPT_OR)})
    assert r.status_code == 403
    client.post(f"/credentials/{DEPT_OR}/grants", headers=auth("dana"),
                json={"professor_id": str(seed_id("prof_dana"))})
    r = client.get("/bindings", headers=auth("dana"))
    assert [b["credential_id"] for b in r.json()] == [str(DEPT_OR)]


# ---------------------------------------------------------------- resolve_credential (for B3, B4, sync)
def test_resolve_granted_department_credential(session):
    c = resolve_credential(session, PROF_A, Provider.runpod)
    assert c.credential_id == DEPT_RP and c.is_department is True
    assert c.grant_ceiling_usd == Decimal("300")
    assert c.secret == "rpa_FAKE_seed_dept_runpod_key"
    assert c.secret not in repr(c)


def test_resolve_own_credential(session):
    c = resolve_credential(session, PROF_B, Provider.openrouter)
    assert c.credential_id == PROFB_OR and c.is_department is False and c.grant_ceiling_usd is None


def test_resolve_fails_without_binding(session):
    session.delete(binding(session, PROF_A, Provider.openrouter))
    session.flush()
    with pytest.raises(CredentialUnavailable) as exc:
        resolve_credential(session, PROF_A, Provider.openrouter)
    assert exc.value.status_code == 409


def test_resolve_fails_when_grant_was_revoked_behind_the_binding(session):
    grant = session.exec(
        select(CredentialGrant).where(CredentialGrant.credential_id == DEPT_RP, CredentialGrant.professor_id == PROF_A)
    ).one()
    grant.status = GrantStatus.revoked
    session.add(grant)
    session.flush()
    with pytest.raises(CredentialUnavailable):
        resolve_credential(session, PROF_A, Provider.runpod)
