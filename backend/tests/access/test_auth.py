"""A2: Supabase tokens are verified and mapped to a ComputeWise user and role."""

import uuid
from datetime import timedelta

from sqlmodel import select

from app.models import AuditLog, User
from tests.conftest import EMAILS, auth, make_token, seed_id


def test_no_token_is_401(client):
    assert client.get("/me").status_code == 401


def test_garbage_token_is_401(client):
    assert client.get("/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_wrong_signature_is_401(client):
    token = make_token(EMAILS["karim"], secret="some-other-secret-that-is-long-enough-xx")
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_expired_token_is_401(client):
    token = make_token(EMAILS["karim"], expires_in=timedelta(minutes=-5))
    r = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["detail"] == "Token expired"


def test_token_from_another_project_is_401(client):
    token = make_token(EMAILS["karim"], issuer="https://someone-else.supabase.co/auth/v1")
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_wrong_audience_is_401(client):
    token = make_token(EMAILS["karim"], audience="anon")
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_anonymous_session_is_403(client):
    token = make_token(EMAILS["karim"], is_anonymous=True)
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_unknown_email_is_403(client):
    token = make_token("stranger@example.com")
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_unconfirmed_email_is_not_linked(client):
    token = make_token(EMAILS["karim"], user_metadata={"email_verified": False})
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_first_login_links_by_email_then_by_auth_id(client, session):
    sub = uuid.uuid4()
    r = client.get("/me", headers={"Authorization": f"Bearer {make_token(EMAILS['omar'], sub=sub)}"})
    assert r.status_code == 200
    assert session.get(User, seed_id("u_omar")).auth_id == sub
    assert session.exec(select(AuditLog).where(AuditLog.action == "user.link_login")).first() is not None

    # Later logins match on auth_id, even if the email on the login changed.
    r = client.get("/me", headers={"Authorization": f"Bearer {make_token('new-address@example.com', sub=sub)}"})
    assert r.status_code == 200 and r.json()["id"] == str(seed_id("u_omar"))


def test_a_linked_row_cannot_be_claimed_by_another_login(client):
    client.get("/me", headers={"Authorization": f"Bearer {make_token(EMAILS['lina'], sub=uuid.uuid4())}"})
    other = make_token(EMAILS["lina"], sub=uuid.uuid4())
    assert client.get("/me", headers={"Authorization": f"Bearer {other}"}).status_code == 403


def test_roles(client):
    assert client.get("/me", headers=auth("dana")).json()["role"] == "department_admin"

    karim = client.get("/me", headers=auth("karim")).json()
    assert karim["role"] == "professor"
    assert karim["professor_id"] == str(seed_id("prof_a"))
    assert karim["department_ids"] == [str(seed_id("dept"))]

    sara = client.get("/me", headers=auth("sara")).json()
    assert sara["role"] == "user"
    assert sorted(m["group_name"] for m in sara["memberships"]) == ["FYP Team 1", "Research Lab"]


def test_new_style_es256_token_is_verified_with_the_public_key(client, monkeypatch):
    """New Supabase projects sign with asymmetric keys, checked against the project's JWKS."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec

    import app.core.auth as auth_module
    from tests.conftest import TEST_SUPABASE_URL

    private_key = ec.generate_private_key(ec.SECP256R1())
    fake_jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private_key.public_key()))
    monkeypatch.setattr(auth_module, "_jwks_client", lambda url: fake_jwks)

    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"sub": str(uuid.uuid4()), "email": EMAILS["rania"], "aud": "authenticated",
              "iss": f"{TEST_SUPABASE_URL}/auth/v1", "iat": now, "exp": now + 3600}
    good = jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": "test"})
    r = client.get("/me", headers={"Authorization": f"Bearer {good}"})
    assert r.status_code == 200 and r.json()["role"] == "professor"

    forged = jwt.encode(claims, ec.generate_private_key(ec.SECP256R1()), algorithm="ES256", headers={"kid": "test"})
    assert client.get("/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
