// Small shared building blocks. Styles are in src/styles.css.

import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";

import { budgetLevel, fraction, usd, type BudgetLevel } from "../../lib/money";

type Variant = "primary" | "secondary" | "danger" | "quiet";

export function Button({
  variant = "secondary",
  size,
  busy,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm"; busy?: boolean }) {
  return (
    <button
      type="button"
      {...rest}
      disabled={rest.disabled || busy}
      aria-busy={busy || undefined}
      className={`btn btn-${variant}${size ? ` btn-${size}` : ""} ${rest.className ?? ""}`}
    >
      {children}
    </button>
  );
}

/** A native <dialog>: focus trap, Escape and the backdrop come for free. */
export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d || d.open) return;
    // A real modal (backdrop, focus trap, Escape). jsdom has no showModal: fall back to open.
    if (typeof d.showModal === "function") d.showModal();
    else d.setAttribute("open", "");
  }, [open]);
  if (!open) return null;
  return (
    <dialog
      ref={ref}
      className="modal"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      aria-labelledby="modal-title"
    >
      <div className="modal-head">
        <h2 id="modal-title">{title}</h2>
        <Button variant="quiet" onClick={onClose} aria-label="Close">
          Close
        </Button>
      </div>
      <div className="modal-body">{children}</div>
      {footer && <div className="modal-foot">{footer}</div>}
    </dialog>
  );
}

export function Notice({ kind, children }: { kind: "info" | "warning" | "critical" | "error"; children: ReactNode }) {
  return (
    <div className={`notice notice-${kind}`} role={kind === "error" || kind === "critical" ? "alert" : "status"}>
      {children}
    </div>
  );
}

export function Tag({ tone, busy, children }: { tone?: "ok" | "low" | "out"; busy?: boolean; children: ReactNode }) {
  return <span className={`tag${tone ? ` tag-${tone}` : ""}${busy ? " tag-busy" : ""}`}>{children}</span>;
}

const LEVEL_TEXT: Record<BudgetLevel, string> = { ok: "On track", low: "Running low", out: "Used up" };
export function BudgetTag({ remaining, funded }: { remaining: string; funded: string }) {
  const level = budgetLevel(remaining, funded);
  return <Tag tone={level}>{LEVEL_TEXT[level]}</Tag>;
}

/** Copyable text: a command, a URL, a password. */
export function CopyField({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  const [copied, setCopied] = useState(false);
  const [shown, setShown] = useState(!secret);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setShown(true); // clipboard blocked: at least let them select it
    }
  };
  return (
    <div>
      <div className="copy-label">{label}</div>
      <div className="copy">
        <code aria-label={label}>{shown ? value : "•".repeat(Math.min(value.length, 16))}</code>
        {secret && (
          <Button size="sm" variant="quiet" onClick={() => setShown((s) => !s)}>
            {shown ? "Hide" : "Show"}
          </Button>
        )}
        <Button size="sm" onClick={copy} aria-live="polite">
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

/**
 * The team's money this month: the filled part is what the team has spent. If the user has
 * his own limit inside the team budget (D9), a marker shows where his share ends.
 */
export function BudgetMeter({
  funded,
  spent,
  remaining,
  mine,
}: {
  funded: string;
  spent: string;
  remaining: string;
  mine?: { funded: string; spent: string; remaining: string } | null;
}) {
  const level = budgetLevel(remaining, funded);
  const spentPct = fraction(spent, funded) * 100;
  // Where his limit would end if he spent all of it on top of what the team has spent.
  const minePct = mine ? Math.min(100, spentPct + fraction(mine.remaining, funded) * 100) : null;
  return (
    <div className="meter" data-level={level}>
      <div
        className="meter-bar"
        role="meter"
        aria-label="Team budget used this month"
        aria-valuemin={0}
        aria-valuemax={Number(funded)}
        aria-valuenow={Number(spent)}
        aria-valuetext={`${usd(spent)} of ${usd(funded)} spent`}
      >
        <div className="meter-spent" style={{ width: `${spentPct}%` }} />
        {minePct !== null && <div className="meter-mine" style={{ left: `${minePct}%` }} title="Where your own limit ends" />}
      </div>
      <div className="meter-legend">
        <span>
          Team: <b>{usd(remaining)}</b> left of {usd(funded)}
        </span>
        {mine && (
          <span>
            <span className="legend-mark" aria-hidden="true" />
            Your limit: <b>{usd(mine.remaining)}</b> left of {usd(mine.funded)}
          </span>
        )}
      </div>
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="muted" role="status">Loading {what}…</p>;
}

// ---------------------------------------------------------------- brand and icons

/** The logo: a GPU chip with a gauge inside, compute you can measure. Drawn in currentColor. */
export function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round">
        <rect x="7" y="7" width="18" height="18" rx="3.5" />
        <path d="M11.5 19.5a4.5 4.5 0 0 1 9 0" />
        <path d="M16 19.5l2.6-3" />
        <path d="M12 3.5v3.5M20 3.5v3.5M12 25v3.5M20 25v3.5M3.5 12h3.5M3.5 20h3.5M25 12h3.5M25 20h3.5" />
      </svg>
    </span>
  );
}

/** A group's initials, e.g. "FYP Team 1" -> "F1", "Research Lab" -> "RL". */
export function Monogram({ name }: { name: string }) {
  const words = name.split(/\s+/).filter(Boolean);
  const digits = name.match(/\d+/)?.[0];
  const text = (words[0]?.[0] ?? "") + (digits ?? words[1]?.[0] ?? "");
  return (
    <span className="monogram" aria-hidden="true">
      {text.toUpperCase()}
    </span>
  );
}

const svg = { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };

/** AI models: a speech bubble with a spark. */
export function IconModels() {
  return (
    <svg {...svg} aria-hidden="true">
      <path d="M4 5h16v11H9l-5 4z" />
      <path d="M12 8v4M10 10h4" />
    </svg>
  );
}

/** GPU pods: a chip. */
export function IconGpu() {
  return (
    <svg {...svg} aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="2" />
      <rect x="9.5" y="9.5" width="5" height="5" rx="1" />
      <path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" />
    </svg>
  );
}
