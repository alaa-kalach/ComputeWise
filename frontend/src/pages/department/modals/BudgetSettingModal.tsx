import { useEffect, useState } from "react";

import { Button, Modal, Notice } from "../../../components/ui";
import { setProfessorBudget } from "../../../lib/api/endpoints";
import type { Professor } from "../../../lib/api/types";
import { usd } from "../../../lib/money";

/** A10/C6: the department gives a professor a monthly ceiling, or only tracks his spend (D1). */
export function BudgetSettingModal({
  professor,
  onClose,
  onDone,
}: {
  professor: Professor | null;
  onClose: () => void;
  onDone: () => Promise<void>;
}) {
  const [mode, setMode] = useState<"ceiling" | "tracking">("tracking");
  const [amount, setAmount] = useState("");
  const [rollover, setRollover] = useState(true);
  const [cap, setCap] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!professor) return;
    const s = professor.budget_setting;
    setMode(s.mode);
    setAmount(s.monthly_amount ? String(Number(s.monthly_amount)) : "");
    setRollover(s.rollover);
    setCap(s.rollover_cap ? String(Number(s.rollover_cap)) : "");
    setError(null);
  }, [professor]);

  if (!professor) return null;
  const ceiling = mode === "ceiling";
  const valid = !ceiling || (amount !== "" && Number(amount) >= 0 && (cap === "" || Number(cap) >= 0));

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await setProfessorBudget(professor.id, {
        mode,
        monthly_amount: ceiling ? amount : null,
        rollover: ceiling ? rollover : true,
        rollover_cap: ceiling && rollover && cap !== "" ? cap : null,
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
      open
      title={`Budget for ${professor.name}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!valid} onClick={save}>Save</Button>
        </>
      }
    >
      <div className="segmented" role="group" aria-label="How the department funds him">
        <button type="button" aria-pressed={ceiling} onClick={() => setMode("ceiling")}>Monthly ceiling</button>
        <button type="button" aria-pressed={!ceiling} onClick={() => setMode("tracking")}>Tracking only</button>
      </div>

      {ceiling ? (
        <>
          <p>He can give his teams up to this much each month. The backend refuses a ceiling below what he has already given out.</p>
          <div className="field">
            <label htmlFor="ceiling-amount">Monthly ceiling (USD)</label>
            <input id="ceiling-amount" className="input" type="number" min="0" step="1"
              value={amount} onChange={(e) => setAmount(e.target.value)} />
          </div>
          <label className="check">
            <input type="checkbox" checked={rollover} onChange={(e) => setRollover(e.target.checked)} />
            Carry unused budget into next month
          </label>
          {rollover && (
            <div className="field">
              <label htmlFor="ceiling-cap">Most that can carry over (USD)</label>
              <input id="ceiling-cap" className="input" type="number" min="0" step="1"
                value={cap} onChange={(e) => setCap(e.target.value)} placeholder={amount ? String(Number(amount)) : ""} />
              <span className="hint">
                Leave empty for the default: one month's amount{amount ? ` (${usd(amount)})` : ""}.
              </span>
            </div>
          )}
        </>
      ) : (
        <p>
          The department records his spend but sets no limit. He is still limited by the ceiling on any
          department account he was granted.
        </p>
      )}
      {error && <Notice kind="error">{error}</Notice>}
    </Modal>
  );
}
