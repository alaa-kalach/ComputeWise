"""Department reports (A7): spend per professor and per platform, and the
monthly billing report split into billable and observed spend.

Billable  = spend paid with a DEPARTMENT credential (charged back to the professor).
Observed  = spend paid with a PROFESSOR's own credential (monitored, not billed).
Unassigned = spend whose credential is missing or unknown (shown, never billed).

The functions work on plain spend entries, so they can be tested without a
database. When the ledger exists, a small adapter turns its `spend` rows into:

    {"period": "2026-11", "professor_id": "prof-1", "team_id": "team-alpha",
     "provider": "openrouter" | "runpod", "credential_id": "cred-1", "usd": 4.25}

`credentials` maps credential_id -> "department" | "professor" (who owns it).
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from app.insight import models as m
from app.insight.escalations import PermissionDenied

BILLABLE, OBSERVED, UNASSIGNED = "billable", "observed", "unassigned"
PROVIDERS = ("openrouter", "runpod")


def classify(entry: dict, credentials: dict) -> str:
    owner = credentials.get(entry.get("credential_id"))
    if owner == "department":
        return BILLABLE
    if owner == "professor":
        return OBSERVED
    return UNASSIGNED


def _empty_row(professor_id, name=""):
    return {"professor_id": professor_id, "professor_name": name,
            BILLABLE: 0.0, OBSERVED: 0.0, UNASSIGNED: 0.0, "total": 0.0,
            "openrouter": 0.0, "runpod": 0.0}


def _round_row(row):
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}


def spend_by_professor(entries, period: str, credentials: dict, professors: Optional[dict] = None):
    """One row per professor for one period, biggest spender first.
    `professors` maps professor_id -> name (optional, for display)."""
    names = professors or {}
    rows = defaultdict(lambda: None)
    for e in entries:
        if e["period"] != period:
            continue
        pid = e["professor_id"]
        if rows[pid] is None:
            rows[pid] = _empty_row(pid, names.get(pid, ""))
        row = rows[pid]
        usd = float(e["usd"])
        row[classify(e, credentials)] += usd
        row["total"] += usd
        if e["provider"] in PROVIDERS:
            row[e["provider"]] += usd
    for pid, name in names.items():              # professors with no spend still appear
        if rows[pid] is None:
            rows[pid] = _empty_row(pid, name)
    return sorted((_round_row(r) for r in rows.values()), key=lambda r: -r["total"])


def monthly_billing_report(entries, period: str, credentials: dict, professors: Optional[dict] = None):
    """The report the department downloads: one row per professor plus totals."""
    rows = spend_by_professor(entries, period, credentials, professors)
    totals = _empty_row("TOTAL", "")
    for r in rows:
        for k in (BILLABLE, OBSERVED, UNASSIGNED, "total", "openrouter", "runpod"):
            totals[k] += r[k]
    return {"period": period, "rows": rows, "totals": _round_row(totals)}


def monthly_totals(entries, credentials: dict):
    """Trend over time: one row per month, oldest first."""
    months = defaultdict(lambda: {BILLABLE: 0.0, OBSERVED: 0.0, UNASSIGNED: 0.0, "total": 0.0})
    for e in entries:
        usd = float(e["usd"])
        months[e["period"]][classify(e, credentials)] += usd
        months[e["period"]]["total"] += usd
    return [{"period": p, **{k: round(v, 2) for k, v in vals.items()}}
            for p, vals in sorted(months.items())]


CSV_COLUMNS = ["professor_id", "professor_name", "period", BILLABLE, OBSERVED,
               UNASSIGNED, "total", "openrouter", "runpod"]


def billing_report_csv(report: dict) -> str:
    """The billing report as CSV text, with a TOTAL row at the end."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in report["rows"] + [report["totals"]]:
        writer.writerow([report["period"] if c == "period" else r.get(c, "") for c in CSV_COLUMNS])
    return out.getvalue()


def _change_pct(this_week, last_week):
    return None if not last_week else round((this_week - last_week) / last_week * 100, 1)


