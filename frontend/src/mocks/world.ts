// An in-memory copy of the backend seed (backend/scripts/seed.py): the same people, groups,
// budgets, keys and pods, so screens built on mocks show the numbers the real API will.
// State lives in memory and resets on reload (resetWorld()).

import type { AccessRequest, BudgetPolicy, BudgetSetting, Escalation, KeySummary, Me, Pod, Provider, Role } from "../lib/api/types";

export interface Persona {
  key: string;
  name: string;
  email: string;
  roles: Role[];
  professor_id: string | null;
  admin: boolean;
  blurb: string; // shown on the mock sign-in screen
}

export const DEPT = "d0000000-0000-4000-8000-000000000001";
export const PROF_A = "p0000000-0000-4000-8000-00000000000a";
export const PROF_B = "p0000000-0000-4000-8000-00000000000b";
export const PROF_DANA = "p0000000-0000-4000-8000-00000000000d";
export const FYP1 = "g0000000-0000-4000-8000-000000000001";
export const FYP2 = "g0000000-0000-4000-8000-000000000002";
export const LAB = "g0000000-0000-4000-8000-000000000003";

export const PERSONAS: Persona[] = [
  { key: "sara", name: "Sara Mansour", email: "sara.mansour@students.example.edu", roles: ["user"], professor_id: null, admin: false, blurb: "Student in two professors' groups" },
  { key: "omar", name: "Omar Itani", email: "omar.itani@students.example.edu", roles: ["user"], professor_id: null, admin: false, blurb: "Student with a running pod" },
  { key: "lina", name: "Lina Saleh", email: "lina.saleh@students.example.edu", roles: ["user"], professor_id: null, admin: false, blurb: "Student, team budget almost used" },
  { key: "jad", name: "Jad Fares", email: "jad.fares@students.example.edu", roles: ["user"], professor_id: null, admin: false, blurb: "Student with a pending request" },
  { key: "maya", name: "Maya Aoun", email: "maya.aoun@students.example.edu", roles: ["user"], professor_id: null, admin: false, blurb: "Student in the research lab" },
  { key: "karim", name: "Prof. Karim Nassar", email: "karim.nassar@cs.example.edu", roles: ["professor"], professor_id: PROF_A, admin: false, blurb: "Professor, $400 ceiling" },
  { key: "rania", name: "Prof. Rania Khoury", email: "rania.khoury@cs.example.edu", roles: ["professor"], professor_id: PROF_B, admin: false, blurb: "Professor, tracking only" },
  { key: "dana", name: "Dana Haddad", email: "dana.haddad@cs.example.edu", roles: ["department_admin"], professor_id: null, admin: true, blurb: "Department admin" },
  { key: "dana2", name: "Dana Haddad", email: "dana.haddad@cs.example.edu", roles: ["department_admin", "professor"], professor_id: PROF_DANA, admin: true, blurb: "Admin who is also a professor (view switch)" },
];

export const USER_IDS: Record<string, string> = {
  sara: "u0000000-0000-4000-8000-00000000005a",
  omar: "u0000000-0000-4000-8000-00000000000a",
  lina: "u0000000-0000-4000-8000-00000000001a",
  jad: "u0000000-0000-4000-8000-00000000003a",
  maya: "u0000000-0000-4000-8000-00000000004a",
  karim: "u0000000-0000-4000-8000-0000000000ka",
  rania: "u0000000-0000-4000-8000-0000000000ra",
  dana: "u0000000-0000-4000-8000-0000000000da",
  dana2: "u0000000-0000-4000-8000-0000000000da",
};

export const GROUPS = [
  { id: FYP1, name: "FYP Team 1", professor_id: PROF_A, professor_name: "Prof. Karim Nassar", created_at: "2026-09-01T09:00:00Z" },
  { id: FYP2, name: "FYP Team 2", professor_id: PROF_A, professor_name: "Prof. Karim Nassar", created_at: "2026-09-01T09:00:00Z" },
  { id: LAB, name: "Research Lab", professor_id: PROF_B, professor_name: "Prof. Rania Khoury", created_at: "2026-09-01T09:00:00Z" },
];

/** Who is in which group. */
export const MEMBERS: Record<string, string[]> = {
  [FYP1]: ["sara", "omar"],
  [FYP2]: ["lina", "jad"],
  [LAB]: ["sara", "maya"],
};

