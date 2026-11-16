import { useEffect } from "react";

import { Loading, Notice } from "../../components/ui";
import { getBudgetSummary, getGroupBudget, listGroups } from "../../lib/api/endpoints";
import { useAuth } from "../../lib/auth";
import { useAsync } from "../../lib/useAsync";
import { SummaryRow } from "./SummaryRow";
import { TeamCard } from "./TeamCard";

export function ProfessorDashboard() {
  const { me } = useAuth();

  const { data, error, loading, reload } = useAsync(async () => {
    const [summary, groups] = await Promise.all([getBudgetSummary(), listGroups()]);
    const mine = groups.filter((g) => g.professor_id === me?.professor_id);
    const budgets = await Promise.all(mine.map((g) => getGroupBudget(g.id)));
    return { summary, teams: mine.map((group, i) => ({ group, budget: budgets[i] })) };
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