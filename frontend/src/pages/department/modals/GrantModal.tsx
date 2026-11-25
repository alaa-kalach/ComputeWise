import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { grantCredential, setGrantCeiling } from "../../../lib/api/endpoints";
import type { Credential, Grant, Professor } from "../../../lib/api/types";

/** A3/C6: lend a department account to a professor (new grant), or change a grant's ceiling.
 *  The grant ceiling caps what he may spend on this account; it is separate from his budget. */
export function GrantModal({
  credential,
  grant,
  professors,
  onClose,
  onDone,
}: {
  credential: Credential | null;
  grant: Grant | null; // null = a new grant
  professors: Professor[];
  onClose: () => void;
  onDone: (text: string) => Promise<void>;
}) {
  const [professorId, setProfessorId] = useState("");
  const [ceiling, setCeiling] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const granted = new Set(credential?.grants.filter((g) => g.status === "active").map((g) => g.professor_id));
  const candidates = professors.filter((p) => !granted.has(p.id));

  useEffect(() => {
    if (!credential) return;
    setProfessorId(grant ? grant.professor_id : (candidates[0]?.id ?? ""));
    setCeiling(grant?.ceiling_usd ? String(Number(grant.ceiling_usd)) : "");
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credential, grant]);

  if (!credential) return null;
  const valid = !!professorId && (ceiling === "" || Number(ceiling) >= 0);

  const save = async () => {
    setBusy(true);
    setError(null);
    const amount = ceiling === "" ? null : ceiling;
    try {
      if (grant) {
        await setGrantCeiling(credential.id, grant.id, amount);
        await onDone(`${grant.professor_name}'s ceiling on ${credential.label} was updated.`);
      } else {
        const g = await grantCredential(credential.id, professorId, amount);
        await onDone(`${credential.label} was granted to ${g.professor_name}.`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save the grant");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title={grant ? `Ceiling for ${grant.professor_name}` : `Grant ${credential.label}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!valid} onClick={save}>
            {grant ? "Save" : "Grant"}
          </Button>
        </>
      }
    >
      {!grant && (
        candidates.length === 0 ? (
          <Notice kind="info">Every professor already has this account.</Notice>
        ) : (
          <div className="field">
            <label htmlFor="grant-prof">Professor</label>
            <select id="grant-prof" className="select" value={professorId} onChange={(e) => setProfessorId(e.target.value)}>
              {candidates.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <span className="hint">He can use it for his groups but never read the key.</span>
          </div>
        )
      )}
      <div className="field">
        <label htmlFor="grant-ceiling">Ceiling (USD)</label>
        <input id="grant-ceiling" className="input" type="number" min="0" step="1" value={ceiling}
          onChange={(e) => setCeiling(e.target.value)} placeholder="No ceiling" />
        <span className="hint">
          The most his teams may spend on this account. Leave empty for no ceiling. It applies even to a professor on
          tracking only.
        </span>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
    </Modal>
  );
}
