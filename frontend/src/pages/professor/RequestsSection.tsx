import { useState } from "react";

import { Button, Notice, Tag } from "../../components/ui";
import { approveRequest } from "../../lib/api/endpoints";
import type { AccessRequest, Provider } from "../../lib/api/types";
import { RejectModal } from "./modals/RejectModal";

const WHAT: Record<Provider, string> = { openrouter: "OpenRouter key", runpod: "GPU pods (RunPod)" };

export function RequestsSection({
  requests,
  onChange,
}: {
  requests: AccessRequest[];
  onChange: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ where: string; text: string } | null>(null);
  const [rejecting, setRejecting] = useState<AccessRequest | null>(null);

  const approve = async (r: AccessRequest) => {
    setBusy(r.id);
    setError(null);
    try {
      await approveRequest(r.id);
      await onChange();
    } catch (e) {
      setError({ where: r.id, text: e instanceof Error ? e.message : "Couldn't approve" });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="group" aria-labelledby="h-requests">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-requests">Access requests</h2>
          <span className="muted">Students asking for an OpenRouter key or GPU pods</span>
        </div>
        {requests.length > 0 && <Tag tone="low">{requests.length} waiting</Tag>}
      </div>

      {requests.length === 0 ? (
        <p className="panel-empty">No pending requests.</p>
      ) : (
        requests.map((r) => (
          <div key={r.id} className="item">
            <div className="row">
              <div className="grow">
                <div className="item-title">
                  {r.user_name} · {r.group_name}
                </div>
                <div className="item-meta">
                  {WHAT[r.provider]} · asked {shortDate(r.created_at)}
                  {r.justification && ` · "${r.justification}"`}
                </div>
              </div>
              <Button size="sm" variant="primary" busy={busy === r.id} onClick={() => approve(r)}>
                Approve
              </Button>
              <Button size="sm" onClick={() => setRejecting(r)}>
                Reject
              </Button>
            </div>
            {error?.where === r.id && <Notice kind="error">{error.text}</Notice>}
          </div>
        ))
      )}

      <RejectModal
        request={rejecting}
        onClose={() => setRejecting(null)}
        onDone={async () => {
          setRejecting(null);
          await onChange();
        }}
      />
    </section>
  );
}

function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}