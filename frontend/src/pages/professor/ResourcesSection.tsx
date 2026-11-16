import { useState } from "react";

import { Button, Notice } from "../../components/ui";
import { revokeKey, startPod, stopPod, terminatePod } from "../../lib/api/endpoints";
import type { Group, KeySummary, Pod } from "../../lib/api/types";
import { perHour, usd } from "../../lib/money";
import { ConfirmModal } from "./modals/ConfirmModal";

type Row =
  | { kind: "key"; id: string; group_id: string; student: string; key: KeySummary }
  | { kind: "pod"; id: string; group_id: string; student: string; pod: Pod };

interface Confirm {
  title: string;
  text: string;
  label: string;
  action: () => Promise<unknown>;
}

const SELECT_STYLE = {
  marginLeft: "auto",
  maxWidth: 200,
  cursor: "pointer",
  appearance: "none" as const,
  paddingRight: 36,
  backgroundImage:
    "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M2 4l4 4 4-4' fill='none' stroke='%23999' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E\")",
  backgroundRepeat: "no-repeat",
  backgroundPosition: "right 14px center",
};

export function ResourcesSection({
  teams,
  keys,
  pods,
  onChange,
}: {
  teams: Group[];
  keys: KeySummary[];
  pods: Pod[];
  onChange: () => Promise<void>;
}) {
  const [team, setTeam] = useState("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ where: string; text: string } | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);

  const teamName = (id: string) => teams.find((t) => t.id === id)?.name ?? "";

  const rows: Row[] = [
    ...keys.map((k) => ({ kind: "key" as const, id: k.id, group_id: k.group_id, student: k.user_name, key: k })),
    ...pods.map((p) => ({ kind: "pod" as const, id: p.id, group_id: p.group_id, student: p.user_name, pod: p })),
  ]
    .filter((r) => team === "all" || r.group_id === team)
    .sort((a, b) => teamName(a.group_id).localeCompare(teamName(b.group_id)) || a.student.localeCompare(b.student));

  // Stop and Start run right away; their errors show under the row.
  const run = async (id: string, action: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await action();
      await onChange();
    } catch (e) {
      setError({ where: id, text: e instanceof Error ? e.message : "Something went wrong" });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="group" aria-labelledby="h-resources">
      <div className="group-head">
        <div className="group-title">
          <h2 id="h-resources">Keys and pods</h2>
          <span className="muted">What your students are using this month</span>
        </div>
        {teams.length > 1 && (
          <select className="select" aria-label="Filter by team" value={team}
            onChange={(e) => setTeam(e.target.value)} style={SELECT_STYLE}>
            <option value="all">All teams</option>
            {teams.map((t) => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
        )}
      </div>

      {rows.length === 0 ? (
        <p className="panel-empty">No keys or pods yet.</p>
      ) : (
        rows.map((r) => (
          <div key={`${r.kind}-${r.id}`} className="item">
            <div className="row">
              <div className="grow">
                <div className="item-title">{r.student} · {teamName(r.group_id)}</div>
                <div className="item-meta">{r.kind === "key" ? keyText(r.key) : podText(r.pod)}</div>
              </div>

              {r.kind === "key" && r.key.status !== "disabled" && (
                <Button size="sm" variant="danger" onClick={() => setConfirm({
                  title: `Revoke ${r.student}'s key?`,
                  text: "The key stops working right away and can't be turned back on.",
                  label: "Revoke",
                  action: () => revokeKey(r.id),
                })}>Revoke</Button>
              )}

              {r.kind === "pod" && (r.pod.state === "running" || r.pod.state === "starting") && (
                <Button size="sm" busy={busy === r.id} onClick={() => run(r.id, () => stopPod(r.id))}>Stop</Button>
              )}

              {r.kind === "pod" && r.pod.state === "stopped" && (
                <>
                  <Button size="sm" busy={busy === r.id} onClick={() => run(r.id, () => startPod(r.id))}>Start</Button>
                  <Button size="sm" variant="danger" onClick={() => setConfirm({
                    title: `Terminate ${r.student}'s pod?`,
                    text: "This deletes the pod and everything saved on its disk. It can't be undone.",
                    label: "Terminate",
                    action: () => terminatePod(r.id),
                  })}>Terminate</Button>
                </>
              )}
            </div>
            {error?.where === r.id && <Notice kind="error">{error.text}</Notice>}
          </div>
        ))
      )}

      <ConfirmModal
        open={confirm !== null}
        title={confirm?.title ?? ""}
        confirmLabel={confirm?.label ?? ""}
        onCancel={() => setConfirm(null)}
        onConfirm={async () => {
          await confirm!.action();
          setConfirm(null);
          await onChange();
        }}
      >
        <p>{confirm?.text}</p>
      </ConfirmModal>
    </section>
  );
}

const STOPPED_BY: Record<string, string> = {
  owner: " by the student",
  professor: " by you",
  watchdog: " automatically: budget used up",
};

function keyText(k: KeySummary): string {
  const used = `${usd(k.usage_usd)} used${k.limit_usd ? ` of ${usd(k.limit_usd)} limit` : ""}`;
  if (k.status === "disabled") return `OpenRouter key · revoked · ${used}`;
  if (k.status === "capped") return `OpenRouter key · capped, team budget used up · ${used}`;
  return `OpenRouter key · active · ${used}`;
}

function podText(p: Pod): string {
  const base = `GPU pod · ${p.gpu_name} · ${usd(p.spent_this_month_usd)} this month`;
  if (p.state === "running") return `${base} · running at ${perHour(p.price_per_hour)}`;
  if (p.state === "starting") return `${base} · starting…`;
  const by = p.stopped_by ? STOPPED_BY[p.stopped_by] : "";
  return `${base} · stopped${by} · disk kept, ${perHour(p.storage_per_hour)} storage`;
}