export const CEILINGS: Record<string, string | null> = {
  [PROF_A]: "400",
  [PROF_B]: null, // tracking only
  [PROF_DANA]: null,
};

export const FUNDING: Record<string, { label: string; billable: boolean }> = {
  [PROF_A]: { label: "Department OpenRouter + RunPod", billable: true },
  [PROF_B]: { label: "Prof. Khoury's own accounts", billable: false },
};

export interface LedgerRow {
  group_id: string;
  user: string | null; // null = team-level (allocation, rollover, adjustment)
  type: "allocation" | "rollover" | "adjustment" | "spend";
  amount: string;
  provider?: Provider; // spend rows only
}

/** C6: a professor as the department sees him. The seed has two; the admin can add more. */
export interface MockProfessor {
  id: string;
  user_id: string;
  name: string;
  email: string;
  removed: boolean;
}

export interface World {
  period: string;
  /** C6: each professor's budget setting from the department (CEILINGS is only the start value). */
  settings: Record<string, BudgetSetting>;
  professors: MockProfessor[];
  escalations: Escalation[];
  lastSync: string;
  ledger: LedgerRow[];
  /** user sub-budgets: `${group}:${user}` -> monthly amount (D9) */
  userPolicies: Record<string, string>;
  teamPolicies: Record<string, BudgetPolicy>;
  requests: AccessRequest[];
  keys: (Omit<KeySummary, "user_id" | "user_name"> & { user: string })[];
  pods: (Pod & { user: string; ready_at: number | null; jupyter: boolean })[];
  suspended: Set<string>; // `${group}:${user}`
}

const now = () => new Date().toISOString();

export function period(): string {
  return new Date().toISOString().slice(0, 7);
}

