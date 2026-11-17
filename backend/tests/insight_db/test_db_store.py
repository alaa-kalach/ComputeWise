"""A6 on the real schema: the escalation and notification services running on DbStore."""
import uuid

import pytest
from sqlmodel import text

from app.insight import models as m
from app.insight.db_store import DbStore
from app.insight.escalations import EscalationService, InvalidState, PermissionDenied
from app.insight.notifications import NotificationService


@pytest.fixture
def world(session):
    dep, user, prof, group = (uuid.uuid4() for _ in range(4))
    session.exec(text(f"insert into departments(id,name) values ('{dep}','D-{dep}')"))
    session.exec(text(f"insert into users(id,name,email) values ('{user}','Prof','{user}@x.c')"))
    session.exec(text(f"insert into professors(id,department_id,user_id) values ('{prof}','{dep}','{user}')"))
    session.exec(text(f"insert into groups(id,professor_id,name) values ('{group}','{prof}','Team A')"))
    session.flush()
    return dict(dep=str(dep), user=str(user), prof=str(prof), group=str(group))


def make(session, executors=None):
    audit = []
    svc = EscalationService(DbStore(session), executors=executors or {},
                            write_audit=lambda *a: audit.append(a))
    return svc, audit


def test_create_respects_cooldown_and_scopes_the_list(session, world):
    svc, _ = make(session)
    e1, created = svc.create("propose_pod_stop", "pod-1", "ISSUE", world["prof"], team_id=world["group"])
    e2, created2 = svc.create("propose_pod_stop", "pod-1", "ISSUE", world["prof"], team_id=world["group"])
    assert created and not created2 and e1.id == e2.id
    owner = m.Actor(user_id=world["user"], role=m.ROLE_PROFESSOR, professor_id=world["prof"])
    stranger = m.Actor(user_id=str(uuid.uuid4()), role=m.ROLE_PROFESSOR, professor_id=str(uuid.uuid4()))
    assert [e.id for e in svc.list_for(owner)] == [e1.id]
    assert svc.list_for(stranger) == []


def test_approve_runs_executor_and_persists(session, world):
    svc, audit = make(session, {"propose_pod_stop": lambda e: {"stopped": e.target}})
    e, _ = svc.create("propose_pod_stop", "pod-1", "ISSUE", world["prof"], team_id=world["group"])
    owner = m.Actor(user_id=world["user"], role=m.ROLE_PROFESSOR, professor_id=world["prof"])
    svc.approve(e.id, owner)
    saved = DbStore(session).get_escalation(e.id)
    assert (saved.status, saved.decided_by, saved.result) == ("approved", world["user"], {"stopped": "pod-1"})
    assert any(a[1] == "escalation.approve" for a in audit)
    with pytest.raises(InvalidState):
        svc.approve(e.id, owner)


def test_stranger_cannot_decide(session, world):
    svc, _ = make(session)
    e, _ = svc.create("propose_pod_stop", "pod-1", "ISSUE", world["prof"])
    stranger = m.Actor(user_id=str(uuid.uuid4()), role=m.ROLE_PROFESSOR, professor_id=str(uuid.uuid4()))
    with pytest.raises(PermissionDenied):
        svc.approve(e.id, stranger)


def test_reject_and_failed_executor_are_saved(session, world):
    def boom(e):
        raise RuntimeError("provider down")
    svc, _ = make(session, {"propose_key_disable": boom})
    owner = m.Actor(user_id=world["user"], role=m.ROLE_PROFESSOR, professor_id=world["prof"])
    a, _ = svc.create("propose_key_disable", "key-1", "ISSUE", world["prof"])
    assert svc.approve(a.id, owner).status == "failed"
    assert DbStore(session).get_escalation(a.id).result == {"error": "provider down"}
    b, _ = svc.create("propose_pod_stop", "pod-9", "ISSUE", world["prof"])
    svc.reject(b.id, owner, note="not now")
    saved = DbStore(session).get_escalation(b.id)
    assert (saved.status, saved.note) == ("rejected", "not now")


def test_notification_cooldown(session, world):
    n = NotificationService(DbStore(session))
    _, c1 = n.warn(world["group"], "80% used")
    _, c2 = n.warn(world["group"], "again")
    assert c1 and not c2 and len(n.for_team(world["group"])) == 1
