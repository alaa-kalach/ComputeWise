"""Seed data for development, tests and demos.

Run from backend/:
    python -m scripts.seed           # seeds an empty database, skips if already seeded
    python -m scripts.seed --reset   # wipes every table first (TRUNCATE), then seeds

The shape follows the design document's own diagram:

    Computer Science (admin: Dana Haddad)
    |- Prof. Karim Nassar (A)  budget ceiling $400/month, uses DEPARTMENT credentials
    |    |- FYP Team 1   $50/month, rollover cap $100   Sara*, Omar
    |    `- FYP Team 2   $50/month, rollover cap $100   Lina, Jad
    `- Prof. Rania Khoury (B)  tracking only (no ceiling), OWN OpenRouter, granted dept RunPod
         `- Research Lab $150/month, no rollover         Sara*, Maya

    * Sara is in groups under both professors, with a separate budget under each.

What the ledger shows (amounts: + allocation/rollover/top-up, - spend):
- Last month FYP Team 1 spent nothing, so this month it has a $50 rollover: $100 total.
- FYP Team 2 spent all $50 last month (no rollover) and is past 80% this month.
- Spend is split by funding source: department credentials = billable,
  Prof. B's own OpenRouter credential = observed.

IDs are deterministic (uuid5 of a name), so mocks and tests can refer to them.
Use `seed_id("prof_a")`, `seed_id("fyp1")`, and so on; the names are listed in IDS below.
"""

import argparse
import hashlib
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from sqlalchemy import select, text
from sqlmodel import Session, SQLModel

from app.core.crypto import encrypt
from app.core.db import get_engine
from app.models import (
    AccessRequest,
    AuditLog,
    BudgetPolicy,
    CredentialGrant,
    Department,
    DepartmentAdmin,
    Group,
    GroupMember,
    IssuedResource,
    LedgerEntry,
    LedgerType,
    OwnerType,
    Professor,
    ProfessorProviderBinding,
    Provider,
    ProviderCredential,
    RequestStatus,
    ResourceStatus,
    ScopeType,
    UsageSnapshot,
    User,
)

_NAMESPACE = uuid.UUID("6f1c2a52-7d1e-4c7b-9a51-0c0ffee5eed0")


def seed_id(name: str) -> uuid.UUID:
    """Stable id for a seed object, e.g. seed_id('prof_a')."""
    return uuid.uuid5(_NAMESPACE, name)


IDS = [
    "dept",
    "u_dana", "u_karim", "u_rania", "u_sara", "u_omar", "u_lina", "u_jad", "u_maya",
    "prof_a", "prof_b",
    "fyp1", "fyp2", "research_lab",
    "cred_dept_or", "cred_dept_rp", "cred_profb_or",
    "res_sara_fyp1_key", "res_sara_lab_key", "res_omar_pod", "res_lina_pod", "res_maya_pod",
]
I = {name: seed_id(name) for name in IDS}


def _periods(today: date) -> tuple[str, str]:
    first = today.replace(day=1)
    prev = first - timedelta(days=1)
    return first.strftime("%Y-%m"), prev.strftime("%Y-%m")


