// Typed calls, one per contract operation. Add new ones here, not inline in pages.

import { api, apiText } from "./client";
import type {
  AccessRequest,
  GpuOption,
  Group,
  GroupBudget,
  IssuedKey,
  KeySummary,
  Me,
  Notification,
  Pod,
  PodDetail,
  PodLaunch,
  Provider,
  BudgetPolicy,
  Money,
  Escalation,
  EscalationStatus,
  WeeklyReport,
  ProfessorBudgetSummary,
  GroupDetail,
  Member,
  MemberBudget,
  MemberStatus,
  BillingReport,
  BudgetSetting,
  Professor,
  ProfessorAdded,
  SyncResult,
  TrendPoint,
  Credential,
  Grant,
  OwnerType,
  SyncStatus,
} from "./types";

const q = (params: Record<string, string | boolean | undefined>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) s.set(k, String(v));
  const str = s.toString();
  return str ? `?${str}` : "";
};

export const getMe = () => api<Me>("GET", "/me");
export const listGroups = () => api<Group[]>("GET", "/groups");

export const listAccessRequests = (params: { status?: string; group_id?: string } = {}) =>
  api<AccessRequest[]>("GET", `/access-requests${q(params)}`);
export const requestAccess = (group_id: string, provider: Provider, justification?: string) =>
  api<AccessRequest>("POST", "/access-requests", { group_id, provider, justification });

export const listGpus = (group_id: string) => api<GpuOption[]>("GET", `/runpod/gpus${q({ group_id })}`);
export const listPods = (params: { group_id?: string; include_terminated?: boolean } = {}) =>
  api<Pod[]>("GET", `/pods${q(params)}`);
export const getPod = (id: string) => api<PodDetail>("GET", `/pods/${id}`);
export const launchPod = (body: PodLaunch) => api<Pod>("POST", "/pods", body);
export const stopPod = (id: string) => api<Pod>("POST", `/pods/${id}/stop`);
export const startPod = (id: string) => api<Pod>("POST", `/pods/${id}/start`);
export const terminatePod = (id: string) => api<void>("DELETE", `/pods/${id}`);

/** Person 3 (proposed in the contract): the key is in this response ONLY. */
export const getMyKey = (group_id: string) => api<IssuedKey>("POST", "/keys", { group_id });

export const listNotifications = () => api<Notification[]>("GET", "/notifications");
export const markNotificationRead = (id: string) => api<Notification>("POST", `/notifications/${id}/read`);

// PROPOSED, not in the contract yet (see types.ts).
export const getGroupBudget = (group_id: string) => api<GroupBudget>("GET", `/groups/${group_id}/budget`);
export const listMyKeys = () => api<KeySummary[]>("GET", "/keys");

export const getBudgetSummary = () => api<ProfessorBudgetSummary>("GET", "/budget/summary");
export const setTeamBudget = (group_id: string, policy: BudgetPolicy) =>
  api<GroupBudget>("PUT", `/groups/${group_id}/budget-policy`, policy);
export const topUpTeam = (group_id: string, amount: Money, note: string) =>
  api<GroupBudget>("POST", `/groups/${group_id}/top-ups`, { amount, note });
export const getGroup = (group_id: string) => api<GroupDetail>("GET", `/groups/${group_id}`);
export const addMember = (group_id: string, email: string, name?: string) =>
  api<Member & { invited: boolean }>("POST", `/groups/${group_id}/members`, { email, name });
export const setMemberStatus = (group_id: string, user_id: string, status: MemberStatus) =>
  api<Member>("PATCH", `/groups/${group_id}/members/${user_id}`, { status });
export const removeMember = (group_id: string, user_id: string) =>
  api<void>("DELETE", `/groups/${group_id}/members/${user_id}`);

export const listMemberBudgets = (group_id: string) =>
  api<MemberBudget[]>("GET", `/groups/${group_id}/member-budgets`);
export const setMemberLimit = (group_id: string, user_id: string, monthly_amount: Money | null) =>
  api<MemberBudget>("PUT", `/groups/${group_id}/members/${user_id}/budget-policy`, { monthly_amount });
export const approveRequest = (id: string) => api<AccessRequest>("POST", `/access-requests/${id}/approve`);
export const rejectRequest = (id: string, reason?: string) =>
  api<AccessRequest>("POST", `/access-requests/${id}/reject`, { reason });
export const revokeKey = (id: string) => api<void>("POST", `/keys/${id}/revoke`);
export const correctTeam = (group_id: string, amount: Money, note: string) =>
  api<GroupBudget>("POST", `/groups/${group_id}/corrections`, { amount, note });

// Person 3: escalations (the agent's proposals that wait for a human).
export const listEscalations = (params: { status?: EscalationStatus } = {}) =>
  api<Escalation[]>("GET", `/escalations${q(params)}`);
export const approveEscalation = (id: string) => api<Escalation>("POST", `/escalations/${id}/approve`);
export const rejectEscalation = (id: string, note?: string) =>
  api<Escalation>("POST", `/escalations/${id}/reject`, { note: note ?? "" });

// Person 3: weekly report. Without week_start the server uses the last full week (Monday to Sunday).
export const getWeeklyReport = (week_start?: string) => api<WeeklyReport>("GET", `/reports/weekly${q({ week_start })}`);

// Person 1 (C6): the department dashboard.
export const listProfessors = () => api<Professor[]>("GET", "/professors");
export const addProfessor = (email: string, name: string) =>
  api<ProfessorAdded>("POST", "/professors", { email, name });
export const removeProfessor = (id: string) => api<void>("DELETE", `/professors/${id}`);
export const setProfessorBudget = (id: string, setting: BudgetSetting) =>
  api<Professor>("PUT", `/professors/${id}/budget-setting`, setting);
export const getBillingReport = (period: string) => api<BillingReport>("GET", `/reports/billing${q({ period })}`);
/** The same report as CSV text, for the download button. */
export const getBillingCsv = (period: string) => apiText("GET", `/reports/billing${q({ period, format: "csv" })}`);
export const getSpendTrend = () => api<TrendPoint[]>("GET", "/reports/trend");
/** "Sync now": the department admin runs the usage sync without waiting for the next 5 minutes. */
export const syncNow = () => api<SyncResult>("POST", "/department/sync");
export const getSyncStatus = () => api<SyncStatus>("GET", "/department/sync");

// Person 1 (A3): provider accounts. The secret goes in once and never comes back.
export const listCredentials = () => api<Credential[]>("GET", "/credentials");
export const addCredential = (owner: OwnerType, provider: Provider, label: string, secret: string) =>
  api<Credential>("POST", "/credentials", { owner, provider, label, secret });
/** Revoke for everyone: grants end, keys issued under it are disabled, its pods are stopped. */
export const revokeCredential = (id: string) => api<void>("DELETE", `/credentials/${id}`);
export const grantCredential = (id: string, professor_id: string, ceiling_usd: Money | null) =>
  api<Grant>("POST", `/credentials/${id}/grants`, { professor_id, ceiling_usd });
export const setGrantCeiling = (id: string, grant_id: string, ceiling_usd: Money | null) =>
  api<Grant>("PATCH", `/credentials/${id}/grants/${grant_id}`, { ceiling_usd });
/** End one professor's grant: same cut-off as revoking, for him only. */
export const revokeGrant = (id: string, grant_id: string) => api<void>("DELETE", `/credentials/${id}/grants/${grant_id}`);
