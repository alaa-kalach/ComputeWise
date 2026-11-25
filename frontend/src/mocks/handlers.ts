// Answers API calls in mock mode, following the contract and the backend's rules (same
// status codes, same refusals, same shapes). Stateful: launching a pod or requesting access
// changes what later calls return, until reload or resetWorld().
import { usd } from "../lib/money";
import { ApiError } from "../lib/api/client";
import type {
  AccessRequest,
  GpuOption,
  GroupBudget,
  IssuedKey,
  Notification,
  Pod,
  PodDetail,
  Totals,
  Provider,
  MemberBudget,
  BillingRow,
  Professor,
  ProfessorAdded,
  SyncResult,
  TrendPoint,
  Credential,
  Grant,
} from "../lib/api/types";
import { DEPT, FUNDING, GROUPS, MEMBERS, PERSONAS, PROF_A, PROF_B, USER_IDS, personaMe, world, type LedgerRow, type MockProfessor, type Persona } from "./world";

// ------------------------------------------------------------------ who is calling
let current: Persona | null = null;
export function setMockPersona(key: string | null): void {
  current = key ? (PERSONAS.find((p) => p.key === key) ?? null) : null;
}
export function mockPersona(): Persona | null {
  return current;
}

// ------------------------------------------------------------------ money (4-decimal units, no floats kept)
const U = 10_000;
const units = (s: string | number) => Math.round(Number(s) * U);
const money = (u: number) => (u / U).toFixed(4);

// ------------------------------------------------------------------ helpers
const fail = (status: number, detail: string, extra: Record<string, unknown> = {}): never => {
  throw new ApiError(status, { detail, ...extra });
};
const me = (): Persona => current ?? fail(401, "Not logged in");
const isMember = (p: Persona, group: string) => MEMBERS[group]?.includes(p.key) ?? false;
const isActive = (p: Persona, group: string) => isMember(p, group) && !world.suspended.has(`${group}:${p.key}`);
const ownsGroup = (p: Persona, group: string) =>
  !!p.professor_id && GROUPS.find((g) => g.id === group)?.professor_id === p.professor_id;
const seesGroup = (p: Persona, group: string) => p.admin || ownsGroup(p, group) || isMember(p, group);
const groupName = (id: string) => GROUPS.find((g) => g.id === id)?.name ?? "";

function totals(group: string, user: string | null): Totals {
  const rows = world.ledger.filter((r) => r.group_id === group && (user === null || r.user === user));
  const funding = rows.filter((r) => r.type !== "spend" && (user === null ? r.user === null : true));
  const spend = rows.filter((r) => r.type === "spend");
  const funded = funding.reduce((a, r) => a + units(r.amount), 0);
  const spent = -spend.reduce((a, r) => a + units(r.amount), 0);
  return { funded: money(funded), spent: money(spent), remaining: money(funded - spent) };
}

function budgetFor(group: string, p: Persona | null): GroupBudget {
  const team = totals(group, null);
  const mine = p && world.userPolicies[`${group}:${p.key}`] ? totals(group, p.key) : null;
  const left = Math.max(0, Math.min(units(team.remaining), mine ? units(mine.remaining) : Infinity));

  const rows = world.ledger.filter((r) => r.group_id === group);
  const sum = (rs: LedgerRow[]) => rs.reduce((a, r) => a + units(r.amount), 0);
  const spentOn = (prov: Provider) => money(-sum(rows.filter((r) => r.type === "spend" && r.provider === prov)));
  const professor = GROUPS.find((g) => g.id === group)!.professor_id;

  return {
    group_id: group, period: world.period, team, mine, available: money(left),
    policy: world.teamPolicies[group] ?? null,
    rolled_over: money(sum(rows.filter((r) => r.type === "rollover"))),
    by_provider: { openrouter: spentOn("openrouter"), runpod: spentOn("runpod") },
    funding: FUNDING[professor] ?? null,
  };
}

const GPUS: [string, string, number, string][] = [
  ["RTX A4000", "0.1700", 16, "high"],
  ["RTX A5000", "0.2700", 24, "medium"],
  ["RTX 4090", "0.6900", 24, "low"],
  ["A100 PCIe", "1.6400", 80, "high"],
  ["H100 PCIe", "2.3900", 80, "none"],
];
const RUNNING_STORAGE = units("0.00274"); // 10 GB disk + 10 GB volume while running

function podOut(pod: (typeof world.pods)[number]): Pod {
  const { user: _u, ready_at: _r, jupyter: _j, ...rest } = pod;
  void _u; void _r; void _j;
  return { ...rest };
}

function refreshReadiness(pod: (typeof world.pods)[number]): void {
  if (pod.state === "starting" && pod.ready_at !== null && Date.now() >= pod.ready_at) {
    pod.state = "running";
    pod.started_at = new Date().toISOString();
  }
}

function findPod(id: string) {
  const p = me();
  const pod = world.pods.find((x) => x.id === id);
  if (!pod) return fail(404, "Pod not found");
  const visible = pod.user === p.key || p.admin || ownsGroup(p, pod.group_id);
  if (!visible) return fail(404, "Pod not found");
  return pod;
}

function controlPod(id: string) {
  const p = me();
  const pod = findPod(id);
  if (pod.user !== p.key && !ownsGroup(p, pod.group_id)) {
    fail(403, "Only the group's professor can approve access, issue keys, control pods, or change this group");
  }
  return { pod, asOwner: pod.user === p.key };
}

