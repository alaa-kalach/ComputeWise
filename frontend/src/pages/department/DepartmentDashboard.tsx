import { useEffect, useState } from "react";

import { Button, Loading, Notice } from "../../components/ui";
import {
  getBillingReport,
  getSpendTrend,
  getSyncStatus,
  listCredentials,
  listGroups,
  listProfessors,
  syncNow,
} from "../../lib/api/endpoints";
import type { SyncResult } from "../../lib/api/types";
import { usd } from "../../lib/money";
import { useAsync } from "../../lib/useAsync";
import { EscalationsSlot } from "../professor/EscalationsSlot";
import { WeeklyReport } from "../reports/WeeklyReport";
import { BillingSection } from "./BillingSection";
import { CredentialsSection } from "./CredentialsSection";
import { ProfessorsSection } from "./ProfessorsSection";
import { SpendChart } from "./SpendChart";

const thisPeriod = () => new Date().toISOString().slice(0, 7);

function monthName(period: string): string {
  return new Date(`${period}-01T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

function syncedAgo(at: string | null): string {
  if (!at) return "usage hasn't been synced yet";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 60_000));
  if (minutes < 1) return "usage synced just now";
  if (minutes < 120) return `usage last synced ${minutes} min ago`;
  return `usage last synced ${new Date(at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
}

function syncText(r: SyncResult): string {
  if (r.skipped) return "A scheduled sync was already running, so nothing new was read. Try again in a minute.";
  const entries = r.spend_entries === 1 ? "1 new spend entry" : `${r.spend_entries} new spend entries`;
  const failed = r.errors > 0 ? ` ${r.errors} provider account${r.errors === 1 ? "" : "s"} did not answer; they are retried at the next sync.` : "";
  return `Synced: read ${r.resources_polled} keys and pods, recorded ${entries}.${failed}`;
}

/** C6: the department observes and bills. It manages professors and their budgets, but never
 *  approves access, issues keys or controls pods (that is the professor's job). */
export function DepartmentDashboard() {
  const period = thisPeriod();
  const { data, error, loading, reload } = useAsync(async () => {
    const [professors, trend, billing, credentials, groups, sync] = await Promise.all([
      listProfessors(), getSpendTrend(), getBillingReport(period), listCredentials(), listGroups(), getSyncStatus(),
    ]);
    return { professors, trend, billing, credentials, groups, sync };
  }, [period]);
  const [syncing, setSyncing] = useState(false);
  const [synced, setSynced] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const [version, setVersion] = useState(0); // remounts the report sections after a sync

  useEffect(() => {
    const onChange = () => void reload();
    window.addEventListener("mock:changed", onChange);
    return () => window.removeEventListener("mock:changed", onChange);
  }, [reload]);

  const sync = async () => {
    setSyncing(true);
    setSynced(null);
    try {
      const r = await syncNow();
      setSynced({ kind: "info", text: syncText(r) });
      await reload();
      setVersion((v) => v + 1);
    } catch (e) {
      setSynced({ kind: "error", text: e instanceof Error ? e.message : "The sync failed" });
    } finally {
      setSyncing(false);
    }
  };

  if (error && !data) return <Notice kind="error">Couldn't load the department dashboard: {error.message}</Notice>;
  if (!data) return <Loading what="the department" />;

  const t = data.billing.totals;
  const periods = [...new Set([...data.trend.map((p) => p.period), period])].sort();

  return (
    <>
      <div className="hero-band">
        <section className="hero hero-row" aria-busy={loading}>
          <div>
            <h1>Department dashboard</h1>
            <p>
              {monthName(period)} · {data.professors.length} professor{data.professors.length === 1 ? "" : "s"} ·{" "}
              {syncedAgo(data.sync.last_synced_at)}
            </p>
          </div>
          <Button className="btn-onbrand" busy={syncing} onClick={sync}>
            {syncing ? "Syncing…" : "Sync now"}
          </Button>
        </section>
        <div className="gauges gauges-fill">
          {([
            ["Spent this month", usd(t.total)],
            ["Billable to professors", usd(t.billable)],
            ["Observed (own accounts)", usd(t.observed)],
          ] as const).map(([label, value]) => (
            <div key={label} className="gauge">
              <span className="gauge-name">{label}</span>
              <span className="gauge-value">{value}</span>
            </div>
          ))}
          <div className="gauge">
            <span className="gauge-name">By platform</span>
            <span className="gauge-split">
              <span>RunPod</span><b>{usd(t.runpod)}</b>
              <span>OpenRouter</span><b>{usd(t.openrouter)}</b>
            </span>
          </div>
        </div>
        {synced && (
          <div className="notices">
            <Notice kind={synced.kind}>{synced.text}</Notice>
          </div>
        )}
      </div>

      <EscalationsSlot key={`esc-${version}`} />

      <section className="group" aria-labelledby="h-trend">
        <div className="group-head">
          <div className="group-title">
            <h2 id="h-trend">Spend over time</h2>
            <span className="muted">All professors, per month, by who pays</span>
          </div>
        </div>
        <SpendChart points={data.trend} />
      </section>

      <ProfessorsSection professors={data.professors} spend={data.billing.rows} groups={data.groups} onChange={reload} />

      <CredentialsSection credentials={data.credentials} professors={data.professors} onChange={reload} />

      <BillingSection key={`bill-${version}`} periods={periods} />

      <WeeklyReport key={`week-${version}`} />
    </>
  );
}