def build(now: datetime) -> list[SQLModel]:
    cur, prev = _periods(now.date())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = month_start - timedelta(days=15)
    rows: list[SQLModel] = []
    add = rows.append

    # ---------------------------------------------------------------- org and roles
    add(Department(id=I["dept"], name="Computer Science"))
    people = {
        "u_dana": ("Dana Haddad", "dana.haddad@cs.example.edu"),
        "u_karim": ("Prof. Karim Nassar", "karim.nassar@cs.example.edu"),
        "u_rania": ("Prof. Rania Khoury", "rania.khoury@cs.example.edu"),
        "u_sara": ("Sara Mansour", "sara.mansour@students.example.edu"),
        "u_omar": ("Omar Itani", "omar.itani@students.example.edu"),
        "u_lina": ("Lina Saleh", "lina.saleh@students.example.edu"),
        "u_jad": ("Jad Fares", "jad.fares@students.example.edu"),
        "u_maya": ("Maya Aoun", "maya.aoun@students.example.edu"),
    }
    for key, (name, email) in people.items():
        add(User(id=I[key], name=name, email=email))

    add(DepartmentAdmin(department_id=I["dept"], user_id=I["u_dana"]))
    add(Professor(id=I["prof_a"], department_id=I["dept"], user_id=I["u_karim"]))
    add(Professor(id=I["prof_b"], department_id=I["dept"], user_id=I["u_rania"]))

    add(Group(id=I["fyp1"], professor_id=I["prof_a"], name="FYP Team 1"))
    add(Group(id=I["fyp2"], professor_id=I["prof_a"], name="FYP Team 2"))
    add(Group(id=I["research_lab"], professor_id=I["prof_b"], name="Research Lab"))

    for group, user in [
        ("fyp1", "u_sara"), ("fyp1", "u_omar"),
        ("fyp2", "u_lina"), ("fyp2", "u_jad"),
        ("research_lab", "u_sara"), ("research_lab", "u_maya"),  # Sara: under both professors
    ]:
        add(GroupMember(group_id=I[group], user_id=I[user]))

    # ---------------------------------------------------------------- credentials
    # Fake secrets, encrypted like real ones. Never use real keys in seed data.
    add(ProviderCredential(
        id=I["cred_dept_or"], owner_type=OwnerType.department, owner_id=I["dept"],
        provider=Provider.openrouter, secret_encrypted=encrypt("sk-or-v1-FAKE-seed-dept-management-key"),
        label="CS Department OpenRouter", created_by=I["u_dana"],
    ))
    add(ProviderCredential(
        id=I["cred_dept_rp"], owner_type=OwnerType.department, owner_id=I["dept"],
        provider=Provider.runpod, secret_encrypted=encrypt("rpa_FAKE_seed_dept_runpod_key"),
        label="CS Department RunPod", created_by=I["u_dana"],
    ))
    add(ProviderCredential(
        id=I["cred_profb_or"], owner_type=OwnerType.professor, owner_id=I["prof_b"],
        provider=Provider.openrouter, secret_encrypted=encrypt("sk-or-v1-FAKE-seed-khoury-management-key"),
        label="Khoury lab OpenRouter", created_by=I["u_rania"],
    ))

    # Grant ceilings (department -> professor). Separate from budget policies.
    add(CredentialGrant(credential_id=I["cred_dept_or"], professor_id=I["prof_a"], ceiling_usd=D("150")))
    add(CredentialGrant(credential_id=I["cred_dept_rp"], professor_id=I["prof_a"], ceiling_usd=D("300")))
    add(CredentialGrant(credential_id=I["cred_dept_rp"], professor_id=I["prof_b"], ceiling_usd=D("200")))

    # One active credential per provider per professor.
    add(ProfessorProviderBinding(professor_id=I["prof_a"], provider=Provider.openrouter, credential_id=I["cred_dept_or"]))
    add(ProfessorProviderBinding(professor_id=I["prof_a"], provider=Provider.runpod, credential_id=I["cred_dept_rp"]))
    add(ProfessorProviderBinding(professor_id=I["prof_b"], provider=Provider.openrouter, credential_id=I["cred_profb_or"]))
    add(ProfessorProviderBinding(professor_id=I["prof_b"], provider=Provider.runpod, credential_id=I["cred_dept_rp"]))

    # ---------------------------------------------------------------- budget policies
    def policy(scope: ScopeType, scope_key: str, amount: str | None, *, group: str | None = None,
               rollover: bool = True, cap: str | None = None) -> BudgetPolicy:
        return BudgetPolicy(
            scope_type=scope, scope_id=I[scope_key], group_id=I[group] if group else None,
            monthly_amount=D(amount) if amount is not None else None,
            rollover=rollover, rollover_cap=D(cap) if cap is not None else None,
        )

    add(policy(ScopeType.professor, "prof_a", "400", cap="800"))       # budget ceiling
    add(policy(ScopeType.professor, "prof_b", None))                    # tracking only
    add(policy(ScopeType.group, "fyp1", "50", group="fyp1", cap="100"))
    add(policy(ScopeType.group, "fyp2", "50", group="fyp2", cap="100"))
    add(policy(ScopeType.group, "research_lab", "150", group="research_lab", rollover=False))
    # Sara has a separate budget under each professor.
    add(policy(ScopeType.user, "u_sara", "20", group="fyp1", rollover=False))
    add(policy(ScopeType.user, "u_sara", "40", group="research_lab", rollover=False))
    add(policy(ScopeType.user, "u_omar", "25", group="fyp1", rollover=False))

    # ---------------------------------------------------------------- issued resources
    def key_hash(fake_key: str) -> str:
        return hashlib.sha256(fake_key.encode()).hexdigest()

    resources = {
        "res_sara_fyp1_key": IssuedResource(
            id=I["res_sara_fyp1_key"], user_id=I["u_sara"], group_id=I["fyp1"],
            provider=Provider.openrouter, credential_id=I["cred_dept_or"],
            external_ref=key_hash("sk-or-v1-FAKE-seed-sara-fyp1"), current_limit=D("20"),
            status=ResourceStatus.active, details={"name": "sara-fyp1"},
            created_at=last_month,
        ),
        "res_sara_lab_key": IssuedResource(
            id=I["res_sara_lab_key"], user_id=I["u_sara"], group_id=I["research_lab"],
            provider=Provider.openrouter, credential_id=I["cred_profb_or"],
            external_ref=key_hash("sk-or-v1-FAKE-seed-sara-lab"), current_limit=D("40"),
            status=ResourceStatus.active, details={"name": "sara-research-lab"},
            created_at=last_month,
        ),
        "res_omar_pod": IssuedResource(
            id=I["res_omar_pod"], user_id=I["u_omar"], group_id=I["fyp1"],
            provider=Provider.runpod, credential_id=I["cred_dept_rp"],
            external_ref="seedpod-omar-fyp1", status=ResourceStatus.running,
            details={"gpu_type": "RTX A4000", "cost_per_hr": 0.17, "disk_gb": 20, "storage_cost_per_hr": 0.0028},
            created_at=month_start + timedelta(days=1),
        ),
        "res_lina_pod": IssuedResource(
            id=I["res_lina_pod"], user_id=I["u_lina"], group_id=I["fyp2"],
            provider=Provider.runpod, credential_id=I["cred_dept_rp"],
            external_ref="seedpod-lina-fyp2", status=ResourceStatus.stopped,
            details={"gpu_type": "RTX A5000", "cost_per_hr": 0.27, "disk_gb": 40, "storage_cost_per_hr": 0.0056},
            created_at=last_month,
        ),
        "res_maya_pod": IssuedResource(
            id=I["res_maya_pod"], user_id=I["u_maya"], group_id=I["research_lab"],
            provider=Provider.runpod, credential_id=I["cred_dept_rp"],
            external_ref="seedpod-maya-lab", status=ResourceStatus.running,
            details={"gpu_type": "A100 PCIe", "cost_per_hr": 1.64, "disk_gb": 50, "storage_cost_per_hr": 0.0069},
            created_at=last_month,
        ),
    }
    rows.extend(resources.values())

    # ---------------------------------------------------------------- ledger
    def entry(period: str, type_: LedgerType, scope: ScopeType, scope_key: str, amount: str, *,
              group: str | None = None, resource: str | None = None, by: str | None = None,
              note: str | None = None, at: datetime) -> LedgerEntry:
        r = resources[resource] if resource else None
        return LedgerEntry(
            scope_type=scope, scope_id=I[scope_key], group_id=I[group] if group else None,
            period=period, type=type_, amount=D(amount),
            provider=r.provider if r else None, credential_id=r.credential_id if r else None,
            resource_id=r.id if r else None, created_by=I[by] if by else None, note=note,
            created_at=at,
        )

    A, S, R, ADJ = LedgerType.allocation, LedgerType.spend, LedgerType.rollover, LedgerType.adjustment
    P, G, U = ScopeType.professor, ScopeType.group, ScopeType.user
    t_prev = month_start - timedelta(days=30)
    t_prev_end = month_start - timedelta(days=2)

    # Last month
    add(entry(prev, A, P, "prof_a", "400", by="u_dana", at=t_prev))
    add(entry(prev, A, G, "fyp1", "50", group="fyp1", by="u_karim", at=t_prev))
    add(entry(prev, A, G, "fyp2", "50", group="fyp2", by="u_karim", at=t_prev))
    add(entry(prev, A, G, "research_lab", "150", group="research_lab", by="u_rania", at=t_prev))
    add(entry(prev, S, U, "u_lina", "-50", group="fyp2", resource="res_lina_pod", at=t_prev_end))
    add(entry(prev, S, U, "u_maya", "-40", group="research_lab", resource="res_maya_pod", at=t_prev_end))
    add(entry(prev, S, U, "u_sara", "-20", group="research_lab", resource="res_sara_lab_key", at=t_prev_end))
    # FYP Team 1 spent nothing last month.

    # Month-end rollover (system job): FYP Team 1 carries its unused $50 -> $100 this month.
    add(entry(cur, R, G, "fyp1", "50", group="fyp1", note=f"Unused budget from {prev}", at=month_start))

    # This month: allocations
    add(entry(cur, A, P, "prof_a", "400", by="u_dana", at=month_start))
    add(entry(cur, A, G, "fyp1", "50", group="fyp1", by="u_karim", at=month_start))
    add(entry(cur, A, G, "fyp2", "50", group="fyp2", by="u_karim", at=month_start))
    add(entry(cur, A, G, "research_lab", "150", group="research_lab", by="u_rania", at=month_start))
    add(entry(cur, A, U, "u_sara", "20", group="fyp1", by="u_karim", at=month_start))
    add(entry(cur, A, U, "u_sara", "40", group="research_lab", by="u_rania", at=month_start))
    add(entry(cur, A, U, "u_omar", "25", group="fyp1", by="u_karim", at=month_start))

    # This month: spend (each with the credential that paid)
    t = now - timedelta(hours=2)
    add(entry(cur, S, U, "u_sara", "-8.5000", group="fyp1", resource="res_sara_fyp1_key", at=t))       # billable
    add(entry(cur, S, U, "u_omar", "-12.3000", group="fyp1", resource="res_omar_pod", at=t))           # billable
    add(entry(cur, S, U, "u_lina", "-41.0000", group="fyp2", resource="res_lina_pod", at=t))           # billable, FYP2 at 82%
    add(entry(cur, S, U, "u_sara", "-15.2000", group="research_lab", resource="res_sara_lab_key", at=t))  # observed
    add(entry(cur, S, U, "u_maya", "-22.0000", group="research_lab", resource="res_maya_pod", at=t))   # billable

    # A correction, as an adjustment entry (never an UPDATE).
    add(entry(cur, ADJ, G, "research_lab", "2.0000", group="research_lab", by="u_rania",
              note="Correction: storage charge counted twice by sync", at=now - timedelta(hours=1)))

    # ---------------------------------------------------------------- usage snapshots
    # Cumulative USD per resource since it was issued; the last one matches its ledger spend.
    spend_totals = {
        "res_sara_fyp1_key": ("0", "8.5"),
        "res_sara_lab_key": ("20", "35.2"),
        "res_omar_pod": ("0", "12.3"),
        "res_lina_pod": ("50", "91"),
        "res_maya_pod": ("40", "62"),
    }
    for key, (start, end) in spend_totals.items():
        s, e = D(start), D(end)
        for frac, when in [(D("0"), month_start), (D("0.5"), now - timedelta(days=1)), (D("1"), now - timedelta(hours=2))]:
            add(UsageSnapshot(issued_resource_id=I[key], polled_at=when, cumulative_usd=s + (e - s) * frac))

    # ---------------------------------------------------------------- access requests
    def request(user: str, group: str, provider: Provider, status: RequestStatus, why: str,
                decided_by: str | None = None) -> AccessRequest:
        return AccessRequest(
            user_id=I[user], group_id=I[group], provider=provider, justification=why, status=status,
            decided_by=I[decided_by] if decided_by else None,
            decided_at=last_month if decided_by else None,
            created_at=last_month - timedelta(days=1) if decided_by else now - timedelta(hours=5),
        )

    add(request("u_sara", "fyp1", Provider.openrouter, RequestStatus.approved, "LLM calls for the FYP chatbot", "u_karim"))
    add(request("u_sara", "research_lab", Provider.openrouter, RequestStatus.approved, "Evaluation runs", "u_rania"))
    add(request("u_omar", "fyp1", Provider.runpod, RequestStatus.approved, "Fine-tuning a small model", "u_karim"))
    add(request("u_lina", "fyp2", Provider.runpod, RequestStatus.approved, "Training the vision model", "u_karim"))
    add(request("u_maya", "research_lab", Provider.runpod, RequestStatus.approved, "A100 experiments", "u_rania"))
    add(request("u_omar", "fyp1", Provider.openrouter, RequestStatus.rejected, "Want to try GPT models", "u_karim"))
    add(request("u_jad", "fyp2", Provider.openrouter, RequestStatus.pending, "Need an API key for the demo"))

    # ---------------------------------------------------------------- audit log
    def audit(actor: str | None, action: str, target_type: str, target: str, at: datetime, **details: object) -> AuditLog:
        return AuditLog(actor_id=I[actor] if actor else None, action=action, target_type=target_type,
                        target_id=str(I[target]), details=details, at=at)

    add(audit("u_dana", "credential.grant", "credential", "cred_dept_rp", t_prev, professor="prof_a", ceiling_usd="300"))
    add(audit("u_karim", "access_request.approve", "user", "u_sara", last_month, group="FYP Team 1", provider="openrouter"))
    add(audit("u_karim", "key.issue", "issued_resource", "res_sara_fyp1_key", last_month, limit_usd="20"))
    add(audit("u_omar", "pod.launch", "issued_resource", "res_omar_pod", month_start + timedelta(days=1), gpu_type="RTX A4000"))
    add(audit(None, "pod.stop", "issued_resource", "res_lina_pod", now - timedelta(hours=2), reason="manual stop by user"))
    add(audit("u_rania", "ledger.adjustment", "group", "research_lab", now - timedelta(hours=1), amount_usd="2.00"))

    return rows


