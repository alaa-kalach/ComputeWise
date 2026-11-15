// Answers API calls in mock mode, following the contract and the backend's rules (same
// status codes, same refusals, same shapes). Stateful: launching a pod or requesting access
// changes what later calls return, until reload or resetWorld().

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
} from "../lib/api/types";
import { GROUPS, MEMBERS, PERSONAS, USER_IDS, personaMe, world, type Persona } from "./world";

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
  return { group_id: group, period: world.period, team, mine, available: money(left) };
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

// ------------------------------------------------------------------ routes
type Route = (m: RegExpMatchArray, body: any, query: URLSearchParams) => unknown; // eslint-disable-line @typescript-eslint/no-explicit-any

const routes: [string, RegExp, Route][] = [
  ["GET", /^\/me$/, () => personaMe(me())],

  ["GET", /^\/groups$/, () => {
    const p = me();
    return GROUPS.filter((g) => seesGroup(p, g.id)).map(({ professor_name: _n, ...g }) => (void _n, g));
  }],

  // PROPOSED (Person 2): what the caller sees of a group's budget.
  ["GET", /^\/groups\/([^/]+)\/budget$/, (m) => {
    const p = me();
    if (!seesGroup(p, m[1])) fail(404, "Group not found");
    return budgetFor(m[1], isMember(p, m[1]) ? p : null);
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

  // PROPOSED (Person 3): my keys, never the secret.
  ["GET", /^\/keys$/, () => {
    const p = me();
    return world.keys.filter((k) => k.user === p.key).map(({ user: _u, ...k }) => (void _u, k));
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

/** What the sync job would record: `amount` USD of spend by this persona in this group. */
export function simulateSpend(personaKey: string, group: string, amount: string): void {
  world.ledger.push({ group_id: group, user: personaKey, type: "spend", amount: `-${amount}` });
}
