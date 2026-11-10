"""write_audit() and the append-only rule."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.audit import REDACTED, write_audit
from app.models import AuditLog, Provider, User
from tests.conftest import seed_id


def test_write_audit_records_json_safe_details(session):
    karim = session.get(User, seed_id("u_karim"))
    target = uuid.uuid4()
    entry = write_audit(
        session, karim, "key.set_limit", "issued_resource", target,
        {"limit_usd": Decimal("12.5000"), "provider": Provider.openrouter, "group_id": seed_id("fyp1")},
    )
    session.commit()

    saved = session.get(AuditLog, entry.id)
    assert saved.actor_id == karim.id
    assert saved.target_id == str(target)
    assert saved.details == {"limit_usd": "12.5000", "provider": "openrouter", "group_id": str(seed_id("fyp1"))}
    assert saved.at is not None


def test_write_audit_never_stores_secrets(session):
    entry = write_audit(
        session, None, "credential.create", "credential", "x",
        {"label": "Lab key", "api_key": "sk-or-v1-REAL", "nested": {"management_key": "mk", "secret": "s"}},
    )
    assert entry.details == {"label": "Lab key", "api_key": REDACTED, "nested": {"management_key": REDACTED, "secret": REDACTED}}


def test_system_jobs_have_no_actor(session):
    assert write_audit(session, None, "pod.stop", "issued_resource", "p").actor_id is None


@pytest.mark.parametrize("table", ["audit_log", "ledger_entries"])
@pytest.mark.parametrize("sql", ["UPDATE {t} SET created_by = NULL", "DELETE FROM {t}"])
def test_append_only_tables_reject_update_and_delete(session, table, sql):
    if table == "audit_log":
        sql = sql.replace("created_by", "actor_id")
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(sql.format(t=table)))
