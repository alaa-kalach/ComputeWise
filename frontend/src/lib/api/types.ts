// Types from docs/contract/openapi.yaml. Money is always a decimal STRING ("12.5000"), never
// a JS number (D7): format it with lib/money.ts, never parseFloat it for arithmetic you keep.

export type Money = string;
export type Role = "department_admin" | "professor" | "user";
export type Provider = "openrouter" | "runpod";
export type MemberStatus = "active" | "suspended";
export type RequestStatus = "pending" | "approved" | "rejected";
export type Cloud = "secure" | "community";
export type PodState = "starting" | "running" | "stopped" | "terminated";

export interface Membership {
  group_id: string;
  group_name: string;
  professor_id: string;
  status: MemberStatus;
}

export interface Me {
  id: string;
  name: string;
  email: string;
  role: Role; // primary role: the default dashboard
  roles: Role[]; // every role held: show a view switch when there is more than one
  department_ids: string[];
  professor_id: string | null;
  memberships: Membership[];
}

export interface Group {
  id: string;
  name: string;
  professor_id: string;
  created_at: string;
}

export interface AccessRequest {
  id: string;
  user_id: string;
  user_name: string;
  group_id: string;
  group_name: string;
  provider: Provider;
  justification: string | null;
  status: RequestStatus;
  reason: string | null;
  decided_by_name: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface GpuOption {
  gpu_type_id: string;
  name: string;
  vram_gb: number;
  cloud: Cloud;
  price_per_hour: Money;
  availability: "none" | "low" | "medium" | "high";
  affordable_hours: string | null;
}

export interface PodLaunch {
  group_id: string;
  gpu_type_id: string;
  cloud: Cloud;
  gpu_count?: 1;
  container_disk_gb?: number;
  volume_gb?: number;
  jupyter?: boolean;
}

export interface LaunchRefused {
  detail: string;
  reason: "budget" | "unavailable" | "no_capacity" | "not_stopped";
  needed_usd: Money | null;
  remaining_usd: Money | null;
}

export interface Pod {
  id: string;
  external_ref: string;
  user_id: string;
  user_name: string;
  group_id: string;
  gpu_name: string;
  cloud: Cloud;
  price_per_hour: Money;
  storage_per_hour: Money;
  container_disk_gb: number;
  volume_gb: number;
  state: PodState;
  started_at: string | null;
  stopped_by: "owner" | "professor" | "watchdog" | null;
  spent_this_month_usd: Money;
  created_at: string;
}

export interface PodConnection {
  ssh_proxy: string | null;
  ssh_direct: string | null;
  jupyter_url: string | null;
  jupyter_password: string | null; // the pod's owner only
}

export interface PodDetail extends Pod {
  connection: PodConnection | null;
  gpu_util_percent: number | null;
}

export interface IssuedKey {
  resource_id: string;
  key: string; // plaintext, shown ONCE
  limit_usd: Money;
}

export interface Notification {
  id: string;
  group_id: string;
  message: string;
  level: "info" | "warning" | "critical";
  read: boolean;
  created_at: string;
}

// ---------------------------------------------------------------------------------------
// PROPOSED, not in the contract yet. The mocks serve these shapes; the owners decide the
// real ones and these types follow.

/** PROPOSED for Person 2 (A4): GET /groups/{group_id}/budget, as seen by the caller. */
export interface Totals {
  funded: Money; // allocations + rollover + adjustments this month
  spent: Money;
  remaining: Money;
}
export interface GroupBudget {
  group_id: string;
  period: string; // "YYYY-MM"
  team: Totals;
  mine: Totals | null; // the caller's own sub-budget in this group, if he has one (D9)
  available: Money; // what the caller can still spend here: min(team, mine)
}

/** PROPOSED for Person 3 (B3): GET /keys, the caller's OpenRouter keys (never the secret). */
export interface KeySummary {
  id: string;
  group_id: string;
  group_name: string;
  status: "active" | "capped" | "disabled";
  limit_usd: Money | null;
  usage_usd: Money;
  created_at: string;
}
