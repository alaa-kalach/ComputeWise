from app.agent import gates


def tool(name, status="ok"):
    return {"kind": "tool", "tool": name, "status": status}


def test_finish_without_looking_is_nudged():
    assert gates.final_check([]) == gates.NUDGE
    assert gates.final_check([tool("get_budgets")]) == gates.NUDGE


def test_finish_after_budget_and_forecast_is_allowed():
    trace = [tool("get_budgets"), tool("forecast_spend")]
    assert gates.final_check(trace) is None


def test_reason_missing_lists_absent_lines():
    assert gates.reason_missing("") == gates.REASON_FIELDS
    full = "ISSUE: a\nEVIDENCE: b\nPROPOSED ACTION: c\nIF IGNORED: d"
    assert gates.reason_missing(full) == []
    assert gates.reason_missing("ISSUE: a\nEVIDENCE: b") == ["PROPOSED ACTION", "IF IGNORED"]


class FakeServer:
    def __init__(self, results=None):
        self.results, self.calls = list(results or []), []

    def call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return self.results.pop(0) if self.results else {"status": "ok"}


def test_format_guard_refuses_incomplete_proposal():
    server = FakeServer()
    guard = gates.FormatGuard(server)
    out = guard.call("propose_pod_stop", pod_id="pod-1", reason="too expensive")
    assert out["status"] == "failed" and "Missing" in out["detail"]
    assert server.calls == []  # never reached the server


def test_format_guard_passes_complete_proposal_and_other_tools():
    server = FakeServer()
    guard = gates.FormatGuard(server)
    reason = "ISSUE: x\nEVIDENCE: y\nPROPOSED ACTION: z\nIF IGNORED: w"
    assert guard.call("propose_pod_stop", pod_id="pod-1", reason=reason)["status"] == "ok"
    assert guard.call("get_budgets", team_id="t")["status"] == "ok"
    assert len(server.calls) == 2


def test_retry_guard_blocks_third_repeat_of_same_failure():
    server = FakeServer([{"status": "failed"}] * 5)
    guard = gates.RetryGuard(server, max_attempts=3)
    for _ in range(3):
        assert guard.call("get_usage", team_id="t")["status"] == "failed"
    blocked = guard.call("get_usage", team_id="t")
    assert "retry limit" in blocked["detail"]
    assert len(server.calls) == 3
    # different arguments are a different call
    assert guard.call("get_usage", team_id="other")["status"] == "failed"
    assert len(server.calls) == 4


def _t(tool, **result):
    return {"kind": "tool", "tool": tool, "result": {"status": "ok", **result}}


def test_overrun_without_action_is_nudged_once_it_looked():
    looked = [_t("get_budgets"), _t("forecast_spend", will_exceed=True)]
    assert gates.final_check(looked) == gates.ACT_NUDGE


def test_overrun_with_a_warning_or_proposal_may_finish():
    for act in ("warn_user", "propose_pod_stop", "propose_key_disable"):
        trace = [_t("get_budgets"), _t("forecast_spend", will_exceed=True), _t(act)]
        assert gates.final_check(trace) is None


def test_no_overrun_may_finish_without_action():
    trace = [_t("get_budgets"), _t("forecast_spend", will_exceed=False)]
    assert gates.final_check(trace) is None


class _FakeServer:
    def __init__(self, anomalies=(), idle=()):
        self.anomalies, self.idle, self.sent = list(anomalies), list(idle), []

    def call(self, name, **kw):
        if name == "detect_spend_anomalies":
            return {"status": "ok", "anomalies": [{"resource_id": k} for k in self.anomalies]}
        if name == "list_idle_pods":
            return {"status": "ok", "idle_pods": [{"id": p} for p in self.idle]}
        self.sent.append((name, kw))
        return {"status": "ok"}


def test_key_disable_refused_without_an_anomaly_flag():
    g = gates.EvidenceGuard(_FakeServer(anomalies=[]))
    g.call("detect_spend_anomalies", team_id="t")
    r = g.call("propose_key_disable", key_id="key-1", reason="x")
    assert r["status"] == "failed" and "warn_user" in r["detail"]
    assert g._server.sent == []


def test_key_disable_allowed_for_the_flagged_key_only():
    g = gates.EvidenceGuard(_FakeServer(anomalies=["key-2"]))
    g.call("detect_spend_anomalies", team_id="t")
    assert g.call("propose_key_disable", key_id="key-2", reason="x")["status"] == "ok"
    assert g.call("propose_key_disable", key_id="key-1", reason="x")["status"] == "failed"