const refused = (reason: string, detail: string, needed?: number, remaining?: number): never =>
  fail(409, detail, {
    reason,
    needed_usd: needed !== undefined ? money(needed) : null,
    remaining_usd: remaining !== undefined ? money(remaining) : null,
  });

function prelaunch(group: string, owner: Persona, hourly: number): void {
  const available = units(budgetFor(group, owner).available);
  if (available < hourly) {
    refused("budget", `Not enough budget: this pod needs ${money(hourly)} for 1 hour; ${money(available)} is left.`, hourly, available);
  }
}

function approvedAccess(p: Persona, group: string, provider: string): boolean {
  return world.requests.some(
    (r) => r.user_id === USER_IDS[p.key] && r.group_id === group && r.provider === provider && r.status === "approved",
  );
}
function requireManage(group: string): Persona {
  const p = me();
  if (!seesGroup(p, group)) fail(404, "Group not found");
  if (!ownsGroup(p, group)) fail(403, "Only the group's professor can change this group");
  return p;
}

const teamFunding = (groups: string[]) =>
  world.ledger
    .filter((r) => groups.includes(r.group_id) && r.user === null && (r.type === "allocation" || r.type === "adjustment"))
    .reduce((a, r) => a + units(r.amount), 0);

const myGroups = (p: Persona) => GROUPS.filter((g) => g.professor_id === p.professor_id).map((g) => g.id);

function checkCeiling(p: Persona, extra: number): void {
  const ceiling = world.settings[p.professor_id!]?.monthly_amount ?? null;
  if (ceiling == null) return; // tracking only
  const left = units(ceiling) - teamFunding(myGroups(p));
  if (extra > left) {
    fail(409, `That's more than you have left to give: ${usd(money(left))}.`, { unallocated: money(left) });
  }
}
const keyOf = (userId: string): string =>
  Object.keys(USER_IDS).find((k) => USER_IDS[k] === userId) ?? fail(404, "Member not found");

function requireMember(group: string, userId: string): string {
  const key = keyOf(userId);
  if (!MEMBERS[group]?.includes(key)) fail(404, "Member not found");
  return key;
}

function memberOut(group: string, key: string) {
  const person = PERSONAS.find((x) => x.key === key)!;
  return {
    user_id: USER_IDS[key], name: person.name, email: person.email,
    status: world.suspended.has(`${group}:${key}`) ? ("suspended" as const) : ("active" as const),
  };
}

function memberBudget(group: string, key: string): MemberBudget {
  const spent = -world.ledger
    .filter((r) => r.group_id === group && r.user === key && r.type === "spend")
    .reduce((a, r) => a + units(r.amount), 0);
  return {
    user_id: USER_IDS[key],
    spent: money(spent),
    limit: world.userPolicies[`${group}:${key}`] ? totals(group, key) : null,
  };
}
// ------------------------------------------------------------------ C6 (Person 1): department helpers
function requireAdmin(): Persona {
  const p = me();
  if (!p.admin) fail(403, "Only the department admin manages professors");
  return p;
}

/** Like the backend: outside your scope is 404, a professor looking at himself is 403. */
function requireAdminOf(id: string) {
  const p = me();
  const prof = world.professors.find((x) => x.id === id && !x.removed);
  if (!prof || (!p.admin && p.professor_id !== id)) return fail(404, "Professor not found");
  if (!p.admin) fail(403, "Only the department admin manages professors");
  return prof;
}

function professorOut(x: MockProfessor): Professor {
  return {
    id: x.id, user_id: x.user_id, name: x.name, email: x.email, department_id: DEPT,
    budget_setting: world.settings[x.id] ?? { mode: "tracking", monthly_amount: null, rollover: true, rollover_cap: null },
    group_count: GROUPS.filter((g) => g.professor_id === x.id).length,
  };
}

/** Which credential paid, as in the backend seed: Prof. Nassar uses the department's accounts;
 *  Prof. Khoury pays OpenRouter herself (observed) but uses the department's RunPod (billable). */
const BILLABLE: Record<string, Record<Provider, boolean>> = {
  [PROF_A]: { openrouter: true, runpod: true },
  [PROF_B]: { openrouter: false, runpod: true },
};

type Units = { professor_id: string; professor_name: string; billable: number; observed: number; unassigned: number; total: number; openrouter: number; runpod: number };
const emptyRow = (id: string, name: string): Units =>
  ({ professor_id: id, professor_name: name, billable: 0, observed: 0, unassigned: 0, total: 0, openrouter: 0, runpod: 0 });

function addSpend(row: Units, provider: Provider, amount: number, billable: boolean | undefined): void {
  row[billable === undefined ? "unassigned" : billable ? "billable" : "observed"] += amount;
  row[provider] += amount;
  row.total += amount;
}

/** Last month's spend in the backend seed (the mock ledger only holds this month). */
const SEED_LAST_MONTH: [string, Provider, string][] = [[PROF_A, "runpod", "50"], [PROF_B, "runpod", "40"], [PROF_B, "openrouter", "20"]];

