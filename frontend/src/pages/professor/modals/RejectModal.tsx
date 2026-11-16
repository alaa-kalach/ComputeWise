import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { rejectRequest } from "../../../lib/api/endpoints";
import type { AccessRequest } from "../../../lib/api/types";

export function RejectModal({
  request,
  onClose,
  onDone,
}: {
  request: AccessRequest | null; // null = closed
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (request) {
      setReason("");
      setError(null);
    }
  }, [request]);

  if (!request) return null;

  const reject = async () => {
    setBusy(true);
    setError(null);
    try {
      await rejectRequest(request.id, reason.trim() || undefined);
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't reject the request");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title={`Reject ${request.user_name}'s request`}
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
        <label htmlFor="reject-reason">Reason</label>
        <textarea id="reject-reason" className="textarea" maxLength={2000} value={reason}
          onChange={(e) => setReason(e.target.value)} placeholder="Use the team pod for now" />
        <span className="hint">Optional. {request.user_name} sees it on their dashboard and can ask again.</span>
      </div>
    </Modal>
  );
}