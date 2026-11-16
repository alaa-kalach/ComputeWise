import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { correctTeam } from "../../../lib/api/endpoints";
import type { Group, Money } from "../../../lib/api/types";
import { usd } from "../../../lib/money";

export function CorrectionModal({
  open,
  group,
  unallocated,
  onClose,
  onDone,
}: {
  open: boolean;
  group: Group;
  unallocated: Money | null;
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const [direction, setDirection] = useState<"add" | "remove">("remove");
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setDirection("remove");
      setAmount("");
      setNote("");
      setError(null);
    }
  }, [open]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await correctTeam(group.id, direction === "remove" ? `-${amount}` : amount, note.trim());
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save the correction");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`Adjust ${group.name}'s balance`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!amount || Number(amount) <= 0 || !note.trim()} onClick={save}>
            Save adjustment
          </Button>
        </>
      }
    >
      <p>
        The history is never edited: this adds a correcting entry, so the department can still see both the mistake and
        the fix.
      </p>

      <div className="row" role="radiogroup" aria-label="Direction">
        <label className="row">
          <input type="radio" name="direction" checked={direction === "remove"} onChange={() => setDirection("remove")} />
          Remove money
        </label>
        <label className="row">
          <input type="radio" name="direction" checked={direction === "add"} onChange={() => setDirection("add")} />
          Add money
        </label>
      </div>

      <div className="field">
        <label htmlFor="correct-amount">Amount (USD)</label>
        <input id="correct-amount" className="input" type="number" min="0" step="0.01"
          value={amount} onChange={(e) => setAmount(e.target.value)} />
        {direction === "add" && unallocated !== null && (
          <span className="hint">You have {usd(unallocated)} left to give.</span>
        )}
      </div>

      <div className="field">
        <label htmlFor="correct-note">Reason</label>
        <textarea id="correct-note" className="textarea" maxLength={500} value={note}
          onChange={(e) => setNote(e.target.value)} placeholder="Top-up was $50 instead of $5" />
      </div>

      {error && <Notice kind="error">{error}</Notice>}
    </Modal>
  );
}