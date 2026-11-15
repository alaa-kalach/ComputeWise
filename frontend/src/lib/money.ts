// Money arrives as a decimal string ("12.5000"). These helpers only FORMAT it for display;
// the backend does every calculation that matters.

const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 });
const usd4 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

/** "$12.50". Rates below a cent keep their digits ("$0.0027") so they don't read as $0.00. */
export function usd(amount: string | null | undefined): string {
  if (amount === null || amount === undefined || amount === "") return "–";
  const n = Number(amount);
  if (!Number.isFinite(n)) return "–";
  return Math.abs(n) > 0 && Math.abs(n) < 0.01 ? usd4.format(n) : usd2.format(n);
}

/** "$0.17/h" */
export function perHour(amount: string): string {
  return `${usd(amount)}/h`;
}

/** Share of a budget used, 0..1 (display only). */
export function fraction(part: string, whole: string): number {
  const p = Number(part);
  const w = Number(whole);
  if (!Number.isFinite(p) || !Number.isFinite(w) || w <= 0) return 0;
  return Math.min(1, Math.max(0, p / w));
}

export type BudgetLevel = "ok" | "low" | "out";

/** Same thresholds as the agent's warnings: low from 80% used, out when nothing is left. */
export function budgetLevel(remaining: string, funded: string): BudgetLevel {
  if (Number(remaining) <= 0) return "out";
  return fraction(remaining, funded) <= 0.2 ? "low" : "ok";
}