function billingRows(period: string): Units[] {
  const rows = new Map<string, Units>();
  const row = (prof: string) => {
    if (!rows.has(prof)) rows.set(prof, emptyRow(prof, world.professors.find((x) => x.id === prof)?.name ?? ""));
    return rows.get(prof)!;
  };
  if (period === world.period) {
    for (const r of world.ledger.filter((x) => x.type === "spend" && x.provider)) {
      const prof = GROUPS.find((g) => g.id === r.group_id)!.professor_id;
      addSpend(row(prof), r.provider!, -units(r.amount), BILLABLE[prof]?.[r.provider!]);
    }
  } else if (period === previousPeriod(world.period)) {
    for (const [prof, provider, amount] of SEED_LAST_MONTH) addSpend(row(prof), provider, units(amount), BILLABLE[prof][provider]);
  }
  return [...rows.values()].sort((a, b) => a.professor_name.localeCompare(b.professor_name));
}

function sumRows(id: string, name: string, rows: Units[]): Units {
  const t = emptyRow(id, name);
  for (const r of rows) for (const k of ["billable", "observed", "unassigned", "total", "openrouter", "runpod"] as const) t[k] += r[k];
  return t;
}

const rowOut = (r: Units): BillingRow => ({
  professor_id: r.professor_id, professor_name: r.professor_name, billable: money(r.billable), observed: money(r.observed),
  unassigned: money(r.unassigned), total: money(r.total), openrouter: money(r.openrouter), runpod: money(r.runpod),
});

function previousPeriod(period: string): string {
  const [y, mo] = period.split("-").map(Number);
  return mo === 1 ? `${y - 1}-12` : `${y}-${String(mo - 1).padStart(2, "0")}`;
}

/** A credential the caller may see (404 otherwise, like the backend's scope). */
function seeCredential(id: string): Credential {
  const p = me();
  const c = world.credentials.find((x) => x.id === id);
  const visible = c && (p.admin
    ? c.owner_type === "department" || world.professors.some((x) => x.id === c.owner_id)
    : !!p.professor_id && (c.owner_id === p.professor_id || c.grants.some((g) => g.professor_id === p.professor_id)));
  if (!c || !visible) return fail(404, "Credential not found");
  return c;
}

function ownsCredential(c: Credential): boolean {
  const p = me();
  return c.owner_type === "department" ? p.admin : c.owner_id === p.professor_id;
}

function seeGrant(credId: string, grantId: string) {
  const cred = seeCredential(credId);
  if (cred.owner_type !== "department" || !me().admin) fail(403, "Only the department admin grants department credentials");
  const grant = cred.grants.find((g) => g.id === grantId);
  if (!grant) return fail(404, "Grant not found");
  return { cred, grant };
}

// ------------------------------------------------------------------ routes
type Route = (m: RegExpMatchArray, body: any, query: URLSearchParams) => unknown; // eslint-disable-line @typescript-eslint/no-explicit-any

