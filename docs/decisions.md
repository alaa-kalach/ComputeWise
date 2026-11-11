# Decisions

Shared decisions for ComputeWise. **Agreed** = settled; change it only after telling the
others. **Proposed** = drafted by Person 1 on 2026-11-11, needs a yes from Person 2 and
Person 3 (reply in the group chat; silence by Thursday evening counts as yes).

Format: what we decided, why, and who it affects.

---

## The four open questions from the design

### D1. Department ceiling or tracking only: **both, per professor** (Proposed)
The department sets, per professor, either a monthly **budget ceiling** or **tracking only**
(observe and bill, no ceiling). Stored as the professor-level budget policy:
`monthly_amount` = the ceiling, or `NULL` = tracking only. Already in the schema and seed
(Prof. A has a $400 ceiling, Prof. B is tracking only). Separate from the **grant ceiling**
on a department credential (`credential_grants.ceiling_usd`), which caps what a professor
can spend through that one key.

What limits a **tracking-only** professor? On department credentials, the grant ceiling;
on his own credentials, nothing (it's his money, the department only observes). Like every
level, his allocations to groups and users are checked in the backend.
Affects: Person 1 (A10), Person 2 (allocation checks), Person 3 (department dashboard).

### D2. Rollover cap: **the most that carries over; default 1 × the monthly amount** (Proposed)
At month end, unused budget carries into the next month as one `rollover` entry, but never
more than `rollover_cap` (a cap on the **amount carried**, not on the total). When a
professor doesn't choose a cap, it defaults to 1 × `monthly_amount`: $50 unused in month 1
gives $100 in month 2 (the plan's test), and a team never holds more than twice its
monthly budget. `rollover = false` turns carry-over off. Affects: Person 2 (rollover logic
and job).

### D3. Watchdog stops pods, never terminates: **stop, storage counted** (Proposed)
At 100% of budget the system **stops** pods (GPU released, volume kept). Storage of stopped
pods is counted as spend (RunPod volume ≈ $0.20/GB/month; a 10 GB volume ≈ $0.003/hour).
Terminating deletes the user's data and RunPod forgets the pod, so only a person
(user or professor) terminates. Affects: Person 2 (watchdog), Person 1 (B4).

### D4. Polling interval: **5 minutes** (Proposed)
`POST /jobs/sync` runs every 5 minutes. RunPod allows 180 calls/minute, so limits are not
the constraint; the interval is how stale spend may be. Worst case overshoot ≈ 5 minutes of
the most expensive running pods. A department admin can also trigger **"Sync now"** (the
same endpoint, called by hand), so the demo and evaluation scenarios don't wait 5 minutes
for a stop. Affects: Person 2 (cron, endpoint), Person 3 (button), everyone (demo timing).

### D5. Keeping Supabase awake: **the 5-minute sync keeps it active** (Proposed)
Free Supabase projects pause after **7 days of low activity**; a paused project can be
resumed from the dashboard with data intact. The sync cron hits the database every 5
minutes, which counts as activity, so once the cron is deployed the project stays awake.
Until then, someone uses it at least every few days. Before the demo, check the
dashboard the day before. Affects: Person 2 (cron), everyone.

---

## Data and money

### D6. Schema additions to the design (Agreed in code, please confirm)
- `budget_policies.group_id` and `ledger_entries.group_id`: required for user-scope rows,
  so a student in groups under two professors has a separate budget under each.
- `budget_policies.monthly_amount` nullable (see D1).
- `group_members.status` (`active` / `suspended`): a professor suspends a user only in his
  own groups.
- `issued_resources.details` (JSON: GPU type, hourly cost, disk sizes; never secrets),
  `issued_resources.updated_at`, `ledger_entries.note` (reason for an adjustment).

### D7. Money (Agreed)
USD only, `NUMERIC(12,4)` in the database, `Decimal` in Python, never `float`. In JSON,
send amounts as **strings** (`"12.5000"`) so cents are never rounded. Affects: everyone,
including the frontend's number formatting.

### D8. Ledger rules (Agreed in code, please confirm)
- Append-only: the database rejects UPDATE and DELETE. Mistakes are fixed with an
  `adjustment` entry that has a `note`.
- Balance = SUM of entries. Signs: `allocation` +, `rollover` +, `spend` −, `adjustment` ±.
- Every `spend` entry carries `provider` and the `credential_id` that paid (the database
  enforces it). Department credential = **billable**, professor's own = **observed**.
- `period` is `YYYY-MM`.

### D9. Team vs user budgets (Proposed, Person 2 to confirm)
A **user budget is a sub-limit inside the team budget**, not extra money:
- Team remaining = team allocations + rollovers + adjustments (scope `group`) + all spend
  in the group (`group_id` = team).
- User remaining (if the user has a policy) = user allocations (scope `user`, same
  `group_id`) + that user's spend in that group.
- A user can spend while **both** are positive. A user with no user policy is limited only
  by the team.
Spend is recorded at user scope with `group_id` set, so both sums come from the same rows.
Affects: Person 2 (ledger functions), Person 3 (dashboards).

---

## Access and security

### D10. Login (Agreed)
- Supabase Auth, email + password. **Public sign-up is off.** A user must already exist
  in `users` (added by a professor or admin, or the seed); the first login links the
  Supabase account by email.
- The frontend uses supabase-js **only for login**. Supabase's Data API is disabled; all
  data goes through our backend with `Authorization: Bearer <access token>`.
- After login the frontend calls `GET /me` and picks the layout from `role`
  (`department_admin`, `professor`, `user`); `roles` lists every role held (see D21).

### D11. Authorization (Agreed)
- One shared layer: every endpoint takes `scope: ScopeDep` and uses its queries
  (`scope.groups()`, `scope.ledger_entries()`, ...) or checks (`scope.require_group(id)`).
  No endpoint writes its own permission logic.
- Outside your scope → **404**, not 403, so ids of other professors' objects don't leak.
- The department **observes and bills**; only the owning professor **manages** a group
  (`scope.require_manage_group(id)` → 403 for the department). See D21.
- A user sees his own rows and his teams' totals, not teammates' individual rows.

### D21. Only professors approve access and issue keys; roles can combine (Agreed, 2026-11-11)
- Approving or rejecting access, issuing, limiting or revoking OpenRouter keys, stopping or
  starting pods in a group, setting group and user budgets, and suspending members are
  done **only by the group's own professor (Sub-admin)**.
- **Approved students launch their own pods** (as the design says). Approving a student's
  RunPod access is what enables it; the launch endpoint checks that the student has
  approved RunPod access in that group and passes the pre-launch budget check. It does
  **not** use the professor-only check. A student can also stop his own pods.
- The **department admin cannot do any of these**. The department adds and removes
  professors, owns and grants credentials, sets professor ceilings or tracking only,
  observes and bills.
- A person can hold **both roles**. A department admin who is also given the professor
  role manages **his own groups** like any professor, and still only observes the others.
- In code: `CurrentUser.roles` holds every role (`role` is the primary one, for the default
  dashboard); every approve, issue, limit, revoke, budget and suspend endpoint, and the
  professor's stop/start of any pod in his group, calls `scope.require_manage_group(group_id)`,
  which passes only for the owning professor. `GET /me` returns `role` and `roles`;
  the frontend offers a view switch when `roles` has more than one entry.
Affects: Person 1 (A5, B4: student launch vs professor stop/start), Person 3 (B3 key
issuing goes through the same check; role switch in the layout; no approve or issue
buttons in the department views).

### D12. Audit (Agreed)
Every approval, limit change, key issue or revocation, pod launch/stop/terminate, budget
change and enforcement action calls `write_audit(...)` **before** `session.commit()`.
Action names are `thing.verb` (`key.issue`, `pod.stop`, `budget.update`).

### D13. Secrets (Agreed)
Provider keys are Fernet-encrypted in the database and never returned by any API. Everyone
uses the **same `FERNET_KEY`**, shared privately, never committed. Personal dev keys
(e.g. `RUNPOD_API_KEY`) live only in each person's `.env`.

---

## Providers

### D14. RunPod: API v2 only (Agreed, deadline-driven)
REST v1 retires on **2026-11-15**. All RunPod code uses `https://api.runpod.io/v2`.
Response shapes and gotchas are in `docs/notes/runpod.md`; the mock adapter replays
`docs/notes/runpod_samples/`.

### D15. RunPod spend is estimated, then reconciled (Proposed)
RunPod billing lags (nothing recorded a minute after a pod ended), so the design's
"poll billing history per pod" can't drive enforcement. Each sync estimates pod spend as
hourly `cost` × running time, plus volume storage for stopped pods, and writes `spend`
entries. When RunPod's billing records arrive, the difference is written as an
`adjustment`. Affects: Person 2 (sync, watchdog), Person 1 (B4).

### D16. RunPod cloud and GPU choice (Proposed)
At launch the user picks a GPU from the live catalog, showing price and availability for
both **Secure** and **Community** cloud (community is cheaper but less reliable; secure was
often unavailable below $0.50/h). The pre-launch check refuses a launch the team budget
can't cover for at least **1 hour** (RunPod's own minimum credit to start a pod; 2 hours
would block mid-range GPUs on the ~$2 evaluation budgets), or a GPU with availability
`NONE`. Affects: Person 1
(B4), Person 3 (launch screen).

### D17. Tagging pods (Proposed)
Pods are named `cw-<group8>-<user8>` (first 8 characters of the ids). `issued_resources`
is the source of truth for who owns a pod; RunPod's own `tags` field is untested.

---

## Working together

### D18. Database environments (Agreed)
- One shared Supabase project (`computewise`, Frankfurt) with the seed data. Nobody runs
  `scripts.seed --reset` on it without telling the others.
- Each person has their own `computewise-test` project for `pytest` (`TEST_DATABASE_URL`);
  tests wipe it. The tests refuse to run on the shared database.

### D19. Migrations: **Person 1 owns them** (Proposed)
Person 1 owns `backend/app/models/` and every Alembic migration. Anyone who needs a schema
change asks Person 1, who adds a **new** revision (never edits an existing one), runs
`alembic upgrade head` on the shared database and tells the others to pull. One owner
means Alembic never ends up with two competing heads.

### D20. Git (Agreed)
We push to `main` directly. Changes to shared folders (`backend/app/models/`,
`backend/app/core/`, `docs/contract/`) are announced in the group chat with what changed.
Always `git pull` before `git push`.
