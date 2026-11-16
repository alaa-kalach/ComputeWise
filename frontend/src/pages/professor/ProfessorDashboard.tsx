import { useEffect } from "react";
import { RequestsSection } from "./RequestsSection";
import { Loading, Notice } from "../../components/ui";
import { listPods, listMyKeys, listAccessRequests, getBudgetSummary, getGroupBudget, listGroups } from "../../lib/api/endpoints";
import { useAuth } from "../../lib/auth";
import { useAsync } from "../../lib/useAsync";
import { SummaryRow } from "./SummaryRow";
import { TeamCard } from "./TeamCard";
import { ResourcesSection } from "./ResourcesSection";
import { EscalationsSlot } from "./EscalationsSlot";

export function ProfessorDashboard() {
  const { me } = useAuth();

  const { data, error, loading, reload } = useAsync(async () => {
    const [summary, groups, pending, keys, pods] = await Promise.all([
      getBudgetSummary(),
      listGroups(),
      listAccessRequests({ status: "pending" }),
      listMyKeys(),
      listPods(),
    ]);
    const mine = groups.filter((g) => g.professor_id === me?.professor_id);
    const ids = new Set(mine.map((g) => g.id));
    const budgets = await Promise.all(mine.map((g) => getGroupBudget(g.id)));
    return {
      summary,
      groups: mine,
      teams: mine.map((group, i) => ({ group, budget: budgets[i] })),
      requests: pending.filter((r) => ids.has(r.group_id)),
      keys: keys.filter((k) => ids.has(k.group_id)),
      pods: pods.filter((p) => ids.has(p.group_id)),
    };
  }, [me?.professor_id]);

  useEffect(() => {
    const onChange = () => void reload();
    window.addEventListener("mock:changed", onChange);
    return () => window.removeEventListener("mock:changed", onChange);
  }, [reload]);

  if (error && !data) return <Notice kind="error">Couldn't load your dashboard: {error.message}</Notice>;
  if (!data) return <Loading what="your teams" />;

  return (
    <>
      <div className="hero-band">
        <section className="hero" aria-busy={loading}>
          <h1>{monthName(data.summary.period)}</h1>
          <p>{syncedText(data.summary.last_synced_at)}</p>
        </section>
        <SummaryRow summary={data.summary} />
      </div>

      {data.teams.length === 0 ? (
        <p className="panel-empty">You have no teams yet. Create one to start giving budgets.</p>
      ) : (
        data.teams.map((t) => (
          <TeamCard key={t.group.id} group={t.group} budget={t.budget}
            unallocated={data.summary.unallocated} onChange={reload} />
        ))
      )}

      <EscalationsSlot />

      <RequestsSection requests={data.requests} onChange={reload} />

      <ResourcesSection teams={data.groups} keys={data.keys} pods={data.pods} onChange={reload} />
    </>
  );
}
function monthName(period: string): string {
  return new Date(`${period}-01T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

function syncedText(at: string | null): string {
  if (!at) return "Usage hasn't been synced yet.";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 60_000));
  return `Usage last synced ${minutes} min ago.`;
}