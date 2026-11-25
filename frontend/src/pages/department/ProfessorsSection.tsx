import { useState } from "react";

import { Button, Notice, Tag } from "../../components/ui";
import { removeProfessor } from "../../lib/api/endpoints";
import type { BillingRow, Group, Professor, ProfessorAdded } from "../../lib/api/types";
import { budgetLevel, fraction, usd } from "../../lib/money";
import { ConfirmModal } from "../professor/modals/ConfirmModal";
import { AddProfessorModal } from "./modals/AddProfessorModal";
import { BudgetSettingModal } from "./modals/BudgetSettingModal";

/** A10/C6: the department's professors, how each is funded, and this month's spend.
 *  The department manages professors here; it never approves access or issues keys. */
export function ProfessorsSection({
  professors,
  spend,
  groups,
  onChange,
}: {
  professors: Professor[];
  spend: BillingRow[];
  groups: Group[];
  onChange: () => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Professor | null>(null);
  const [removing, setRemoving] = useState<Professor | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const byId = new Map(spend.map((r) => [r.professor_id, r]));

  const added = async (p: ProfessorAdded) => {
    setAdding(false);
    setNote(p.invited ? `${p.name} was added and invited to sign in.` : `${p.name} was added as a professor.`);
    await onChange();
  };

  return (
    <section className="group" aria-labelledby="h-professors">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-professors">Professors</h2>
          <span className="muted">How each professor is funded, and what his teams spent this month</span>
        </div>
        <div className="toolbar">
          <Button variant="primary" onClick={() => (setNote(null), setAdding(true))}>Add professor</Button>
        </div>
      </div>

      {note && <Notice kind="info">{note}</Notice>}

      {professors.length === 0 ? (
        <p className="panel-empty">No professors yet. Add one to start tracking his spend.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Professor</th>
                <th scope="col">Budget from the department</th>
                <th scope="col">Groups</th>
                <th scope="col" className="num">Spent this month</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {professors.map((p) => {
                const s = byId.get(p.id);
                const setting = p.budget_setting;
                return (
                  <tr key={p.id}>
                    <th scope="row">
                      <div className="item-title">{p.name}</div>
                      <div className="item-meta">{p.email}</div>
                    </th>
                    <td>
                      {setting.mode === "ceiling" ? (
                        <>
                          <div>{usd(setting.monthly_amount)} a month</div>
                          <CeilingUse spent={s?.total ?? "0"} ceiling={setting.monthly_amount ?? "0"} />
                          <div className="item-meta">
                            {setting.rollover
                              ? `Rollover up to ${usd(setting.rollover_cap ?? setting.monthly_amount)}`
                              : "No rollover"}
                          </div>
                        </>
                      ) : (
                        <Tag>Tracking only</Tag>
                      )}
                    </td>
                    <td>
                      {groups.filter((g) => g.professor_id === p.id).map((g) => g.name).join(", ") || (
                        <span className="muted">None yet</span>
                      )}
                    </td>
                    <td className="num">
                      <div><b>{usd(s?.total ?? "0")}</b></div>
                      <div className="item-meta">
                        {usd(s?.billable ?? "0")} billable · {usd(s?.observed ?? "0")} observed
                      </div>
                    </td>
                    <td className="actions">
                      <Button size="sm" onClick={() => setEditing(p)} aria-label={`Set budget for ${p.name}`}>
                        Set budget
                      </Button>
                      <Button size="sm" variant="quiet" onClick={() => setRemoving(p)} aria-label={`Remove ${p.name}`}>
                        Remove
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <AddProfessorModal open={adding} onClose={() => setAdding(false)} onDone={added} />

      <BudgetSettingModal
        professor={editing}
        onClose={() => setEditing(null)}
        onDone={async () => {
          setEditing(null);
          await onChange();
        }}
      />

      <ConfirmModal
        open={removing !== null}
        title={`Remove ${removing?.name ?? ""}?`}
        confirmLabel="Remove professor"
        onCancel={() => setRemoving(null)}
        onConfirm={async () => {
          if (!removing) return;
          await removeProfessor(removing.id);
          setNote(`${removing.name} is no longer a professor. His history stays in the reports.`);
          setRemoving(null);
          await onChange();
        }}
      >
        <p>
          He loses the professor role and any department account granted to him. His past spend stays in the
          billing reports. This only works once his groups have no active members, keys or pods.
        </p>
      </ConfirmModal>
    </section>
  );
}

/** How much of his monthly ceiling his teams have spent (display only; the backend enforces). */
function CeilingUse({ spent, ceiling }: { spent: string; ceiling: string }) {
  const pct = Math.round(fraction(spent, ceiling) * 100);
  const level = budgetLevel(String(Number(ceiling) - Number(spent)), ceiling);
  return (
    <div className="ceiling-use" data-level={level}>
      <div className="usage" role="meter" aria-label="Ceiling used this month" aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={pct}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <span className="item-meta">{pct}% used</span>
    </div>
  );
}
