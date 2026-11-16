// Typed calls, one per contract operation. Add new ones here, not inline in pages.

import { api } from "./client";
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
  ProfessorBudgetSummary,
  GroupDetail,
  Member,
  MemberBudget,
  MemberStatus
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