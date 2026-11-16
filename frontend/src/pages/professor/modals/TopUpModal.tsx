import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { topUpTeam } from "../../../lib/api/endpoints";
import type { Group, Money } from "../../../lib/api/types";
import { usd } from "../../../lib/money";

export function TopUpModal({
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
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setAmount("");
      setNote("");
      setError(null);
    }
  }, [open]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await topUpTeam(group.id, amount, note.trim());
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't top up");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`Top up ${group.name}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!amount || !note.trim()} onClick={save}>
            Add money
          </Button>
        </>
      }
    >
      <p>Adds money to this month only. A team that ran out is reopened at the next sync.</p>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="field">
        <label htmlFor="topup-amount">Amount (USD)</label>
        <input id="topup-amount" className="input" type="number" min="0" step="0.01"
          value={amount} onChange={(e) => setAmount(e.target.value)} />
        {unallocated !== null && <span className="hint">You have {usd(unallocated)} left to give.</span>}
      </div>
      <div className="field">
        <label htmlFor="topup-note">Reason</label>
        <textarea id="topup-note" className="textarea" maxLength={500} value={note}
          onChange={(e) => setNote(e.target.value)} placeholder="Extra GPU time for the demo week" />
        <span className="hint">Saved with the entry, so the department can see why.</span>
      </div>
    </Modal>
  );
}