function freshWorld(): World {
  const t = new Date(Date.now() - 2 * 3600_000).toISOString();
  const lastMonth = new Date(Date.now() - 20 * 86400_000).toISOString();
  const req = (
    id: string, user: string, group: string, provider: "openrouter" | "runpod",
    status: AccessRequest["status"], why: string, decided = false,
  ): AccessRequest => ({
    id, user_id: USER_IDS[user], user_name: PERSONAS.find((p) => p.key === user)!.name,
    group_id: group, group_name: GROUPS.find((g) => g.id === group)!.name, provider,
    justification: why, status, reason: null,
    decided_by_name: decided ? GROUPS.find((g) => g.id === group)!.professor_name : null,
    decided_at: decided ? lastMonth : null, created_at: decided ? lastMonth : t,
  });
  const pod = (
    id: string, user: string, group: string, gpu: string, price: string, state: Pod["state"], spent: string,
  ) => ({
    id, external_ref: `seedpod-${user}`, user_id: USER_IDS[user], user_name: PERSONAS.find((p) => p.key === user)!.name,
    group_id: group, gpu_name: gpu, cloud: "community" as const, price_per_hour: price, storage_per_hour: "0.0027",
    container_disk_gb: 10, volume_gb: 10, state, started_at: state === "running" ? t : null,
    stopped_by: state === "stopped" ? ("owner" as const) : null, spent_this_month_usd: spent,
    created_at: lastMonth, user, ready_at: state === "running" ? 0 : null, jupyter: true,
  });
  const setting = (ceiling: string | null): BudgetSetting =>
    ceiling === null
      ? { mode: "tracking", monthly_amount: null, rollover: true, rollover_cap: null }
      : { mode: "ceiling", monthly_amount: Number(ceiling).toFixed(4), rollover: true, rollover_cap: null };
  return {
    period: period(),
    settings: Object.fromEntries(Object.entries(CEILINGS).map(([id, c]) => [id, setting(c)])),
    professors: [
      { id: PROF_A, user_id: USER_IDS.karim, name: "Prof. Karim Nassar", email: "karim.nassar@cs.example.edu", removed: false },
      { id: PROF_B, user_id: USER_IDS.rania, name: "Prof. Rania Khoury", email: "rania.khoury@cs.example.edu", removed: false },
    ],
    escalations: [
      {
        id: "esc-dept-1", kind: "escalate_to_department", target: PROF_A, recipient_type: "department", group_id: null,
        status: "pending", created_at: t, decided_at: null, note: null, result: null,
        reason: [
          "ISSUE: Prof. Karim Nassar's teams are on course to need more than his $400 ceiling this month.",
          "EVIDENCE: FYP Team 2 has used 82% of its $50 after 10 days; projected demand across his teams is about $430.",
          "PROPOSED ACTION: Talk to Prof. Nassar about raising his ceiling or slowing FYP Team 2.",
          "IF IGNORED: FYP Team 2 runs out around day 12 and its pod is stopped automatically.",
        ].join("\n"),
      },
    ],
    lastSync: new Date(Date.now() - 3 * 60_000).toISOString(),
    ledger: [
      { group_id: FYP1, user: null, type: "rollover", amount: "50" },
      { group_id: FYP1, user: null, type: "allocation", amount: "50" },
      { group_id: FYP2, user: null, type: "allocation", amount: "50" },
      { group_id: LAB, user: null, type: "allocation", amount: "150" },
      { group_id: LAB, user: null, type: "adjustment", amount: "2" },
      { group_id: FYP1, user: "sara", type: "allocation", amount: "20" },
      { group_id: LAB, user: "sara", type: "allocation", amount: "40" },
      { group_id: FYP1, user: "omar", type: "allocation", amount: "25" },
      { group_id: FYP1, user: "sara", type: "spend", amount: "-8.5", provider: "openrouter" },
      { group_id: FYP1, user: "omar", type: "spend", amount: "-12.3", provider: "runpod" },
      { group_id: FYP2, user: "lina", type: "spend", amount: "-41", provider: "runpod" },
      { group_id: LAB, user: "sara", type: "spend", amount: "-15.2", provider: "openrouter" },
      { group_id: LAB, user: "maya", type: "spend", amount: "-22", provider: "runpod" },
    ],
    userPolicies: { [`${FYP1}:sara`]: "20", [`${LAB}:sara`]: "40", [`${FYP1}:omar`]: "25" },
    teamPolicies: {
      [FYP1]: { monthly_amount: "50.0000", rollover: true, rollover_cap: null },
      [FYP2]: { monthly_amount: "50.0000", rollover: false, rollover_cap: null },
      [LAB]: { monthly_amount: "150.0000", rollover: true, rollover_cap: null },
    },
    requests: [
      req("r1", "sara", FYP1, "openrouter", "approved", "LLM calls for the FYP chatbot", true),
      req("r2", "sara", LAB, "openrouter", "approved", "Evaluation runs", true),
      req("r3", "omar", FYP1, "runpod", "approved", "Fine-tuning a small model", true),
      req("r4", "lina", FYP2, "runpod", "approved", "Training the vision model", true),
      req("r5", "maya", LAB, "runpod", "approved", "A100 experiments", true),
      { ...req("r6", "omar", FYP1, "openrouter", "rejected", "Want to try GPT models", true), reason: "Use the team pod for now" },
      req("r7", "jad", FYP2, "openrouter", "pending", "Need an API key for the demo"),
    ],
    keys: [
      { id: "k1", user: "sara", group_id: FYP1, group_name: "FYP Team 1", status: "active", limit_usd: "20.0000", usage_usd: "8.5000", created_at: lastMonth },
      { id: "k2", user: "sara", group_id: LAB, group_name: "Research Lab", status: "active", limit_usd: "60.0000", usage_usd: "35.2000", created_at: lastMonth },
    ],
    pods: [
      pod("res_omar_pod", "omar", FYP1, "RTX A4000", "0.1700", "running", "12.3000"),
      pod("res_lina_pod", "lina", FYP2, "RTX A5000", "0.2700", "stopped", "41.0000"),
      pod("res_maya_pod", "maya", LAB, "A100 PCIe", "1.6400", "running", "22.0000"),
    ],
    suspended: new Set(),
  };
}

export let world: World = freshWorld();

export function resetWorld(): void {
  world = freshWorld();
}

export function personaMe(p: Persona): Me {
  const memberships = GROUPS.filter((g) => MEMBERS[g.id].includes(p.key)).map((g) => ({
    group_id: g.id,
    group_name: g.name,
    professor_id: g.professor_id,
    status: world.suspended.has(`${g.id}:${p.key}`) ? ("suspended" as const) : ("active" as const),
  }));
  return {
    id: USER_IDS[p.key],
    name: p.name,
    email: p.email,
    role: p.roles[0],
    roles: p.roles,
    department_ids: p.admin || p.professor_id ? [DEPT] : [],
    professor_id: p.professor_id,
    memberships,
  };
}

export { now };
