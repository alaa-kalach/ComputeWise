import { useState } from "react";

import { Button, Loading, Notice } from "../../components/ui";
import { getBillingCsv, getBillingReport } from "../../lib/api/endpoints";
import type { BillingRow } from "../../lib/api/types";
import { usd } from "../../lib/money";
import { useAsync } from "../../lib/useAsync";

function monthName(period: string): string {
  return new Date(`${period}-01T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

/** Save text as a file in the browser. */
function download(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** A7/C6: the monthly billing report per professor, and its CSV download. */
export function BillingSection({ periods }: { periods: string[] }) {
  const [period, setPeriod] = useState(periods[periods.length - 1]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { data, error: loadError } = useAsync(() => getBillingReport(period), [period]);

  const csv = async () => {
    setBusy(true);
    setError(null);
    try {
      download(`billing-${period}.csv`, await getBillingCsv(period));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't download the report");
    } finally {
      setBusy(false);
    }
  };

  const cells = (r: BillingRow) => (
    <>
      <td className="num">{usd(r.billable)}</td>
      <td className="num">{usd(r.observed)}</td>
      <td className="num">{usd(r.unassigned)}</td>
      <td className="num">{usd(r.openrouter)}</td>
      <td className="num">{usd(r.runpod)}</td>
      <td className="num"><b>{usd(r.total)}</b></td>
    </>
  );

  return (
    <section className="group" aria-labelledby="h-billing">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-billing">Billing report</h2>
          <span className="muted">Billable spend is charged back to the professor; observed spend is only monitored</span>
        </div>
        <div className="toolbar">
          <label className="sr-only" htmlFor="billing-period">Month</label>
          <select id="billing-period" className="select" value={period} onChange={(e) => setPeriod(e.target.value)}>
            {[...periods].reverse().map((p) => <option key={p} value={p}>{monthName(p)}</option>)}
          </select>
          <Button busy={busy} onClick={csv}>Download CSV</Button>
        </div>
      </div>

      {error && <Notice kind="error">{error}</Notice>}
      {loadError && !data && <Notice kind="error">Couldn't load the billing report: {loadError.message}</Notice>}
      {!data && !loadError && <Loading what="the billing report" />}

      {data && (data.rows.length === 0 ? (
        <p className="panel-empty">No spend was recorded in {monthName(data.period)}.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <caption className="sr-only">Billing report for {monthName(data.period)}</caption>
            <thead>
              <tr>
                <th scope="col">Professor</th>
                <th scope="col" className="num">Billable</th>
                <th scope="col" className="num">Observed</th>
                <th scope="col" className="num">Unassigned</th>
                <th scope="col" className="num">OpenRouter</th>
                <th scope="col" className="num">RunPod</th>
                <th scope="col" className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.professor_id}>
                  <th scope="row">{r.professor_name || "Unknown professor"}</th>
                  {cells(r)}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row">Total</th>
                {cells(data.totals)}
              </tr>
            </tfoot>
          </table>
        </div>
      ))}
    </section>
  );
}