const routes: [string, RegExp, Route][] = [
  ["GET", /^\/me$/, () => personaMe(me())],

  ["GET", /^\/groups$/, () => {
    const p = me();
    return GROUPS.filter((g) => seesGroup(p, g.id)).map(({ professor_name: _n, ...g }) => (void _n, g));
  }],

  ["GET", /^\/groups\/([^/]+)\/budget$/, (m) => {
    const p = me();
    if (!seesGroup(p, m[1])) fail(404, "Group not found");
    return budgetFor(m[1], isMember(p, m[1]) ? p : null);
  }],

    ["GET", /^\/budget\/summary$/, () => {
    const p = me();
    if (!p.professor_id) fail(403, "Only professors have a budget summary");
    const groups = myGroups(p);
    const ceiling = world.settings[p.professor_id!]?.monthly_amount ?? null;
    const allocated = teamFunding(groups);
    const spent = -world.ledger
      .filter((r) => groups.includes(r.group_id) && r.type === "spend")
      .reduce((a, r) => a + units(r.amount), 0);
    return {
      period: world.period,
      ceiling: ceiling == null ? null : money(units(ceiling)),
      allocated: money(allocated),
      unallocated: ceiling == null ? null : money(units(ceiling) - allocated),
      spent: money(spent),
      last_synced_at: world.lastSync,
    };
  }],

  ["PUT", /^\/groups\/([^/]+)\/budget-policy$/, (m, body) => {
    const p = requireManage(m[1]);
    const amount = units(body.monthly_amount);
    if (!(amount >= 0)) fail(422, "Monthly amount must be 0 or more");
    const current = world.ledger
      .filter((r) => r.group_id === m[1] && r.user === null && r.type === "allocation")
      .reduce((a, r) => a + units(r.amount), 0);
    checkCeiling(p, amount - current);
    world.teamPolicies[m[1]] = {
      monthly_amount: money(amount),
      rollover: !!body.rollover,
      rollover_cap: body.rollover_cap ? money(units(body.rollover_cap)) : null,
    };
    if (amount !== current) {
      world.ledger.push({ group_id: m[1], user: null, type: "allocation", amount: money(amount - current) });
    }
    return budgetFor(m[1], null);
  }],

  ["POST", /^\/groups\/([^/]+)\/top-ups$/, (m, body) => {
    const p = requireManage(m[1]);
    const amount = units(body.amount);
    if (!(amount > 0)) fail(422, "Top-up must be more than 0");
    if (!body.note?.trim()) fail(422, "Write a short reason for the top-up");
    checkCeiling(p, amount);
    world.ledger.push({ group_id: m[1], user: null, type: "adjustment", amount: money(amount) });
    return budgetFor(m[1], null);
  }],
  ["POST", /^\/groups\/([^/]+)\/corrections$/, (m, body) => {
    const p = requireManage(m[1]);
    const amount = units(body.amount);
    if (!Number.isFinite(amount) || amount === 0) fail(422, "The correction can't be 0");
    if (!body.note?.trim()) fail(422, "Write what the mistake was");
    if (amount > 0) checkCeiling(p, amount); // adding money counts against the ceiling, removing never does
    world.ledger.push({ group_id: m[1], user: null, type: "adjustment", amount: money(amount) });
    return budgetFor(m[1], null);
  }],
  ["GET", /^\/groups\/([^/]+)$/, (m) => {
    const p = me();
    if (!seesGroup(p, m[1])) fail(404, "Group not found");
    const { professor_name: _n, ...group } = GROUPS.find((g) => g.id === m[1])!;
    void _n;
    const staff = p.admin || ownsGroup(p, m[1]);
    const keys = staff ? MEMBERS[m[1]] : MEMBERS[m[1]].filter((k) => k === p.key);
    return { ...group, members: keys.map((k) => memberOut(m[1], k)) };
  }],

  ["POST", /^\/groups\/([^/]+)\/members$/, (m, body) => {
    requireManage(m[1]);
    const email = String(body.email ?? "").trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) fail(422, "Not a valid email address");
    let person = PERSONAS.find((x) => x.email === email);
    let invited = false;
    if (!person) {
      if (!body.name?.trim()) fail(422, "This email is new: add the student's name too");
      const key = `new${Date.now()}`;
      person = { key, name: body.name.trim(), email, roles: ["user"], professor_id: null, admin: false, blurb: "Added by a professor" };
      PERSONAS.push(person);
      USER_IDS[key] = `u-${key}`;
      invited = true;
    }
    if (MEMBERS[m[1]].includes(person.key)) fail(409, "Already a member of this group");
    MEMBERS[m[1]].push(person.key);
    return { ...memberOut(m[1], person.key), invited };
  }],

  ["PATCH", /^\/groups\/([^/]+)\/members\/([^/]+)$/, (m, body) => {
    requireManage(m[1]);
    const key = requireMember(m[1], m[2]);
    const suspend = body.status === "suspended";
    if (suspend) world.suspended.add(`${m[1]}:${key}`);
    else world.suspended.delete(`${m[1]}:${key}`);
    for (const k of world.keys) {
      if (k.user === key && k.group_id === m[1]) k.status = suspend ? "disabled" : "active";
    }
    if (suspend) {
      for (const pod of world.pods) {
        if (pod.user === key && pod.group_id === m[1] && pod.state === "running") {
          pod.state = "stopped";
          pod.stopped_by = "professor";
        }
      }
    }
    return memberOut(m[1], key);
  }],

  ["DELETE", /^\/groups\/([^/]+)\/members\/([^/]+)$/, (m) => {
    requireManage(m[1]);
    const key = requireMember(m[1], m[2]);
    if (world.ledger.some((r) => r.group_id === m[1] && r.user === key && r.type === "spend")) {
      fail(409, "This student has spent money in this group. Suspend them instead, so the history stays complete.");
    }
    MEMBERS[m[1]] = MEMBERS[m[1]].filter((k) => k !== key);
    return undefined;
  }],

  ["GET", /^\/groups\/([^/]+)\/member-budgets$/, (m) => {
    requireManage(m[1]);
    return MEMBERS[m[1]].map((k) => memberBudget(m[1], k));
  }],

  ["PUT", /^\/groups\/([^/]+)\/members\/([^/]+)\/budget-policy$/, (m, body) => {
    requireManage(m[1]);
    const key = requireMember(m[1], m[2]);
    const tag = `${m[1]}:${key}`;
    if (body.monthly_amount === null) {
      delete world.userPolicies[tag];
      return memberBudget(m[1], key);
    }
    const amount = units(body.monthly_amount);
    if (!(amount >= 0)) fail(422, "Limit must be 0 or more");
    const teamFunded = units(totals(m[1], null).funded);
    if (amount > teamFunded) {
      fail(409, `A student's limit can't be more than the team's budget: ${usd(money(teamFunded))}.`);
    }
    const current = world.ledger
      .filter((r) => r.group_id === m[1] && r.user === key && r.type === "allocation")
      .reduce((a, r) => a + units(r.amount), 0);
    world.userPolicies[tag] = money(amount);
    if (amount !== current) {
      world.ledger.push({ group_id: m[1], user: key, type: "allocation", amount: money(amount - current) });
    }
    return memberBudget(m[1], key);
  }],

  ["GET", /^\/access-requests$/, (_m, _b, query) => {
    const p = me();
    let rows = world.requests.filter(
      (r) => r.user_id === USER_IDS[p.key] || p.admin || ownsGroup(p, r.group_id),
    );
    const status = query.get("status");
    const group = query.get("group_id");
    if (status) rows = rows.filter((r) => r.status === status);
    if (group) rows = rows.filter((r) => r.group_id === group);
    return [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at));
  }],

  ["POST", /^\/access-requests$/, (_m, body) => {
    const p = me();
    if (!isMember(p, body.group_id)) fail(404, "Group not found");
    if (!isActive(p, body.group_id)) fail(409, "You are suspended in this group");
    const existing = world.requests.find(
      (r) => r.user_id === USER_IDS[p.key] && r.group_id === body.group_id && r.provider === body.provider &&
        (r.status === "pending" || r.status === "approved"),
    );
    if (existing) fail(409, existing.status === "approved" ? "You already have access" : "You already have a pending request");
    const req: AccessRequest = {
      id: `r${Date.now()}`, user_id: USER_IDS[p.key], user_name: p.name, group_id: body.group_id,
      group_name: groupName(body.group_id), provider: body.provider,
      justification: body.justification?.trim() || null, status: "pending", reason: null,
      decided_by_name: null, decided_at: null, created_at: new Date().toISOString(),
    };
    world.requests.push(req);
    return req;
  }],
  ["POST", /^\/access-requests\/([^/]+)\/(approve|reject)$/, (m, body) => {
    const req = world.requests.find((r) => r.id === m[1]) ?? fail(404, "Request not found");
    const p = requireManage(req.group_id);
    if (req.status !== "pending") fail(409, "This request was already decided");
    req.status = m[2] === "approve" ? "approved" : "rejected";
    req.reason = m[2] === "reject" ? body.reason?.trim() || null : null;
    req.decided_by_name = p.name;
    req.decided_at = new Date().toISOString();
    return req;
  }],
  // PROPOSED (Person 3): my keys, never the secret.
  // PROPOSED (Person 3): my keys, plus every key in my teams if I'm their professor. Never the secret.
  ["GET", /^\/keys$/, () => {
    const p = me();
    return world.keys
      .filter((k) => k.user === p.key || ownsGroup(p, k.group_id))
      .map(({ user, ...k }) => ({
        ...k,
        user_id: USER_IDS[user],
        user_name: PERSONAS.find((x) => x.key === user)!.name,
      }));
  }],

  // PROPOSED (Person 3): the team's professor revokes a key.
  ["POST", /^\/keys\/([^/]+)\/revoke$/, (m) => {
    const key = world.keys.find((k) => k.id === m[1]) ?? fail(404, "Key not found");
    requireManage(key.group_id);
    if (key.status === "disabled") fail(409, "This key is already revoked");
    key.status = "disabled";
    return undefined;
  }],

  ["POST", /^\/keys$/, (_m, body): IssuedKey => {
    const p = me();
    if (!isActive(p, body.group_id) || !approvedAccess(p, body.group_id, "openrouter")) {
      fail(403, "No approved OpenRouter access in this group");
    }
    if (world.keys.some((k) => k.user === p.key && k.group_id === body.group_id)) fail(409, "Key already issued");
    const limit = budgetFor(body.group_id, p).available;
    const id = `k${Date.now()}`;
    world.keys.push({
      id, user: p.key, group_id: body.group_id, group_name: groupName(body.group_id), status: "active",
      limit_usd: limit, usage_usd: "0.0000", created_at: new Date().toISOString(),
    });
    const secret = Array.from({ length: 48 }, () => "abcdef0123456789"[Math.floor(Math.random() * 16)]).join("");
    return { resource_id: id, key: `sk-or-v1-${secret}`, limit_usd: limit };
  }],

  ["GET", /^\/notifications$/, (): Notification[] => {
    const p = me();
    return GROUPS.filter((g) => isMember(p, g.id) || ownsGroup(p, g.id)).flatMap((g) => {
      const t = totals(g.id, null);
      const used = 1 - Number(t.remaining) / Number(t.funded);
      if (used < 0.8) return [];
      const byUser = new Map<string, number>();
      for (const r of world.ledger) {
        if (r.group_id === g.id && r.type === "spend" && r.user) byUser.set(r.user, (byUser.get(r.user) ?? 0) - units(r.amount));
      }
      const top = [...byUser.entries()].sort((a, b) => b[1] - a[1])[0];
      const topName = top ? PERSONAS.find((x) => x.key === top[0])?.name : null;
      return [{
        id: `n-${g.id}`, group_id: g.id, read: false, created_at: new Date().toISOString(),
        level: used >= 1 ? "critical" : "warning",
        message:
          `${g.name} has used ${Math.round(used * 100)}% of its $${Number(t.funded).toFixed(0)} this month.` +
          (topName ? ` ${topName} accounts for most of it.` : ""),
      }];
    });
  }],

  ["GET", /^\/runpod\/gpus$/, (_m, _b, query): GpuOption[] => {
    const p = me();
    const group = query.get("group_id") ?? "";
    if (!seesGroup(p, group)) fail(404, "Group not found");
    const available = units(budgetFor(group, isMember(p, group) ? p : null).available);
    return (["community", "secure"] as const).flatMap((cloud) =>
      GPUS.map(([name, price, vram, availability]) => {
        const hourly = units(price) + RUNNING_STORAGE;
        return {
          gpu_type_id: name, name, vram_gb: vram, cloud, price_per_hour: price,
          availability: availability as GpuOption["availability"],
          affordable_hours: (Math.floor((available / hourly) * 10) / 10).toFixed(1),
        };
      }),
    ).sort((a, b) => units(a.price_per_hour) - units(b.price_per_hour) || a.cloud.localeCompare(b.cloud));
  }],

  ["GET", /^\/pods$/, (_m, _b, query): Pod[] => {
    const p = me();
    const group = query.get("group_id");
    const withTerminated = query.get("include_terminated") === "true";
    return world.pods
      .filter((x) => x.user === p.key || p.admin || ownsGroup(p, x.group_id))
      .filter((x) => !group || x.group_id === group)
      .filter((x) => withTerminated || x.state !== "terminated")
      .map((x) => (refreshReadiness(x), podOut(x)))
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
  }],

  ["POST", /^\/pods$/, (_m, body): Pod => {
    const p = me();
    if (!seesGroup(p, body.group_id)) fail(404, "Group not found");
    if (!isActive(p, body.group_id)) fail(403, "You are not an active member of this group");
    if (!approvedAccess(p, body.group_id, "runpod")) {
      fail(403, "You don't have approved runpod access in this group. Request it first.");
    }
    const gpu = GPUS.find(([name]) => name === body.gpu_type_id);
    if (!gpu) return fail(422, `${body.gpu_type_id} is not offered on ${body.cloud} cloud`);
    if (gpu[3] === "none") refused("unavailable", `No ${gpu[0]} is available on ${body.cloud} cloud right now.`);
    const hourly = units(gpu[1]) + RUNNING_STORAGE;
    prelaunch(body.group_id, p, hourly);
    const id = `pod-${Date.now()}`;
    const pod = {
      id, external_ref: `mock${Math.random().toString(36).slice(2, 12)}`, user_id: USER_IDS[p.key],
      user_name: p.name, group_id: body.group_id, gpu_name: gpu[0], cloud: body.cloud,
      price_per_hour: gpu[1], storage_per_hour: "0.0027", container_disk_gb: body.container_disk_gb ?? 10,
      volume_gb: body.volume_gb ?? 10, state: "starting" as const, started_at: new Date().toISOString(),
      stopped_by: null, spent_this_month_usd: "0.0000", created_at: new Date().toISOString(),
      user: p.key, ready_at: Date.now() + 4000, jupyter: body.jupyter ?? true,
    };
    world.pods.push(pod);
    return podOut(pod);
  }],

  ["GET", /^\/pods\/([^/]+)$/, (m): PodDetail => {
    const p = me();
    const pod = findPod(m[1]);
    refreshReadiness(pod);
    const running = pod.state === "running";
    return {
      ...podOut(pod),
      connection: running
        ? {
            ssh_proxy: `ssh ${pod.external_ref}-64410f89@ssh.runpod.io`,
            ssh_direct: `ssh root@194.26.196.170 -p 22796`,
            jupyter_url: pod.jupyter ? `https://${pod.external_ref}-8888.proxy.runpod.net` : null,
            jupyter_password: pod.jupyter && pod.user === p.key ? "mock-jupyter-password" : null,
          }
        : null,
      gpu_util_percent: running ? 37 : null,
    };
  }],

  ["POST", /^\/pods\/([^/]+)\/stop$/, (m): Pod => {
    const { pod, asOwner } = controlPod(m[1]);
    refreshReadiness(pod);
    if (pod.state !== "running" && pod.state !== "starting") fail(409, "This pod is not running");
    pod.state = "stopped";
    pod.started_at = null;
    pod.stopped_by = asOwner ? "owner" : "professor";
    return podOut(pod);
  }],

  ["POST", /^\/pods\/([^/]+)\/start$/, (m): Pod => {
    const { pod, asOwner } = controlPod(m[1]);
    if (pod.state !== "stopped") refused("not_stopped", "Only a stopped pod can be started.");
    const owner = PERSONAS.find((x) => x.key === pod.user)!;
    if (asOwner && (!isActive(owner, pod.group_id) || !approvedAccess(owner, pod.group_id, "runpod"))) {
      fail(403, "You don't have approved runpod access in this group. Request it first.");
    }
    prelaunch(pod.group_id, owner, units(pod.price_per_hour) + RUNNING_STORAGE);
    pod.state = "starting";
    pod.stopped_by = null;
    pod.ready_at = Date.now() + 4000;
    pod.started_at = new Date().toISOString();
    return podOut(pod);
  }],

  ["DELETE", /^\/pods\/([^/]+)$/, (m) => {
    const { pod } = controlPod(m[1]);
    pod.state = "terminated";
    pod.started_at = null;
    return undefined;
  }],

  // ---------------------------------------------------------------- C6 (Person 1): the department
  ["GET", /^\/professors$/, () => {
    const p = me();
    return world.professors
      .filter((x) => !x.removed && (p.admin || x.id === p.professor_id))
      .map(professorOut);
  }],

  ["POST", /^\/professors$/, (_m, body): ProfessorAdded => {
    requireAdmin();
    const email = String(body.email ?? "").trim().toLowerCase();
    const name = String(body.name ?? "").trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) fail(422, "not a valid email address");
    if (!name) fail(422, "name is empty");
    let prof = world.professors.find((x) => x.email === email);
    if (prof && !prof.removed) fail(409, "Already a professor");
    const known = !!prof || PERSONAS.some((x) => x.email === email);
    if (prof) {
      prof.removed = false; // gets the role back, keeps his history
    } else {
      const n = world.professors.length + 1;
      prof = { id: `p-added-${n}`, user_id: `u-added-${n}`, name, email, removed: false };
      world.professors.push(prof);
      world.settings[prof.id] = { mode: "tracking", monthly_amount: null, rollover: true, rollover_cap: null };
    }
    return { ...professorOut(prof), invited: !known };
  }],

  ["DELETE", /^\/professors\/([^/]+)$/, (m) => {
    const prof = requireAdminOf(m[1]);
    const groups = GROUPS.filter((g) => g.professor_id === prof.id).map((g) => g.id);
    const activeMember = groups.some((g) => MEMBERS[g].some((k) => !world.suspended.has(`${g}:${k}`)));
    const live = world.pods.some((x) => groups.includes(x.group_id) && x.state !== "terminated")
      || world.keys.some((k) => groups.includes(k.group_id) && k.status !== "disabled");
    if (activeMember || live) {
      fail(409, "This professor still has groups with active members, keys or pods. "
        + "He must suspend or remove his members and stop his resources first.");
    }
    prof.removed = true;
    return undefined;
  }],

  ["PUT", /^\/professors\/([^/]+)\/budget-setting$/, (m, body) => {
    const prof = requireAdminOf(m[1]);
    const mode = body.mode;
    if (mode !== "ceiling" && mode !== "tracking") fail(422, "mode must be ceiling or tracking");
    const amount = body.monthly_amount ?? null;
    if (mode === "ceiling" && amount === null) fail(422, "monthly_amount is required for a ceiling");
    if (mode === "tracking" && amount !== null) fail(422, "monthly_amount must be null for tracking only");
    if (amount !== null && !(Number(amount) >= 0)) fail(422, "Input should be greater than or equal to 0");
    if (amount !== null) {
      const given = teamFunding(GROUPS.filter((g) => g.professor_id === prof.id).map((g) => g.id));
      if (given > units(amount)) {
        fail(422, `He has already given his groups $${money(given)} this month; `
          + `the ceiling must leave at least that much (it would leave $${money(units(amount))}).`);
      }
    }
    world.settings[prof.id] = {
      mode, monthly_amount: amount === null ? null : money(units(amount)),
      rollover: body.rollover ?? true,
      rollover_cap: body.rollover_cap == null ? null : money(units(body.rollover_cap)),
    };
    return professorOut(prof);
  }],

  ["GET", /^\/reports\/billing$/, (_m, _b, query) => {
    const p = me();
    const period = query.get("period") ?? "";
    if (!/^\d{4}-\d{2}$/.test(period)) fail(422, "period must look like YYYY-MM");
    if (!p.admin && !p.professor_id) fail(403, "only professors and department admins can see billing reports");
    const all = billingRows(period);
    const rows = p.admin ? all : all.filter((r) => r.professor_id === p.professor_id);
    const report = { period, rows: rows.map(rowOut), totals: rowOut(sumRows("TOTAL", "", rows)) };
    if (query.get("format") === "csv") {
      const cols = ["professor_id", "professor_name", "period", "billable", "observed", "unassigned", "total", "openrouter", "runpod"] as const;
      const line = (r: BillingRow) => cols.map((c) => (c === "period" ? period : r[c])).join(",");
      return [cols.join(","), ...report.rows.map(line), line(report.totals)].join("\n") + "\n";
    }
    return report;
  }],

  ["GET", /^\/reports\/trend$/, (): TrendPoint[] => {
    if (!me().admin) fail(403, "only the department can see spend trends");
    return [previousPeriod(world.period), world.period].map((period) => {
      const t = sumRows("TOTAL", "", billingRows(period));
      return { period, billable: money(t.billable), observed: money(t.observed), unassigned: money(t.unassigned), total: money(t.total) };
    }).filter((x) => units(x.total) > 0);
  }],

  ["POST", /^\/department\/sync$/, (): SyncResult => {
    if (!me().admin) fail(403, "Only the department admin can run a sync by hand");
    world.lastSync = new Date().toISOString();
    const live = world.pods.filter((x) => x.state !== "terminated").length
      + world.keys.filter((k) => k.status !== "disabled").length;
    return { resources_polled: live, spend_entries: 0, skipped: false, errors: 0 };
  }],

  ["GET", /^\/department\/sync$/, () => {
    if (!me().admin) fail(403, "Only the department admin sees the sync status");
    return { last_synced_at: world.lastSync };
  }],

  // ---------------------------------------------------------------- A3: provider accounts and grants
  ["GET", /^\/credentials$/, () => {
    const p = me();
    return world.credentials.filter((c) =>
      p.admin ? c.owner_type === "department" || world.professors.some((x) => x.id === c.owner_id)
        : !!p.professor_id && (c.owner_id === p.professor_id
          || c.grants.some((g) => g.professor_id === p.professor_id && g.status === "active")));
  }],

  ["POST", /^\/credentials$/, (_m, body): Credential => {
    const p = me();
    const provider = body.provider as Provider;
    let owner_id = p.professor_id ?? "";
    if (body.owner === "department") {
      if (!p.admin) fail(403, "Only a department admin adds department credentials");
      owner_id = DEPT;
    } else if (!p.professor_id) {
      fail(403, "Only a professor adds his own credentials");
    }
    const label = String(body.label ?? "").trim();
    const secret = String(body.secret ?? "").trim();
    if (!label) fail(422, "label is empty");
    if (!secret) fail(422, "The key is empty");
    // The backend asks the provider; the mock accepts keys shaped like real ones.
    const looksRight = provider === "openrouter" ? secret.startsWith("sk-or-") : secret.startsWith("rpa_");
    if (!looksRight) {
      fail(422, `${provider} rejected this key. Check that it is a `
        + (provider === "openrouter" ? "management key" : "RunPod API key") + " and that it is still active.");
    }
    const cred: Credential = {
      id: `cred-${world.credentials.length + 1}`, owner_type: body.owner, owner_id, provider, label,
      status: "active", created_at: new Date().toISOString(), grants: [],
    };
    world.credentials.push(cred);
    return cred;
  }],

  ["DELETE", /^\/credentials\/([^/]+)$/, (m) => {
    const cred = seeCredential(m[1]);
    if (!ownsCredential(cred)) fail(403, "Only the credential's owner can revoke it");
    cred.status = "revoked";
    for (const g of cred.grants) g.status = "revoked";
    return undefined;
  }],

  ["POST", /^\/credentials\/([^/]+)\/grants$/, (m, body): Grant => {
    const cred = seeCredential(m[1]);
    if (cred.owner_type !== "department" || !me().admin) fail(403, "Only the department admin grants department credentials");
    if (cred.status !== "active") fail(409, "This credential is revoked");
    const prof = world.professors.find((x) => x.id === body.professor_id && !x.removed);
    if (!prof) return fail(404, "Professor not found");
    if (cred.grants.some((g) => g.professor_id === prof.id && g.status === "active")) fail(409, "Already granted to this professor");
    const ceiling = body.ceiling_usd ?? null;
    if (ceiling !== null && !(Number(ceiling) >= 0)) fail(422, "Input should be greater than or equal to 0");
    const grant: Grant = {
      id: `grant-${cred.id}-${cred.grants.length + 1}`, credential_id: cred.id, professor_id: prof.id,
      professor_name: prof.name, ceiling_usd: ceiling === null ? null : money(units(ceiling)), status: "active",
    };
    cred.grants.push(grant);
    return grant;
  }],

  ["PATCH", /^\/credentials\/([^/]+)\/grants\/([^/]+)$/, (m, body): Grant => {
    const { grant } = seeGrant(m[1], m[2]);
    if (grant.status !== "active") fail(409, "This grant is revoked");
    const ceiling = body.ceiling_usd ?? null;
    if (ceiling !== null && !(Number(ceiling) >= 0)) fail(422, "Input should be greater than or equal to 0");
    grant.ceiling_usd = ceiling === null ? null : money(units(ceiling));
    return grant;
  }],

  ["DELETE", /^\/credentials\/([^/]+)\/grants\/([^/]+)$/, (m) => {
    seeGrant(m[1], m[2]).grant.status = "revoked";
    return undefined;
  }],

  // Person 3's components (C7) on mocks, so the dashboards render without the backend.
  ["GET", /^\/reports\/weekly$/, () => {
    const p = me();
    if (!p.admin && !p.professor_id) fail(403, "only professors and department admins can see weekly reports");
    const monday = new Date();
    monday.setUTCDate(monday.getUTCDate() - ((monday.getUTCDay() + 6) % 7) - 7);
    // The seed's spend is from today, so the last full week is empty, as on the real API.
    return { week_start: monday.toISOString().slice(0, 10), rows: [],
      totals: { this_week_usd: "0.0000", last_week_usd: "0.0000", change_pct: null }, escalations: {} };
  }],

  ["GET", /^\/escalations$/, (_m, _b, query) => {
    const p = me();
    const status = query.get("status");
    return world.escalations.filter((e) =>
      (e.recipient_type === "department" ? p.admin : !!e.group_id && ownsGroup(p, e.group_id))
      && (!status || e.status === status));
  }],

  ["POST", /^\/escalations\/([^/]+)\/(approve|reject)$/, (m, body) => {
    const p = me();
    const e = world.escalations.find((x) => x.id === m[1]);
    const mine = e && (e.recipient_type === "department" ? p.admin : !!e.group_id && ownsGroup(p, e.group_id));
    if (!e || !mine) return fail(404, "Escalation not found");
    if (e.status !== "pending") fail(409, `This escalation is already ${e.status}`);
    e.status = m[2] === "approve" ? "approved" : "rejected";
    e.decided_at = new Date().toISOString();
    e.note = m[2] === "reject" ? (body.note || null) : null;
    e.result = m[2] === "approve" ? {} : null;
    return e;
  }],
];

