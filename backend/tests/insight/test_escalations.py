from datetime import datetime, timedelta

import pytest

from app.insight import models as m
from app.insight.escalations import (EscalationService, InvalidState, PermissionDenied)
from app.insight.notifications import NotificationService
from app.insight.store import InMemoryStore

REASON = "ISSUE: a\nEVIDENCE: b\nPROPOSED ACTION: c\nIF IGNORED: d"
PROF1 = m.Actor("u-p1", m.ROLE_PROFESSOR, professor_id="prof-1")
PROF2 = m.Actor("u-p2", m.ROLE_PROFESSOR, professor_id="prof-2")
DEPT = m.Actor("u-d", m.ROLE_DEPARTMENT_ADMIN, department_id="dept-1")
STUDENT = m.Actor("u-s", m.ROLE_USER)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 18, 12, 0)

    def __call__(self):
        return self.t

    def advance(self, hours):
        self.t += timedelta(hours=hours)


def make(executors=None, can_act=None):
    clock, audit = Clock(), []
    kwargs = {"can_act": can_act} if can_act else {}
    svc = EscalationService(
        InMemoryStore(), executors=executors, now=clock, cooldown_hours=24,
        write_audit=lambda actor, action, ttype, tid, details=None: audit.append((actor, action, tid)),
        **kwargs)
    return svc, clock, audit


def pod_stop(svc, target="pod-1"):
    return svc.create("propose_pod_stop", target, REASON, professor_id="prof-1",
                      department_id="dept-1", team_id="team-alpha")


def test_pod_proposal_routes_to_the_professor_and_department_issue_to_the_department():
    svc, _, _ = make()
    e, created = pod_stop(svc)
    assert created and (e.recipient_type, e.recipient_id) == (m.PROFESSOR, "prof-1")
    d, _ = svc.create("escalate_to_department", "prof-1", REASON, professor_id="prof-1",
                      department_id="dept-1")
    assert (d.recipient_type, d.recipient_id) == (m.DEPARTMENT, "dept-1")


def test_approving_runs_the_action_and_is_audited():
    stopped = []
    svc, _, audit = make(executors={"propose_pod_stop": lambda e: stopped.append(e.target) or {"stopped": e.target}})
    e, _ = pod_stop(svc)
    svc.approve(e.id, PROF1)
    assert stopped == ["pod-1"]
    assert e.status == m.APPROVED and e.result == {"stopped": "pod-1"}
    assert ("u-p1", "escalation.approve", e.id) in audit


def test_nothing_runs_before_approval_and_rejecting_runs_nothing():
    stopped = []
    svc, _, audit = make(executors={"propose_pod_stop": lambda e: stopped.append(e.target) or {}})
    e, _ = pod_stop(svc)
    assert stopped == []
    svc.reject(e.id, PROF1, note="pod is needed for the demo")
    assert stopped == [] and e.status == m.REJECTED
    assert ("u-p1", "escalation.reject", e.id) in audit


def test_cooldown_suppresses_the_same_issue_but_not_other_issues():
    svc, clock, _ = make()
    first, created = pod_stop(svc)
    again, created_again = pod_stop(svc)
    assert created and not created_again and again is first
    other, created_other = pod_stop(svc, target="pod-2")
    assert created_other and other is not first
    svc.reject(first.id, PROF1)
    _, still_suppressed = pod_stop(svc)
    assert not still_suppressed                 # rejected, but inside the cooldown
    clock.advance(25)
    _, after_cooldown = pod_stop(svc)
    assert after_cooldown


def test_wrong_professor_student_and_department_cannot_decide_a_professor_escalation():
    svc, _, _ = make()
    e, _ = pod_stop(svc)
    for actor in (PROF2, STUDENT, DEPT):
        with pytest.raises(PermissionDenied):
            svc.approve(e.id, actor)
    assert e.status == m.PENDING


def test_permission_is_rechecked_at_approval_time():
    owns = {"prof-1": True}
    svc, _, _ = make(can_act=lambda actor, e: owns.get(actor.professor_id, False))
    e, _ = pod_stop(svc)
    owns["prof-1"] = False                      # the team was reassigned after the proposal
    with pytest.raises(PermissionDenied):
        svc.approve(e.id, PROF1)


def test_cannot_decide_twice():
    svc, _, _ = make()
    e, _ = pod_stop(svc)
    svc.approve(e.id, PROF1)
    with pytest.raises(InvalidState):
        svc.approve(e.id, PROF1)
    with pytest.raises(InvalidState):
        svc.reject(e.id, PROF1)


def test_failed_action_is_recorded_not_raised():
    def boom(e):
        raise RuntimeError("RunPod is down")
    svc, _, audit = make(executors={"propose_pod_stop": boom})
    e, _ = pod_stop(svc)
    svc.approve(e.id, PROF1)
    assert e.status == m.FAILED and "RunPod is down" in e.result["error"]
    assert ("u-p1", "escalation.fail", e.id) in audit


def test_department_escalation_is_acknowledged_by_default():
    svc, _, _ = make()
    e, _ = svc.create("escalate_to_department", "prof-1", REASON, professor_id="prof-1",
                      department_id="dept-1")
    svc.approve(e.id, DEPT)
    assert e.status == m.APPROVED and e.result == {"acknowledged": True}


def test_list_for_shows_only_what_the_actor_may_decide():
    svc, _, _ = make()
    pod_stop(svc)
    svc.create("escalate_to_department", "prof-1", REASON, professor_id="prof-1",
               department_id="dept-1")
    assert [e.kind for e in svc.list_for(PROF1)] == ["propose_pod_stop"]
    assert [e.kind for e in svc.list_for(DEPT)] == ["escalate_to_department"]
    assert svc.list_for(PROF2) == [] and svc.list_for(STUDENT) == []


def test_bad_input():
    svc, _, _ = make()
    with pytest.raises(ValueError):
        svc.create("raise_budget", "team-a", REASON, professor_id="prof-1")
    with pytest.raises(ValueError):
        svc.create("escalate_to_department", "prof-1", REASON, professor_id="prof-1")


def test_notification_cooldown_per_team():
    clock = Clock()
    svc = NotificationService(InMemoryStore(), now=clock, cooldown_hours=24)
    n1, c1 = svc.warn("team-a", "slow down")
    n2, c2 = svc.warn("team-a", "slow down again")
    _, c3 = svc.warn("team-b", "slow down")
    assert c1 and not c2 and n2 is n1 and c3
    clock.advance(25)
    assert svc.warn("team-a", "still fast")[1]
    assert len(svc.for_team("team-a")) == 2


def test_admin_who_is_also_a_professor_can_decide_both_kinds():
    """Only sub-admins (professors) open keys, so an admin who wants keys also holds the
    professor role. One person, both ids: they can decide both kinds of escalation."""
    from app.insight.escalations import default_can_act
    from app.insight.models import Actor, Escalation, PROFESSOR, DEPARTMENT
    from datetime import datetime
    both = Actor(user_id="u1", role="department_admin", professor_id="prof-1", department_id="dep-1")
    def esc(rt, rid):
        return Escalation(id=1, kind="x", target="t", reason="r", recipient_type=rt,
                          recipient_id=rid, created_at=datetime(2026, 11, 1))
    assert default_can_act(both, esc(PROFESSOR, "prof-1"))
    assert default_can_act(both, esc(DEPARTMENT, "dep-1"))
    assert not default_can_act(both, esc(PROFESSOR, "prof-2"))
    assert not default_can_act(both, esc(DEPARTMENT, "dep-2"))
