// C6: the department mocks follow the backend's rules (same refusals, same numbers as the seed).

import { beforeEach, describe, expect, it } from "vitest";

import { ApiError } from "../lib/api/client";
import { mockFetch, setMockPersona } from "./handlers";
import { PROF_A, PROF_B, resetWorld, world } from "./world";

const call = (method: string, path: string, body?: unknown) => mockFetch(method, path, body) as Promise<any>; // eslint-disable-line @typescript-eslint/no-explicit-any

async function status(p: Promise<unknown>): Promise<number> {
  try {
    await p;
    return 200;
  } catch (e) {
    return (e as ApiError).status;
  }
}

beforeEach(() => {
  resetWorld();
  setMockPersona("dana");
});

describe("professors", () => {
  it("lists the department's professors with how each is funded", async () => {
    const profs = await call("GET", "/professors");
    expect(profs.map((p: { name: string }) => p.name)).toEqual(["Prof. Karim Nassar", "Prof. Rania Khoury"]);
    expect(profs[0].budget_setting).toEqual({ mode: "ceiling", monthly_amount: "400.0000", rollover: true, rollover_cap: null });
    expect(profs[1].budget_setting.mode).toBe("tracking");
  });

  it("lets only the department admin manage professors", async () => {
    setMockPersona("karim");
    expect((await call("GET", "/professors")).length).toBe(1); // himself only
    expect(await status(call("POST", "/professors", { email: "x@cs.example.edu", name: "X" }))).toBe(403);
    expect(await status(call("PUT", `/professors/${PROF_A}/budget-setting`, { mode: "tracking", monthly_amount: null }))).toBe(403);
    expect(await status(call("DELETE", `/professors/${PROF_B}`))).toBe(404); // not his to see
    setMockPersona("sara");
    expect(await call("GET", "/professors")).toEqual([]);
  });

  it("adds a new professor as tracking only and invites him", async () => {
    const p = await call("POST", "/professors", { email: " Hana.Saab@CS.example.edu ", name: "Prof. Hana Saab" });
    expect(p).toMatchObject({ email: "hana.saab@cs.example.edu", invited: true, group_count: 0 });
    expect(p.budget_setting.mode).toBe("tracking");
    expect(await status(call("POST", "/professors", { email: "hana.saab@cs.example.edu", name: "Again" }))).toBe(409);
    expect(await status(call("POST", "/professors", { email: "not-an-email", name: "X" }))).toBe(422);
  });

  it("refuses to remove a professor whose groups are still active, but removes an empty one", async () => {
    expect(await status(call("DELETE", `/professors/${PROF_A}`))).toBe(409);
    const p = await call("POST", "/professors", { email: "hana.saab@cs.example.edu", name: "Prof. Hana Saab" });
    expect(await status(call("DELETE", `/professors/${p.id}`))).toBe(200);
    expect((await call("GET", "/professors")).length).toBe(2);
  });

  it("checks a ceiling like the backend: required for a ceiling, null for tracking, not below what he gave", async () => {
    const put = (body: unknown) => call("PUT", `/professors/${PROF_A}/budget-setting`, body);
    expect(await status(put({ mode: "ceiling", monthly_amount: null }))).toBe(422);
    expect(await status(put({ mode: "tracking", monthly_amount: "100" }))).toBe(422);
    expect(await status(put({ mode: "ceiling", monthly_amount: "80" }))).toBe(422); // he gave his teams $100
    const p = await put({ mode: "ceiling", monthly_amount: "250", rollover: true, rollover_cap: "100" });
    expect(p.budget_setting).toEqual({ mode: "ceiling", monthly_amount: "250.0000", rollover: true, rollover_cap: "100.0000" });
  });

  it("shows a new ceiling on the professor's own dashboard", async () => {
    await call("PUT", `/professors/${PROF_A}/budget-setting`, { mode: "ceiling", monthly_amount: "300" });
    setMockPersona("karim");
    const s = await call("GET", "/budget/summary");
    expect(s.ceiling).toBe("300.0000");
    expect(s.unallocated).toBe("200.0000");
  });
});