/** The mock "server". A short delay so loading states are visible while building screens. */
export async function mockFetch(method: string, rawPath: string, body: unknown): Promise<unknown> {
  const url = new URL(rawPath, "http://mock");
  await new Promise((r) => setTimeout(r, import.meta.env.MODE === "test" ? 0 : 180));
  for (const [m, re, handler] of routes) {
    if (m !== method) continue;
    const match = url.pathname.match(re);
    if (match) return structuredClone(handler(match, body ?? {}, url.searchParams) ?? null) ?? undefined;
  }
  return fail(404, `Mock has no route for ${method} ${url.pathname}`);
}

// ------------------------------------------------------------------ mock-only tools (the panel in mock mode)
/** What the group's professor would do in his dashboard (C3): approve every pending request. */
export function approvePendingFor(personaKey: string): number {
  const pending = world.requests.filter((r) => r.user_id === USER_IDS[personaKey] && r.status === "pending");
  for (const r of pending) {
    r.status = "approved";
    r.decided_at = new Date().toISOString();
    r.decided_by_name = GROUPS.find((g) => g.id === r.group_id)?.professor_name ?? null;
  }
  return pending.length;
}

export function simulateSpend(personaKey: string, group: string, amount: string, provider: Provider = "openrouter"): void {
  world.ledger.push({ group_id: group, user: personaKey, type: "spend", amount: `-${amount}`, provider });
}
