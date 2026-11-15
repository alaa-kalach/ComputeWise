# ComputeWise frontend

Vite + React + TypeScript. Supabase is used for login only; every piece of data comes from
our backend (D10).

## Run it

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
npm test             # vitest: the mocks' rules and the screens
npm run build        # type-check + production build
```

## Mocks or the real API

One setting, in the repo-root `.env`:

| `VITE_USE_MOCKS` | What happens |
|---|---|
| unset or `true` | The browser answers every API call from `src/mocks/`. Sign in by picking a seed person; no backend, no Supabase. A "Mock tools" panel plays the parts that aren't built yet (a professor approving, the sync job recording spend). |
| `false` | Real login with Supabase (`VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`) and the real API at `VITE_API_URL` (default `http://localhost:8000`). |

The mock world is a copy of `backend/scripts/seed.py`: the same people, groups, budgets, keys
and pods, so a screen shows the numbers the real API will.

## Where things go

| Path | What |
|---|---|
| `src/lib/api/types.ts` | Types from `docs/contract/openapi.yaml`. Money is a string (`"12.5000"`), never a number. |
| `src/lib/api/endpoints.ts` | One typed function per API call. Pages use these, never `fetch`. |
| `src/lib/auth.tsx` | Login, `GET /me`, and the view switch for people with several roles (D21). |
| `src/lib/money.ts` | `usd()`, `perHour()`: formatting only. |
| `src/mocks/` | The mock server (`handlers.ts`) and its data (`world.ts`). Add a route here when you add an endpoint. |
| `src/components/ui/` | Shared pieces: `Button`, `Modal`, `Notice`, `Tag`, `CopyField`, `BudgetMeter`. |
| `src/pages/user/` | C4, the student dashboard (Person 1). |
| `src/pages/professor/` | C3, the professor dashboard (Person 2). Placeholder for now. |
| `src/pages/department/` | C6, the department dashboard (Person 1, Week 3). Placeholder for now. |
| `src/styles.css` | Design tokens (colours, type, spacing) and all styles, light and dark. |

## Not in the contract yet

The student dashboard needs two calls the contract doesn't have. The mocks serve these shapes
(see `types.ts`); the owners decide the real ones:

- `GET /groups/{group_id}/budget` (Person 2): the caller's view of a group's budget:
  `team` and `mine` totals and `available`.
- `GET /keys` (Person 3): the caller's OpenRouter keys with usage and limit, never the secret.
