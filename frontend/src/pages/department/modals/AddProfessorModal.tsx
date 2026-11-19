import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { addProfessor } from "../../../lib/api/endpoints";
import type { ProfessorAdded } from "../../../lib/api/types";

/** A10/C6: add a professor by email. A new email gets an account and a sign-in invite. */
export function AddProfessorModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: (added: ProfessorAdded) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName("");
      setEmail("");
      setError(null);
    }
  }, [open]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await onDone(await addProfessor(email.trim(), name.trim()));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't add the professor");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title="Add a professor"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!name.trim() || !email.trim()} onClick={save}>
            Add professor
          </Button>
        </>
      }
    >
      <p>He starts as tracking only. Set a ceiling afterwards if the department funds him.</p>
      <div className="field">
        <label htmlFor="prof-name">Name</label>
        <input id="prof-name" className="input" value={name} maxLength={200}
          onChange={(e) => setName(e.target.value)} placeholder="Prof. Hana Saab" />
      </div>
      <div className="field">
        <label htmlFor="prof-email">University email</label>
        <input id="prof-email" className="input" type="email" value={email} maxLength={320}
          onChange={(e) => setEmail(e.target.value)} placeholder="hana.saab@cs.example.edu" />
        <span className="hint">Someone new gets an invite to sign in.</span>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
    </Modal>
  );
}
