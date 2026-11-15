import type { AccessRequest, GroupBudget, KeySummary, Membership, Pod, Provider } from "../../lib/api/types";

/** Everything the dashboard shows for one of the student's groups. */
export interface GroupData {
  membership: Membership;
  budget: GroupBudget;
  requests: AccessRequest[];
  keys: KeySummary[];
  pods: Pod[];
}

/** The newest request for this provider in this group (what the panel should reflect). */
export function latestRequest(requests: AccessRequest[], provider: Provider): AccessRequest | undefined {
  return requests.filter((r) => r.provider === provider).sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
}

export function hasApproved(requests: AccessRequest[], provider: Provider): boolean {
  return requests.some((r) => r.provider === provider && r.status === "approved");
}
