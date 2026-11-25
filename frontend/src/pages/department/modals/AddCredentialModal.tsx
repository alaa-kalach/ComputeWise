import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { addCredential } from "../../../lib/api/endpoints";
import type { Provider } from "../../../lib/api/types";

const KEY_NAME: Record<Provider, string> = { openrouter: "management key", runpod: "API key" };

/** A3/C6: the department adds one of its own provider accounts. The key is checked with the
 *  provider, stored encrypted, and never shown again, not even to the department. */
export function AddCredentialModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: (label: string) => Promise<void>;
}) {
  const [provider, setProvider] = useState<Provider>("openrouter");
  const [label, setLabel] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setProvider("openrouter");
      setLabel("");
      setSecret("");
      setError(null);
    }
  }, [open]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const c = await addCredential("department", provider, label.trim(), secret.trim());
      setSecret(""); // don't keep the key in memory longer than needed
      await onDone(c.label);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't add the account");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title="Add a department account"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!label.trim() || !secret.trim()} onClick={save}>
            Add account
          </Button>
        </>
      }
    >
      <p>
        The department pays for what professors spend on it, and bills them for it. Grant it to the professors
        who should use it.
      </p>
      <div className="segmented" role="group" aria-label="Platform">
        <button type="button" aria-pressed={provider === "openrouter"} onClick={() => setProvider("openrouter")}>OpenRouter</button>
        <button type="button" aria-pressed={provider === "runpod"} onClick={() => setProvider("runpod")}>RunPod</button>
      </div>
      <div className="field">
        <label htmlFor="cred-label">Name</label>
        <input id="cred-label" className="input" value={label} maxLength={200}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={provider === "openrouter" ? "CS Department OpenRouter" : "CS Department RunPod"} />
      </div>
      <div className="field">
        <label htmlFor="cred-secret">{provider === "openrouter" ? "OpenRouter management key" : "RunPod API key"}</label>
        <input id="cred-secret" className="input mono" type="password" autoComplete="off" spellCheck={false}
          value={secret} onChange={(e) => setSecret(e.target.value)} />
        <span className="hint">
          {provider === "openrouter"
            ? "A management key: it can create and limit keys, not call models."
            : "RunPod gives this key access to the whole account, so students never see it."}{" "}
          It is checked with {provider === "openrouter" ? "OpenRouter" : "RunPod"}, stored encrypted, and never shown again.
        </span>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      <p className="item-meta">Paste the {KEY_NAME[provider]} itself; nobody, the department included, can read it back later.</p>
    </Modal>
  );
}
