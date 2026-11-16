import type { ProfessorBudgetSummary } from "../../lib/api/types";
import { usd } from "../../lib/money";

export function SummaryRow({ summary }: { summary: ProfessorBudgetSummary }) {
  const boxes: [string, string][] =
    summary.ceiling === null
      ? [
          ["Budget from the department", "Tracking only"],
          ["Given to my teams", usd(summary.allocated)],
          ["Spent by my teams", usd(summary.spent)],
        ]
      : [
          ["My budget from the department", usd(summary.ceiling)],
          ["Given to my teams", usd(summary.allocated)],
          ["Not yet allocated", usd(summary.unallocated)],
          ["Spent by my teams", usd(summary.spent)],
        ];

  return (
    <div className="gauges">
      {boxes.map(([label, value]) => (
        <div key={label} className="gauge">
          <span className="gauge-name">{label}</span>
          <span className="gauge-value">{value}</span>
        </div>
      ))}
    </div>
  );
}