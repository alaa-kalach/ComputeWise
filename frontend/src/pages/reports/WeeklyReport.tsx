import { Loading, Notice, Tag } from "../../components/ui";
import { getWeeklyReport, listGroups } from "../../lib/api/endpoints";
import { usd } from "../../lib/money";
import { useAsync } from "../../lib/useAsync";

/** "+12.5%", "-8%", or a plain sentence when last week had no spend to compare with. */
function change(pct: number | null): string {
  if (pct === null) return "no spend the week before";
  const rounded = Math.round(pct * 10) / 10;
  return `${rounded > 0 ? "+" : ""}${rounded}% vs the week before`;
}

function weekRange(start: string): string {
  const from = new Date(`${start}T12:00:00`);
  const to = new Date(from);
  to.setDate(to.getDate() + 6);
  const fmt = (d: Date) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return `${fmt(from)} to ${fmt(to)}`;
}

const STATUS_ORDER = ["pending", "approved", "rejected", "failed"] as const;
const STATUS_TEXT: Record<(typeof STATUS_ORDER)[number], string> = {
  pending: "waiting",
  approved: "approved",
  rejected: "rejected",
  failed: "failed",
};

/** C7: the weekly report, this week against the week before. A professor sees his own row, the department sees all. */
export function WeeklyReport() {
  const { data, error } = useAsync(async () => {
    const [report, groups] = await Promise.all([getWeeklyReport(), listGroups()]);
    return { report, names: new Map(groups.map((g) => [g.id, g.name])) };
  }, []);

  if (error && !data) return <Notice kind="error">Couldn't load the weekly report: {error.message}</Notice>;
  if (!data) return <Loading what="the weekly report" />;

  const { report, names } = data;
  const counts = STATUS_ORDER.filter((s) => (report.escalations[s] ?? 0) > 0);

  return (
    <section className="group" aria-labelledby="h-weekly">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-weekly">Last week</h2>
          <span className="muted">{weekRange(report.week_start)}</span>
        </div>
        <Tag>{usd(report.totals.this_week_usd)} spent</Tag>
      </div>
      <p className="item-meta">{change(report.totals.change_pct)}</p>

      {report.rows.length === 0 && <p className="panel-empty">No spend recorded for this week.</p>}

      {report.rows.map((p) => (
        <div key={p.professor_id} className="item">
          <div className="row">
            <div className="grow">
              <div className="item-title">{p.professor_name}</div>
              <div className="item-meta">
                {usd(p.this_week_usd)} · {change(p.change_pct)}
              </div>
            </div>
            <div className="item-meta">
              Billed {usd(p.billable_usd)} · Watched {usd(p.observed_usd)}
            </div>
          </div>
          {p.groups.map((g, i) => (
            <div key={g.group_id ?? i} className="row">
              <div className="grow item-meta">{(g.group_id && names.get(g.group_id)) || "Unassigned"}</div>
              <div className="item-meta">
                {usd(g.this_week_usd)} · {change(g.change_pct)}
              </div>
            </div>
          ))}
        </div>
      ))}

      <p className="item-meta">
        {counts.length === 0
          ? "No agent alerts this week."
          : `Agent alerts this week: ${counts.map((s) => `${report.escalations[s]} ${STATUS_TEXT[s]}`).join(", ")}.`}
      </p>
    </section>
  );
}
