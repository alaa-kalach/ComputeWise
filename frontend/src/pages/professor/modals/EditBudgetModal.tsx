import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { setTeamBudget } from "../../../lib/api/endpoints";
import type { BudgetPolicy, Group, Money } from "../../../lib/api/types";
import { usd } from "../../../lib/money";

export function EditBudgetModal({
  open,
  group,
  policy,
  unallocated,
  onClose,
  onDone,
}: {
  open: boolean;
  group: Group;
  policy: BudgetPolicy | null;
  unallocated: Money | null;
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const [monthly, setMonthly] = useState("");
  const [rollover, setRollover] = useState(true);
  const [cap, setCap] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fill the form with the team's current settings each time it opens.
  useEffect(() => {
    if (open) {
      setMonthly(policy ? String(Number(policy.monthly_amount)) : "");
      setRollover(policy?.rollover ?? true);
      setCap(policy?.rollover_cap ? String(Number(policy.rollover_cap)) : "");
      setError(null);
    }
  }, [open, policy]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await setTeamBudget(group.id, {
        monthly_amount: monthly,
        rollover,
        rollover_cap: rollover && cap ? cap : null,
      });
      await onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save the budget");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={`Budget for ${group.name}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={monthly === ""} onClick={save}>
            Save
          </Button>
        </>
      }
    >
      {error && <Notice kind="error">{error}</Notice>}
      <div className="field">
        <label htmlFor="budget-monthly">Monthly budget (USD)</label>
        <input id="budget-monthly" className="input" type="number" min="0" step="0.01"
          value={monthly} onChange={(e) => setMonthly(e.target.value)} />
        <span className="hint">
          Applies this month right away: the difference is added or removed now.
          {unallocated !== null && ` You have ${usd(unallocated)} not yet allocated.`}
        </span>
      </div>
      <label className="row">
        <input type="checkbox" checked={rollover} onChange={(e) => setRollover(e.target.checked)} />
        Carry unused money into next month
      </label>
      {rollover && (
        <div className="field">
          <label htmlFor="budget-cap">Most that can carry over (USD)</label>
          <input id="budget-cap" className="input" type="number" min="0" step="0.01"
            value={cap} onChange={(e) => setCap(e.target.value)} placeholder="Same as the monthly budget" />
          <span className="hint">Leave empty for the default: one month's budget.</span>
        </div>
      )}
    </Modal>
  );
}