// The one way the frontend talks to the backend.
//
//   VITE_USE_MOCKS unset or "true"  -> src/mocks answers, in the browser; no backend, no login.
//   VITE_USE_MOCKS="false"          -> the real API at VITE_API_URL, with the Supabase token.
//
// Pages call the typed functions in endpoints.ts and never know which one answered.

export const USE_MOCKS = import.meta.env.VITE_USE_MOCKS !== "false";
const API_URL = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

/** Any non-2xx answer. `body` is the parsed JSON ({"detail": ...} or a LaunchRefused). */
export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(ApiError.message(body, status));
  }

  static message(body: unknown, status: number): string {
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === "string") return d;
      if (Array.isArray(d)) return d.map((e) => (e as { msg?: string }).msg ?? "Invalid value").join("; ");
    }
    return `Request failed (${status})`;
  }
}

type TokenGetter = () => Promise<string | null>;
let getToken: TokenGetter = async () => null;
/** Set by the auth provider: returns the current Supabase access token. */
export function setTokenGetter(fn: TokenGetter): void {
  getToken = fn;
}

type MockHandler = (method: string, path: string, body: unknown) => Promise<unknown>;
let mockHandler: MockHandler | null = null;
/** Set by src/mocks at start-up in mock mode. */
export function setMockHandler(fn: MockHandler): void {
  mockHandler = fn;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (USE_MOCKS) {
    if (!mockHandler) throw new Error("Mock mode is on but the mocks are not loaded");
    return (await mockHandler(method, path, body)) as T;
  }
  const res = await send(method, path, body);
  if (res.status === 204) return undefined as T;
  const data: unknown = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

/** Like api(), for answers that are text rather than JSON (the CSV billing report). */
export async function apiText(method: string, path: string): Promise<string> {
  if (USE_MOCKS) {
    if (!mockHandler) throw new Error("Mock mode is on but the mocks are not loaded");
    return String(await mockHandler(method, path, undefined));
  }
  const res = await send(method, path);
  if (!res.ok) throw new ApiError(res.status, await res.json().catch(() => null));
  return res.text();
}

async function send(method: string, path: string, body?: unknown): Promise<Response> {
  const token = await getToken();
  return fetch(API_URL + path, {
    method,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}
