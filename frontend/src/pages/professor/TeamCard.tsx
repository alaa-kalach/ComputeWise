import { useState } from "react";
import { MembersModal } from "./modals/MembersModal";
import { BudgetMeter, BudgetTag, Button, Monogram, Notice } from "../../components/ui";
import type { Group, GroupBudget, Money } from "../../lib/api/types";
import { budgetLevel, usd } from "../../lib/money";
import { EditBudgetModal } from "./modals/EditBudgetModal";
import { TopUpModal } from "./modals/TopUpModal";

export function TeamCard({
  group,
  budget,
  unallocated,
  onChange,
}: {
  group: Group;
  budget: GroupBudget;
  unallocated: Money | null;
  onChange: () => Promise<void>;
}) {
  const [open, setOpen] = useState<"topup" | "edit" | "members" | null>(null);
  const done = async () => {
    setOpen(null);
    await onChange();
  };
  const { team, by_provider, funding } = budget;
  const out = budgetLevel(team.remaining, team.funded) === "out";

  return (
    <section className="group" aria-labelledby={`h-${group.id}`}>
      <div className="group-head">
        <Monogram name={group.name} />
        <div className="group-title">
          <h2 id={`h-${group.id}`}>{group.name}</h2>
          <span className="muted">{policyText(budget)}</span>
        </div>
        <BudgetTag remaining={team.remaining} funded={team.funded} />
      </div>

      <BudgetMeter {...team} />

      <p className="muted" style={{ marginBottom: "var(--s3)" }}>
        {usd(by_provider.openrouter)} on models · {usd(by_provider.runpod)} on GPU pods
        {funding && ` · Paid by ${funding.label} (${funding.billable ? "billable" : "observed"})`}
      </p>

      {out && (
        <Notice kind="critical">
          Budget used up: keys are capped and pods are stopped. Top up to reopen the team.
        </Notice>
      )}

      <div className="row" style={{ marginTop: "var(--s3)" }}>
        <Button variant={out ? "primary" : "secondary"} onClick={() => setOpen("topup")}>Top up</Button>
        <Button onClick={() => setOpen("edit")}>Edit budget</Button>
        <Button onClick={() => setOpen("members")}>Members</Button>
      </div>

      <TopUpModal open={open === "topup"} group={group} unallocated={unallocated}
        onClose={() => setOpen(null)} onDone={done} />
      <EditBudgetModal open={open === "edit"} group={group} policy={budget.policy} unallocated={unallocated}
        onClose={() => setOpen(null)} onDone={done} />
      <MembersModal open={open === "members"} group={group}
        onClose={() => setOpen(null)} onChange={onChange} />
    </section>
  );
}

function policyText(b: GroupBudget): string {
  if (!b.policy) return "No monthly budget set yet";
  let text = `${usd(b.policy.monthly_amount)} monthly`;
  if (Number(b.rolled_over) > 0) text += ` + ${usd(b.rolled_over)} rolled over from last month`;
  if (!b.policy.rollover) text += " · no rollover";
  else if (Number(b.rolled_over) <= 0) text += " · rollover on";
  return text;
}