# ComputeWise

A budgeting and access-control layer for lab compute on OpenRouter and RunPod.
Departments observe and bill, professors assign budgets and approve access,
and students get keys or pods within their team's budget. A role-scoped
budget-analyst agent forecasts spend, warns users and escalates judgment
calls to a human.

## Stack
React + Vite + shadcn/ui · FastAPI on Vercel · Supabase (Postgres, Auth, pg_cron) · Fernet encryption · pytest with mock adapters

## Layout
| Folder | What lives there | Owner |
|---|---|---|
| `docs/contract/` | Frozen API contract (OpenAPI) | Shared |
| `docs/decisions.md` | Answers to the design's open questions | Shared |
| `backend/app/models/` | SQLModel tables | Shared |
| `backend/app/core/` | Config, DB, auth, scoping, encryption, `write_audit()` | Person 1 |
| `backend/app/access/` | Groups, access requests, credentials, department | Person 1 |
| `backend/app/budget/` | Ledger, budget policies, rollover, top-ups | Person 2 |
| `backend/app/insight/` | Notifications, escalations, reports | Person 3 |
| `backend/app/adapters/` | Interface + mocks (P2), RunPod (P1), OpenRouter (P3) | Split |
| `backend/app/jobs/` | Sync, watchdog, rollover job endpoints | Person 2 |
| `backend/app/agent/` | Agent loop (P3) and tools (split by file) | Split |
| `frontend/src/app/` | Scaffold, login, role layout | Person 3 |
| `frontend/src/pages/` | user, department (P1) · professor (P2) · escalations, reports (P3) | Split |
| `supabase/cron.sql` | pg_cron schedules | Person 2 |
| `evaluation/` | Scenarios, baselines, simulated world, results | Person 3 |
| `demo/` | Demo scenarios | Person 2 |

## Rules
1. **Contract first.** `docs/contract/` and `backend/app/models/` change only through a PR the other two approve.
2. **Mocks first.** Build against `adapters/mock.py` and the seed data.
3. **One folder per slice.** Edit mostly your own folders (see `.github/CODEOWNERS`).
4. **Merge every two or three days** with small PRs.

## Team
Person 1 (Access and identity) = ____ · Person 2 (Budget and enforcement) = ____ · Person 3 (Insight) = ____
