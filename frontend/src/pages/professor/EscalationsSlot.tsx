import { useState } from "react";

import { Button, Modal, Notice, Tag } from "../../components/ui";
import { approveEscalation, listEscalations, rejectEscalation } from "../../lib/api/endpoints";
import type { Escalation, EscalationKind } from "../../lib/api/types";
import { useAsync } from "../../lib/useAsync";

const TITLE: Record<EscalationKind, string> = {
  propose_pod_stop: "Stop a pod",
  propose_key_disable: "Disable a key",
  propose_budget_change: "Move budget between teams",
  escalate_to_department: "Needs the department",
};

const STATUS_TEXT: Record<Escalation["status"], string> = {
  pending: "Waiting for you",
  approved: "Approved",
  rejected: "Rejected",
  failed: "Approved, but it failed",
};

/** The reason is four lines: "ISSUE: ...", "EVIDENCE: ...", "PROPOSED ACTION: ...", "IF IGNORED: ...". */
function parseReason(reason: string): { label: string; text: string }[] {
  return reason
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = /^(ISSUE|EVIDENCE|PROPOSED ACTION|IF IGNORED)\s*:\s*(.*)$/i.exec(line);
      return m ? { label: sentence(m[1]), text: m[2] } : { label: "", text: line };
    });
}

function sentence(s: string): string {
  const t = s.toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** C7: the agent's proposals for the signed-in professor (or department admin). Real API only. */
export function EscalationsSlot() {
  const { data, error, reload } = useAsync(() => listEscalations(), []);
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<{ id: string; text: string } | null>(null);
  const [rejecting, setRejecting] = useState<Escalation | null>(null);

  const approve = async (e: Escalation) => {
    setBusy(e.id);
    setFailed(null);
    try {
      await approveEscalation(e.id);
    } catch (err) {
      setFailed({ id: e.id, text: err instanceof Error ? err.message : "Couldn't approve" });
    } finally {
      setBusy(null);
      await reload();
    }
  };

  const pending = (data ?? []).filter((e) => e.status === "pending");
  const decided = (data ?? []).filter((e) => e.status !== "pending").slice(0, 5);

  return (
    <section className="group" aria-labelledby="h-alerts">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-alerts">Agent alerts</h2>
          <span className="muted">Suggestions from the budget assistant that need your decision</span>
        </div>
        {pending.length > 0 && <Tag tone="low">{pending.length} waiting</Tag>}
      </div>

      {error && !data && <Notice kind="error">Couldn't load the alerts: {error.message}</Notice>}
      {data && pending.length === 0 && <p className="panel-empty">No alerts right now.</p>}

      {pending.map((e) => (
        <div key={e.id} className="item">
          <div className="row">
            <div className="grow">
              <div className="item-title">{TITLE[e.kind]}</div>
              <div className="item-meta">Suggested {shortDate(e.created_at)}</div>
            </div>
            {e.kind === "escalate_to_department" ? (
              <Button size="sm" variant="primary" busy={busy === e.id} onClick={() => approve(e)}>Got it</Button>
            ) : (
              <Button size="sm" variant="primary" busy={busy === e.id} onClick={() => approve(e)}>Approve</Button>
            )}
            <Button size="sm" onClick={() => setRejecting(e)}>Reject</Button>
          </div>
          <dl className="evidence">
            {parseReason(e.reason).map((l, i) => (
              <div key={i}>
                {l.label && <dt>{l.label}</dt>}
                <dd>{l.text}</dd>
              </div>
            ))}
          </dl>
          {failed?.id === e.id && <Notice kind="error">{failed.text}</Notice>}
        </div>
      ))}

      {decided.length > 0 && (
        <>
          <h3 className="muted">Recent decisions</h3>
          {decided.map((e) => (
            <div key={e.id} className="item">
              <div className="row">
                <div className="grow">
                  <div className="item-title">{TITLE[e.kind]}</div>
                  <div className="item-meta">
                    {e.decided_at ? shortDate(e.decided_at) : shortDate(e.created_at)}
                    {e.note && ` · "${e.note}"`}
                  </div>
                </div>
                <Tag tone={e.status === "approved" ? "ok" : e.status === "failed" ? "out" : undefined}>
                  {STATUS_TEXT[e.status]}
                </Tag>
              </div>
              {e.status === "failed" && typeof e.result?.error === "string" && (
                <Notice kind="error">{e.result.error}</Notice>
              )}
            </div>
          ))}
        </>
      )}

      <RejectEscalationModal
        escalation={rejecting}
        onClose={() => setRejecting(null)}
        onDone={async () => {
          setRejecting(null);
          await reload();
        }}
      />
    </section>
  );
}

function RejectEscalationModal({
  escalation,
  onClose,
  onDone,
}: {
  escalation: Escalation | null; // null = closed
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!escalation) return null;

  const reject = async () => {
    setBusy(true);
    setError(null);
    try {
      await rejectEscalation(escalation.id, note.trim());
      setNote("");
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't reject the alert");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title={`Reject: ${TITLE[escalation.kind]}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" busy={busy} onClick={reject}>Reject</Button>
        </>
      }
    >
      {error && <Notice kind="error">{error}</Notice>}
      <div className="field">
        <label htmlFor="reject-note">Note</label>
        <textarea id="reject-note" className="textarea" maxLength={2000} value={note}
          onChange={(e) => setNote(e.target.value)} placeholder="Why not? (optional)" />
        <span className="hint">Nothing is changed. Your note is kept with the decision.</span>
      </div>
    </Modal>
  );
}
