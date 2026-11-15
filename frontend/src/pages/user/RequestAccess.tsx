// "Request access" and the states a request goes through, for one provider in one group.

import { useState } from "react";

import { Button, Modal, Notice, Tag } from "../../components/ui";
import { requestAccess } from "../../lib/api/endpoints";
import type { AccessRequest, Provider } from "../../lib/api/types";

const WHAT: Record<Provider, string> = { openrouter: "access to AI models", runpod: "access to GPU pods" };

/** Shown instead of the panel's content while access isn't approved. */
export function AccessState({
  provider,
  groupId,
  groupName,
  request,
  onChange,
}: {
  provider: Provider;
  groupId: string;
  groupName: string;
  request: AccessRequest | undefined;
  onChange: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);

  if (request?.status === "pending") {
    return (
      <div className="panel-body">
        <div className="row">
          <Tag busy>Waiting for your professor</Tag>
        </div>
        <p className="panel-empty">You asked on {new Date(request.created_at).toLocaleDateString()}.</p>
      </div>
    );
  }

  return (
    <div className="panel-body">
      {request?.status === "rejected" ? (
        <Notice kind="warning">
          Your professor declined your request{request.reason ? `: "${request.reason}".` : "."} You can ask again.
        </Notice>
      ) : (
        <p className="panel-empty">You don't have {WHAT[provider]} in this group yet.</p>
      )}
      <div>
        <Button variant="primary" onClick={() => setOpen(true)}>
          {request?.status === "rejected" ? "Ask again" : "Request access"}
        </Button>
      </div>
      <RequestDialog
        open={open}
        provider={provider}
        groupId={groupId}
        groupName={groupName}
        onClose={() => setOpen(false)}
        onSent={async () => {
          setOpen(false);
          await onChange();
        }}
      />
    </div>
  );
}

function RequestDialog({
  open,
  provider,
  groupId,
  groupName,
  onClose,
  onSent,
}: {
  open: boolean;
  provider: Provider;
  groupId: string;
  groupName: string;
  onClose: () => void;
  onSent: () => Promise<void>;
}) {
  const [why, setWhy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async () => {
    setBusy(true);
    setError(null);
    try {
      await requestAccess(groupId, provider, why.trim() || undefined);
      setWhy("");
      await onSent();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't send the request");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`Request ${WHAT[provider]}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={send}>
            Send request
          </Button>
        </>
      }
    >
      <p>Your professor for {groupName} will see this and approve or decline it.</p>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="field">
        <label htmlFor="why">What do you need it for?</label>
        <textarea id="why" className="textarea" maxLength={2000} value={why} onChange={(e) => setWhy(e.target.value)}
          placeholder={provider === "runpod" ? "Fine-tuning a small vision model for the FYP" : "Calling GPT models from our chatbot"} />
        <span className="hint">Optional, but it helps your professor decide.</span>
      </div>
    </Modal>
  );
}
