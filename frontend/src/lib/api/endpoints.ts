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