def test_pod_stop_needs_the_pod_to_be_listed_idle():
    g = gates.EvidenceGuard(_FakeServer(idle=["pod-1"]))
    assert g.call("propose_pod_stop", pod_id="pod-1", reason="x")["status"] == "failed"  # never looked
    g.call("list_idle_pods", team_id="t")
    assert g.call("propose_pod_stop", pod_id="pod-1", reason="x")["status"] == "ok"
    assert g.call("propose_pod_stop", pod_id="pod-2", reason="x")["status"] == "failed"


# ---- department run: unusual spend on a department credential ----

def _budget(credential, exceed=False):
    return _t("get_professor_budget", credential=credential, will_exceed_ceiling=exceed)


def test_department_credential_must_check_for_unusual_spend_before_finishing():
    assert gates.department_final_check([_budget("department")]) == gates.DEPT_ANOMALY_NUDGE
    trace = [_budget("department"), _t("detect_spend_anomalies", anomalies=[])]
    assert gates.department_final_check(trace) is None


def test_own_credential_needs_no_anomaly_check():
    assert gates.department_final_check([_budget("own")]) is None


class _DeptServer:
    def __init__(self, credential="department", exceed=False, anomalies=()):
        self.credential, self.exceed, self.anomalies, self.sent = credential, exceed, list(anomalies), []

    def call(self, name, **kw):
        if name == "get_professor_budget":
            return {"status": "ok", "credential": self.credential, "will_exceed_ceiling": self.exceed}
        if name == "detect_spend_anomalies":
            return {"status": "ok", "anomalies": [{"resource_id": k} for k in self.anomalies]}
        self.sent.append(name)
        return {"status": "ok"}


def _escalate(g):
    return g.call("escalate_to_department", professor_id="prof-1", reason="x")["status"]


def test_escalation_allowed_for_ceiling_overrun_on_department_credential():
    g = gates.EvidenceGuard(_DeptServer(exceed=True))
    g.call("get_professor_budget", professor_id="prof-1")
    assert _escalate(g) == "ok"


def test_escalation_allowed_for_unusual_spend_on_department_credential():
    g = gates.EvidenceGuard(_DeptServer(anomalies=["key-1"]))
    g.call("get_professor_budget", professor_id="prof-1")
    g.call("detect_spend_anomalies", team_id="t")
    assert _escalate(g) == "ok"


def test_escalation_refused_without_evidence_or_on_own_credential():
    quiet = gates.EvidenceGuard(_DeptServer())
    quiet.call("get_professor_budget", professor_id="prof-1")
    quiet.call("detect_spend_anomalies", team_id="t")
    assert _escalate(quiet) == "failed"
    own = gates.EvidenceGuard(_DeptServer(credential="own", exceed=True, anomalies=["key-1"]))
    own.call("get_professor_budget", professor_id="prof-1")
    own.call("detect_spend_anomalies", team_id="t")
    assert _escalate(own) == "failed"
    never_looked = gates.EvidenceGuard(_DeptServer(exceed=True))
    assert _escalate(never_looked) == "failed"


# ---- a warning must say what drove the spend ----

class _UsageServer:
    def __init__(self, top=("key-1",)):
        self.top, self.sent = list(top), []

    def call(self, name, **kw):
        if name == "get_usage":
            return {"status": "ok", "top_drivers": [{"id": d} for d in self.top]}
        self.sent.append((name, kw))
        return {"status": "ok"}


def test_warning_refused_before_get_usage():
    g = gates.EvidenceGuard(_UsageServer())
    r = g.call("warn_user", team_id="t", message="You will run out in 3 days.")
    assert r["status"] == "failed" and "get_usage" in r["detail"]
    assert g._server.sent == []


def test_warning_must_name_a_top_driver():
    g = gates.EvidenceGuard(_UsageServer(top=["key-1", "pod-2"]))
    g.call("get_usage", team_id="t")
    bad = g.call("warn_user", team_id="t", message="You will run out in 3 days.")
    assert bad["status"] == "failed" and "key-1" in bad["detail"] and "pod-2" in bad["detail"]
    assert g.call("warn_user", team_id="t", message="key-1 spent $30 this week; budget runs out in 3 days.")["status"] == "ok"


def test_a_driver_id_must_match_as_a_whole_word():
    g = gates.EvidenceGuard(_UsageServer(top=["key-1"]))
    g.call("get_usage", team_id="t")
    assert g.call("warn_user", team_id="t", message="key-10 is spending a lot")["status"] == "failed"
    assert g.call("warn_user", team_id="t", message="Key-1 is spending a lot")["status"] == "ok"


def test_a_team_with_no_resources_may_still_be_warned():
    g = gates.EvidenceGuard(_UsageServer(top=[]))
    g.call("get_usage", team_id="t")
    assert g.call("warn_user", team_id="t", message="You will run out in 3 days.")["status"] == "ok"
