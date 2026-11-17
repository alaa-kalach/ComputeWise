"""A simulated ComputeWise server for one scenario.

Same interface as the real backend will have for the agent:
`server.call(name, **args)`. Read tools answer from the scenario's data;
action tools only RECORD what the agent asked for in `server.actions`, so the
scorer can compare them with the scenario's expected answer.
"""
from __future__ import annotations

from app.agent.tools.anomalies import detect_spend_anomalies
from app.agent.tools.forecast import forecast_spend
from app.agent.tools_schema import FORBIDDEN_TOOLS
from evaluation.world.world import team_daily_spend, team_resources

IDLE_HOURS_THRESHOLD = 24
READ = ["get_budgets", "get_usage", "forecast_spend", "detect_spend_anomalies",
        "list_idle_pods", "get_professor_budget"]
ACTIONS = ["warn_user", "propose_pod_stop", "propose_key_disable",
           "propose_budget_change", "escalate_to_department"]


class SimServer:
    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.actions: list[dict] = []
        self.agent_results: list[dict] = []

    # ------------------------------------------------------------ dispatcher

    def call(self, name, **kwargs):
        if name in FORBIDDEN_TOOLS:
            self.actions.append({"tool": name, "target": next(iter(kwargs.values()), None)})
            return {"status": "failed", "detail": f"{name} is not allowed: a human decides."}
        if name not in READ + ACTIONS:
            return {"status": "failed", "detail": f"unknown tool {name}"}
        return getattr(self, name)(**kwargs)

    # ------------------------------------------------------------ helpers

    def _team(self, team_id):
        for t in self.scenario["teams"]:
            if t["id"] == team_id:
                return t
        return None

    def _unknown(self, what, value):
        return {"status": "failed", "detail": f"unknown {what}: {value}"}

    def _find(self, resource_id, rtype):
        for r in self.scenario["resources"]:
            if r["id"] == resource_id and r["type"] == rtype:
                return r
        return None

    # ------------------------------------------------------------ read tools

    def get_budgets(self, team_id):
        team = self._team(team_id)
        if not team:
            return self._unknown("team", team_id)
        p = self.scenario["period"]
        spent = round(sum(team_daily_spend(self.scenario, team_id)), 2)
        return {"status": "ok", "team_id": team_id, "name": team["name"],
                "balance_usd": team["balance_usd"], "spent_usd": spent,
                "remaining_usd": round(max(team["balance_usd"] - spent, 0), 2),
                "pct_used": round(spent / team["balance_usd"], 3),
                "period_day": p["day"], "period_days": p["days"]}

    def get_usage(self, team_id):
        if not self._team(team_id):
            return self._unknown("team", team_id)
        rows = []
        for r in team_resources(self.scenario, team_id):
            row = {"id": r["id"], "type": r["type"], "user": r["user"],
                   "spent_usd": round(sum(r["daily_spend"]), 2),
                   "last_7_days_usd": round(sum(r["daily_spend"][-7:]), 2)}
            if r["type"] == "pod":
                row.update(status=r["status"], hourly_usd=r["hourly_usd"],
                           idle_hours=r["idle_hours"])
            rows.append(row)
        total7 = sum(x["last_7_days_usd"] for x in rows) or 1
        drivers = sorted(rows, key=lambda x: x["last_7_days_usd"], reverse=True)[:3]
        return {"status": "ok", "team_id": team_id, "resources": rows,
                "top_drivers": [{"id": d["id"], "last_7_days_usd": d["last_7_days_usd"],
                                 "share_of_last_7_days": round(d["last_7_days_usd"] / total7, 2)}
                                for d in drivers]}

    def forecast_spend(self, team_id):
        team = self._team(team_id)
        if not team:
            return self._unknown("team", team_id)
        result = forecast_spend(team_daily_spend(self.scenario, team_id),
                                self.scenario["period"]["days"], team["balance_usd"])
        return {"status": "ok", "team_id": team_id, **result}

    def detect_spend_anomalies(self, team_id):
        if not self._team(team_id):
            return self._unknown("team", team_id)
        series = {r["id"]: r["daily_spend"] for r in team_resources(self.scenario, team_id)}
        return {"status": "ok", "team_id": team_id,
                "anomalies": detect_spend_anomalies(series)}

    def list_idle_pods(self, team_id):
        if not self._team(team_id):
            return self._unknown("team", team_id)
        idle = [{"id": r["id"], "user": r["user"], "hourly_usd": r["hourly_usd"],
                 "idle_hours": r["idle_hours"]}
                for r in team_resources(self.scenario, team_id)
                if r["type"] == "pod" and r["status"] == "running"
                and r["idle_hours"] >= IDLE_HOURS_THRESHOLD]
        return {"status": "ok", "team_id": team_id, "idle_pods": idle}

    def get_professor_budget(self, professor_id):
        prof = next((p for p in self.scenario.get("professors", [])
                     if p["id"] == professor_id), None)
        if not prof:
            return self._unknown("professor", professor_id)
        teams = [t for t in self.scenario["teams"] if t["professor_id"] == professor_id]
        days = self.scenario["period"]["days"]
        spent = projected = 0.0
        for t in teams:
            daily = team_daily_spend(self.scenario, t["id"])
            f = forecast_spend(daily, days, t["balance_usd"])
            spent += f["spent_usd"]
            projected += f["projected_total_usd"]   # demand, not capped at the balance
        ceiling = prof["grant_ceiling_usd"]
        return {"status": "ok", "professor_id": professor_id, "name": prof["name"],
                "grant_ceiling_usd": ceiling,
                # "department": billable, the department pays and bills the professor.
                # "own": observed only, the professor pays from his own account.
                "credential": prof.get("credential", "department"),
                "allocated_usd": round(sum(t["balance_usd"] for t in teams), 2),
                "spent_usd": round(spent, 2),
                "pct_of_ceiling_spent": round(spent / ceiling, 3),
                "projected_demand_usd": round(projected, 2),
                "will_exceed_ceiling": projected > ceiling,
                "team_ids": [t["id"] for t in teams]}

    # ------------------------------------------------------------ action tools

    def _record(self, tool, target, detail):
        self.actions.append({"tool": tool, "target": target})
        return {"status": "ok", "detail": detail}

    def warn_user(self, team_id, message):
        if not self._team(team_id):
            return self._unknown("team", team_id)
        return self._record("warn_user", team_id, "warning sent to the team")

    def propose_pod_stop(self, pod_id, reason):
        if not self._find(pod_id, "pod"):
            return self._unknown("pod", pod_id)
        return self._record("propose_pod_stop", pod_id, "proposal sent to the professor")

    def propose_key_disable(self, key_id, reason):
        if not self._find(key_id, "key"):
            return self._unknown("key", key_id)
        return self._record("propose_key_disable", key_id, "proposal sent to the professor")

    def propose_budget_change(self, from_team_id, to_team_id, amount_usd, reason):
        for t in (from_team_id, to_team_id):
            if not self._team(t):
                return self._unknown("team", t)
        return self._record("propose_budget_change", f"{from_team_id}->{to_team_id}",
                            "proposal sent to the professor")

    def escalate_to_department(self, professor_id, reason):
        known = {t["professor_id"] for t in self.scenario["teams"]}
        if professor_id not in known:
            return self._unknown("professor", professor_id)
        return self._record("escalate_to_department", professor_id,
                            "escalation sent to the department")
