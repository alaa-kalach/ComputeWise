"""Gates around the agent loop. Nothing here talks to the model.

1. final_check(trace)   Evidence gate. When the model wants to stop, check it
   actually looked at the budget and the forecast first. At most MAX_NUDGES.
2. FormatGuard(server)  Proposals and escalations must carry the four lines
   ISSUE / EVIDENCE / PROPOSED ACTION / IF IGNORED, or they are refused with
   a message saying what is missing.
3. EvidenceGuard(server) A key may be proposed for disabling only if detect_spend_anomalies
   flagged it, and a pod for stopping only if list_idle_pods listed it. Without
   that evidence the proposal is refused and the agent is told to warn instead.
4. RetryGuard(server)   The same call with the same arguments may fail
   MAX_ATTEMPTS times, then it is refused (the agent must change approach).
"""
from __future__ import annotations

import json
import re

MAX_NUDGES = 2
MAX_ATTEMPTS = 3

REQUIRED_BEFORE_FINISH = ("get_budgets", "forecast_spend")
REQUIRED_BEFORE_FINISH_DEPARTMENT = ("get_professor_budget",)
PROPOSAL_TOOLS = {"propose_pod_stop", "propose_key_disable",
                  "propose_budget_change", "escalate_to_department"}
REASON_FIELDS = ["ISSUE", "EVIDENCE", "PROPOSED ACTION", "IF IGNORED"]

NUDGE = ("Before you finish: you have not looked at the data yet. Call `get_budgets` and "
         "`forecast_spend` for the team, check the other read tools that matter, and only "
         "then decide. Doing nothing is a valid decision, but only after you looked.")


def make_final_check(required, nudge):
    def check(trace):
        used = {t.get("tool") for t in trace if t.get("kind") == "tool"}
        return None if all(name in used for name in required) else nudge
    return check


ACT_NUDGE = ("Before you finish: your forecast says this team will run out of money before the "
             "period ends, but you have not warned anyone or proposed anything. Either act "
             "now, or say in your summary exactly why no action is justified. Use "
             "`warn_user` unless the tools showed a faulty pod (`list_idle_pods`) or a key with "
             "a sudden jump (`detect_spend_anomalies`); only then propose stopping or "
             "disabling it.")
ACTION_TOOLS = PROPOSAL_TOOLS | {"warn_user"}


def _forecast_says_overrun(trace):
    return any(t.get("kind") == "tool" and t.get("tool") == "forecast_spend"
               and isinstance(t.get("result"), dict) and t["result"].get("will_exceed") is True
               for t in trace)


_looked = make_final_check(REQUIRED_BEFORE_FINISH, NUDGE)


def final_check(trace):
    """Evidence gate for a team review: look first; and if the forecast shows an
    overrun, do not finish without acting (or saying why not)."""
    nudge = _looked(trace)
    if nudge:
        return nudge
    used = {t.get("tool") for t in trace if t.get("kind") == "tool"}
    if _forecast_says_overrun(trace) and not (used & ACTION_TOOLS):
        return ACT_NUDGE
    return None


_dept_looked = make_final_check(
    REQUIRED_BEFORE_FINISH_DEPARTMENT,
    "Before you finish: you have not looked at the professor's budget yet. Call "
    "`get_professor_budget` first, and only then decide. Doing nothing is a valid "
    "decision, but only after you looked.")
DEPT_ANOMALY_NUDGE = ("Before you finish: this professor uses a department credential, so the "
                      "department pays. Call `detect_spend_anomalies` for each of his teams to "
                      "check for unusual spend, and only then decide.")


def _professor_credential(trace):
    for t in trace:
        if t.get("kind") == "tool" and t.get("tool") == "get_professor_budget":
            r = t.get("result")
            if isinstance(r, dict) and r.get("status") == "ok":
                return r.get("credential")
    return None


def department_final_check(trace):
    """Look first; and for a department credential, also check for unusual spend."""
    nudge = _dept_looked(trace)
    if nudge:
        return nudge
    used = {t.get("tool") for t in trace if t.get("kind") == "tool"}
    if _professor_credential(trace) == "department" and "detect_spend_anomalies" not in used:
        return DEPT_ANOMALY_NUDGE
    return None


def reason_missing(reason):
    """Return the required lines missing from a proposal's reason."""
    text = (reason or "").upper()
    return [f for f in REASON_FIELDS if f + ":" not in text]