def insert_seed(session: Session, now: datetime | None = None) -> list[SQLModel]:
    """Insert all seed rows (no commit). Used by main() and by the tests."""
    rows = build(now or datetime.now(timezone.utc))
    # The models have no relationship() attributes, so insert table by table in
    # foreign-key order (parents before children).
    for table in SQLModel.metadata.sorted_tables:
        batch = [r for r in rows if r.__tablename__ == table.name]  # type: ignore[attr-defined]
        if batch:
            session.add_all(batch)
            session.flush()
    return rows


def reset(session: Session) -> None:
    # TRUNCATE skips the row-level append-only triggers on purpose: dev/test reset only.
    tables = ", ".join(t.name for t in SQLModel.metadata.sorted_tables)
    session.exec(text(f"TRUNCATE {tables} CASCADE"))  # type: ignore[call-overload]


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the ComputeWise database.")
    parser.add_argument("--reset", action="store_true", help="wipe all tables before seeding (dev only)")
    args = parser.parse_args()

    with Session(get_engine()) as session:
        if args.reset:
            reset(session)
        elif session.exec(select(Department).where(Department.id == I["dept"])).first():  # type: ignore[call-overload]
            print("Already seeded. Use --reset to wipe and seed again.")
            return 0
        rows = insert_seed(session)
        session.commit()

    counts: dict[str, int] = {}
    for r in rows:
        counts[r.__tablename__] = counts.get(r.__tablename__, 0) + 1  # type: ignore[attr-defined]
    print("Seeded:", ", ".join(f"{n} {t}" for t, n in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