def weekly_report(entries, week_start: date, credentials: dict,
                  professors: Optional[dict] = None, escalations=()):
    """Spend in the 7 days from week_start against the 7 days before, per professor and
    per team, with the billable/observed split and this week's escalations by status.
    Plain arithmetic, never done by the model. Entries without a date are ignored."""
    professors = professors or {}
    this_end, prev_start = week_start + timedelta(days=7), week_start - timedelta(days=7)
    prof = defaultdict(lambda: {"this": 0.0, "last": 0.0, "billable": 0.0, "observed": 0.0,
                                "teams": defaultdict(lambda: {"this": 0.0, "last": 0.0})})
    for en in entries:
        if not en.get("date"):
            continue
        day = date.fromisoformat(en["date"])
        which = "this" if week_start <= day < this_end else "last" if prev_start <= day < week_start else None
        if which is None:
            continue
        row = prof[en["professor_id"]]
        row[which] += en["usd"]
        row["teams"][en.get("team_id")][which] += en["usd"]
        if which == "this":
            kind = classify(en, credentials)
            if kind in ("billable", "observed"):
                row[kind] += en["usd"]
    rows = []
    for pid in sorted(set(prof) | set(professors)):
        r = prof[pid] if pid in prof else {"this": 0.0, "last": 0.0, "billable": 0.0,
                                         "observed": 0.0, "teams": {}}
        rows.append({
            "professor_id": pid, "professor_name": professors.get(pid, ""),
            "this_week_usd": round(r["this"], 2), "last_week_usd": round(r["last"], 2),
            "change_pct": _change_pct(r["this"], r["last"]),
            "billable_usd": round(r["billable"], 2), "observed_usd": round(r["observed"], 2),
            "teams": [{"team_id": t, "this_week_usd": round(v["this"], 2),
                       "last_week_usd": round(v["last"], 2),
                       "change_pct": _change_pct(v["this"], v["last"])}
                      for t, v in sorted(r["teams"].items(), key=lambda kv: str(kv[0]))],
        })
    counts = {m.PENDING: 0, m.APPROVED: 0, m.REJECTED: 0, m.FAILED: 0}
    for e in escalations:
        if week_start <= e.created_at.date() < this_end:
            counts[e.status] += 1
    this_total = round(sum(r["this_week_usd"] for r in rows), 2)
    last_total = round(sum(r["last_week_usd"] for r in rows), 2)
    return {"week_start": week_start.isoformat(), "rows": rows,
            "totals": {"this_week_usd": this_total, "last_week_usd": last_total,
                       "change_pct": _change_pct(this_total, last_total)},
            "escalations": counts}


class ReportService:
    """Role-aware access: the department sees everything, a professor sees only
    his own row, a student sees nothing."""

    def __init__(self, entries_source, credentials: dict, professors: Optional[dict] = None):
        self._entries = entries_source        # a function returning the spend entries
        self.credentials = credentials
        self.professors = professors or {}

    def billing_report(self, actor: m.Actor, period: str):
        report = monthly_billing_report(self._entries(), period, self.credentials, self.professors)
        if actor.role == m.ROLE_DEPARTMENT_ADMIN:
            return report
        if actor.role == m.ROLE_PROFESSOR:
            mine = [r for r in report["rows"] if r["professor_id"] == actor.professor_id]
            totals = dict(mine[0]) if mine else _empty_row(actor.professor_id)
            totals["professor_id"], totals["professor_name"] = "TOTAL", ""
            return {"period": period, "rows": mine, "totals": totals}
        raise PermissionDenied("only professors and department admins can see billing reports")

    def trend(self, actor: m.Actor):
        if actor.role != m.ROLE_DEPARTMENT_ADMIN:
            raise PermissionDenied("only the department can see spend trends")
        return monthly_totals(self._entries(), self.credentials)

    def weekly_report(self, actor: m.Actor, week_start: date, escalations=()):
        """Department: every professor. Professor: only his own row. `escalations` is the
        list this actor may see (EscalationService.list_for), so the counts are scoped too."""
        report = weekly_report(self._entries(), week_start, self.credentials,
                               self.professors, escalations)
        if actor.role == m.ROLE_DEPARTMENT_ADMIN:
            return report
        if actor.role == m.ROLE_PROFESSOR:
            mine = [r for r in report["rows"] if r["professor_id"] == actor.professor_id]
            report["rows"] = mine
            report["totals"] = {"this_week_usd": sum(r["this_week_usd"] for r in mine),
                                "last_week_usd": sum(r["last_week_usd"] for r in mine),
                                "change_pct": mine[0]["change_pct"] if mine else None}
            return report
        raise PermissionDenied("only professors and department admins can see weekly reports")
