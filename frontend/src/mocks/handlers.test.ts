// The mocks must follow the backend's rules, or screens built on them will break on the real API.

import { beforeEach, describe, expect, it } from "vitest";

import { ApiError } from "../lib/api/client";
import { approvePendingFor, mockFetch, setMockPersona, simulateSpend } from "./handlers";
import { FYP1, FYP2, LAB, resetWorld } from "./world";

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
  setMockPersona("sara");
});

describe("budgets (same numbers as the backend seed)", () => {
  it("keeps a student's budgets under two professors separate", async () => {
    const fyp = await call("GET", `/groups/${FYP1}/budget`);
    const lab = await call("GET", `/groups/${LAB}/budget`);
    expect(fyp.team).toEqual({ funded: "100.0000", spent: "20.8000", remaining: "79.2000" });
    expect(fyp.mine.remaining).toBe("11.5000");
    expect(fyp.available).toBe("11.5000");
    expect(lab.available).toBe("24.8000");
  });

  it("is limited by the team when there is no personal budget", async () => {
    setMockPersona("lina");
    const b = await call("GET", `/groups/${FYP2}/budget`);
    expect(b.mine).toBeNull();
    expect(b.available).toBe("9.0000");
  });

  it("hides other groups", async () => {
    setMockPersona("omar");
    expect(await status(call("GET", `/groups/${LAB}/budget`))).toBe(404);
  });
});

describe("access requests", () => {
  it("refuses duplicates and lets a rejected request be asked again", async () => {
    setMockPersona("jad");
    expect(await status(call("POST", "/access-requests", { group_id: FYP2, provider: "openrouter" }))).toBe(409);
    setMockPersona("omar");
    const r = await call("POST", "/access-requests", { group_id: FYP1, provider: "openrouter" });
    expect(r.status).toBe("pending");
  });

  it("only in your own groups", async () => {
    setMockPersona("omar");
    expect(await status(call("POST", "/access-requests", { group_id: LAB, provider: "runpod" }))).toBe(404);
  });
});

describe("keys", () => {
  it("gives the key once, after approval, with the available budget as limit", async () => {
    setMockPersona("jad");
    expect(await status(call("POST", "/keys", { group_id: FYP2 }))).toBe(403);
    approvePendingFor("jad");
    const k = await call("POST", "/keys", { group_id: FYP2 });
    expect(k.key).toMatch(/^sk-or-v1-/);
    expect(k.limit_usd).toBe("9.0000");
    expect(await status(call("POST", "/keys", { group_id: FYP2 }))).toBe(409);
    const list = await call("GET", "/keys");
    expect(list).toHaveLength(1);
    expect(JSON.stringify(list)).not.toContain(k.key);
  });
});

describe("pods", () => {
  it("launches for an approved student, starting until ready", async () => {
    setMockPersona("omar");
    const pod = await call("POST", "/pods", { group_id: FYP1, gpu_type_id: "RTX A4000", cloud: "community" });
    expect(pod.state).toBe("starting");
    const detail = await call("GET", `/pods/${pod.id}`);
    expect(detail.connection).toBeNull(); // not ready yet: no SSH or Jupyter
  });

  it("refuses a student without approved RunPod access", async () => {
    expect(await status(call("POST", "/pods", { group_id: FYP1, gpu_type_id: "RTX A4000", cloud: "community" }))).toBe(403);
  });

  it("refuses an unavailable GPU and an unaffordable hour with the contract's LaunchRefused", async () => {
    setMockPersona("omar");
    await expect(call("POST", "/pods", { group_id: FYP1, gpu_type_id: "H100 PCIe", cloud: "community" })).rejects.toMatchObject({
      status: 409,
      body: { reason: "unavailable" },
    });
    simulateSpend("omar", FYP1, "12.6");
    await expect(call("POST", "/pods", { group_id: FYP1, gpu_type_id: "RTX A4000", cloud: "community" })).rejects.toMatchObject({
      status: 409,
      body: { reason: "budget", needed_usd: "0.1727", remaining_usd: "0.1000" },
    });
  });

  it("shows the Jupyter password to the owner only", async () => {
    setMockPersona("omar");
    expect((await call("GET", "/pods/res_omar_pod")).connection.jupyter_password).toBe("mock-jupyter-password");
    setMockPersona("karim");
    expect((await call("GET", "/pods/res_omar_pod")).connection.jupyter_password).toBeNull();
    setMockPersona("sara"); // a teammate can't see it at all
    expect(await status(call("GET", "/pods/res_omar_pod"))).toBe(404);
  });

  it("lets the owner and the professor stop, but not the department", async () => {
    setMockPersona("dana");
    expect(await status(call("POST", "/pods/res_omar_pod/stop"))).toBe(403);
    setMockPersona("karim");
    expect((await call("POST", "/pods/res_omar_pod/stop")).stopped_by).toBe("professor");
    setMockPersona("omar");
    await expect(call("POST", "/pods/res_omar_pod/start")).resolves.toMatchObject({ state: "starting" });
    await expect(call("POST", "/pods/res_omar_pod/start")).rejects.toMatchObject({ body: { reason: "not_stopped" } });
  });
});