describe("reports", () => {
  it("splits this month's spend into billable and observed per professor", async () => {
    const r = await call("GET", `/reports/billing?period=${world.period}`);
    expect(r.rows.map((x: { professor_name: string; billable: string; observed: string }) => [x.professor_name, x.billable, x.observed]))
      .toEqual([["Prof. Karim Nassar", "61.8000", "0.0000"], ["Prof. Rania Khoury", "22.0000", "15.2000"]]);
    expect(r.totals).toMatchObject({ professor_id: "TOTAL", total: "99.0000", openrouter: "23.7000", runpod: "75.3000" });
  });

  it("gives the same report as CSV, with a TOTAL row", async () => {
    const csv: string = await call("GET", `/reports/billing?period=${world.period}&format=csv`);
    const lines = csv.trim().split("\n");
    expect(lines[0]).toBe("professor_id,professor_name,period,billable,observed,unassigned,total,openrouter,runpod");
    expect(lines).toHaveLength(4);
    expect(lines[3]).toBe(`TOTAL,,${world.period},83.8000,15.2000,0.0000,99.0000,23.7000,75.3000`);
  });

  it("shows a professor only his own billing row, and the trend only to the department", async () => {
    setMockPersona("rania");
    const r = await call("GET", `/reports/billing?period=${world.period}`);
    expect(r.rows.map((x: { professor_id: string }) => x.professor_id)).toEqual([PROF_B]);
    expect(await status(call("GET", "/reports/trend"))).toBe(403);
    setMockPersona("sara");
    expect(await status(call("GET", `/reports/billing?period=${world.period}`))).toBe(403);
  });

  it("has a monthly trend, oldest first", async () => {
    const t = await call("GET", "/reports/trend");
    expect(t).toHaveLength(2);
    expect(t[0]).toMatchObject({ billable: "90.0000", observed: "20.0000", total: "110.0000" });
    expect(t[1].period).toBe(world.period);
  });
});

describe("sync now", () => {
  it("runs for the department admin only", async () => {
    expect(await call("POST", "/department/sync")).toMatchObject({ skipped: false, errors: 0 });
    setMockPersona("karim");
    expect(await status(call("POST", "/department/sync"))).toBe(403);
  });
});

describe("department accounts (A3)", () => {
  it("lists the department's accounts and professors' own, never a secret", async () => {
    const creds = await call("GET", "/credentials");
    expect(creds.map((c: { label: string }) => c.label)).toEqual(
      ["CS Department OpenRouter", "CS Department RunPod", "Khoury lab OpenRouter"]);
    expect(JSON.stringify(creds)).not.toMatch(/secret|sk-or|rpa_/);
    setMockPersona("sara");
    expect(await call("GET", "/credentials")).toEqual([]);
  });

  it("adds an account only with a key the provider accepts, and never returns it", async () => {
    expect(await status(call("POST", "/credentials", { owner: "department", provider: "runpod", label: "X", secret: "nope" }))).toBe(422);
    const c = await call("POST", "/credentials", { owner: "department", provider: "runpod", label: "Spare RunPod", secret: "rpa_abc" });
    expect(c).toMatchObject({ owner_type: "department", provider: "runpod", status: "active", grants: [] });
    expect(JSON.stringify(c)).not.toContain("rpa_abc");
    setMockPersona("karim");
    expect(await status(call("POST", "/credentials", { owner: "department", provider: "runpod", label: "X", secret: "rpa_x" }))).toBe(403);
  });

  it("grants once per professor, with a ceiling, and refuses a revoked account", async () => {
    expect(await status(call("POST", "/credentials/cred-dept-or/grants", { professor_id: PROF_A, ceiling_usd: "10" }))).toBe(409);
    const g = await call("POST", "/credentials/cred-dept-or/grants", { professor_id: PROF_B, ceiling_usd: "75" });
    expect(g).toMatchObject({ professor_name: "Prof. Rania Khoury", ceiling_usd: "75.0000", status: "active" });
    expect((await call("PATCH", `/credentials/cred-dept-or/grants/${g.id}`, { ceiling_usd: null })).ceiling_usd).toBeNull();
    await call("DELETE", "/credentials/cred-dept-or");
    expect(await status(call("POST", "/credentials/cred-dept-or/grants", { professor_id: PROF_B, ceiling_usd: null }))).toBe(409);
  });

  it("revoking an account ends all of its grants", async () => {
    await call("DELETE", "/credentials/cred-dept-rp");
    const rp = (await call("GET", "/credentials")).find((c: { id: string }) => c.id === "cred-dept-rp");
    expect(rp.status).toBe("revoked");
    expect(rp.grants.every((g: { status: string }) => g.status === "revoked")).toBe(true);
  });

  it("lets only the owner revoke, and hides other professors' accounts", async () => {
    setMockPersona("karim");
    expect(await status(call("DELETE", "/credentials/cred-dept-or"))).toBe(403); // granted to him, not his
    expect(await status(call("DELETE", "/credentials/cred-khoury-or"))).toBe(404); // not his to see
  });
});