class FormatGuard:
    """Wraps the server. Refuses proposals whose reason lacks the four lines."""

    def __init__(self, server):
        self._server = server

    def __getattr__(self, name):
        return getattr(self._server, name)

    def call(self, name, **kwargs):
        if name in PROPOSAL_TOOLS:
            missing = reason_missing(kwargs.get("reason"))
            if missing:
                return {"status": "failed",
                        "detail": "reason is incomplete. Write it as four lines, each starting "
                                  "with its label: ISSUE:, EVIDENCE:, PROPOSED ACTION:, "
                                  f"IF IGNORED:. Missing: {', '.join(missing)}."}
        return self._server.call(name, **kwargs)


class RetryGuard:
    """Wraps the server. Everything except `call` passes straight through."""

    def __init__(self, server, max_attempts=MAX_ATTEMPTS):
        self._server = server
        self._max = max_attempts
        self._failures = {}

    def __getattr__(self, name):
        return getattr(self._server, name)

    def call(self, name, **kwargs):
        key = (name, json.dumps(kwargs, sort_keys=True, default=str))
        if self._failures.get(key, 0) >= self._max:
            return {"status": "failed",
                    "detail": f"retry limit reached: {name} with these arguments already failed "
                              f"{self._max} times. Do not repeat it. Take a different approach "
                              f"or finish."}
        result = self._server.call(name, **kwargs)
        if isinstance(result, dict) and result.get("status") == "failed":
            self._failures[key] = self._failures.get(key, 0) + 1
        return result


def mentions_resource(message, resource_id):
    """True when the text names the resource id as a whole word (key-1 is not key-10)."""
    pattern = r"(?<![\w-])" + re.escape(resource_id) + r"(?![\w-])"
    return re.search(pattern, message or "", re.IGNORECASE) is not None


class EvidenceGuard:
    """Wraps the server. Proposals must be backed by what the tools found, and a warning
    must say what drove the spend."""

    def __init__(self, server):
        self._server = server
        self._flagged_keys = set()
        self._idle_pods = set()
        self._ceiling_exceeded = False
        self._dept_credential = False
        self._dept_anomaly = False
        self._top_drivers = None   # None = get_usage not called yet

    def __getattr__(self, name):
        return getattr(self._server, name)

    def call(self, name, **kwargs):
        if name == "warn_user":
            if self._top_drivers is None:
                return {"status": "failed",
                        "detail": "a warning must say what drove the spend. Call `get_usage` "
                                  "first, then name the top drivers in the warning."}
            if self._top_drivers and not any(
                    mentions_resource(kwargs.get("message"), d) for d in self._top_drivers):
                return {"status": "failed",
                        "detail": "the warning does not say what drove the spend. Name at least "
                                  "one of the top drivers and what it spent: "
                                  f"{', '.join(sorted(self._top_drivers))}."}
        if name == "propose_key_disable" and kwargs.get("key_id") not in self._flagged_keys:
            return {"status": "failed",
                    "detail": "no evidence: `detect_spend_anomalies` did not flag this key, so "
                              "its spend is normal use and you may not propose disabling it. "
                              "If the team still spends too fast, use `warn_user` instead."}
        if name == "propose_pod_stop" and kwargs.get("pod_id") not in self._idle_pods:
            return {"status": "failed",
                    "detail": "no evidence: `list_idle_pods` did not list this pod as idle, so "
                              "you may not propose stopping it. If the team still spends too "
                              "fast, use `warn_user` instead."}
        if name == "escalate_to_department" and not (
                self._dept_credential and (self._ceiling_exceeded or self._dept_anomaly)):
            return {"status": "failed",
                    "detail": "no evidence: escalate only when the professor uses a department "
                              "credential AND either `get_professor_budget` shows he will exceed "
                              "his ceiling or `detect_spend_anomalies` flagged a team's key. "
                              "If neither is true, finish without escalating."}
        result = self._server.call(name, **kwargs)
        if isinstance(result, dict) and result.get("status") == "ok":
            if name == "get_professor_budget":
                self._dept_credential = result.get("credential") == "department"
                self._ceiling_exceeded = bool(result.get("will_exceed_ceiling"))
            if name == "detect_spend_anomalies":
                flagged = {a.get("resource_id") for a in result.get("anomalies", [])}
                self._flagged_keys |= flagged
                self._dept_anomaly = self._dept_anomaly or bool(flagged)
            elif name == "list_idle_pods":
                self._idle_pods |= {p.get("id") for p in result.get("idle_pods", [])}
            if name == "get_usage":
                self._top_drivers = {d.get("id") for d in result.get("top_drivers", [])}
        return result
