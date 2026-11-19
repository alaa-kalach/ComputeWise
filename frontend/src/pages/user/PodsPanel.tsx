// The student's RunPod pods in one group: launch (with the 1-hour budget check), stop,
// start, terminate, and the SSH / Jupyter details once the pod is actually ready.

import { useEffect, useMemo, useState } from "react";

import { Button, CopyField, IconGpu, Modal, Notice, Tag } from "../../components/ui";
import { ApiError } from "../../lib/api/client";
import { getPod, launchPod, listGpus, startPod, stopPod, terminatePod } from "../../lib/api/endpoints";
import type { Cloud, GpuOption, LaunchRefused, Pod, PodDetail } from "../../lib/api/types";
import { perHour, usd } from "../../lib/money";
import { useAsync } from "../../lib/useAsync";
import { AccessState } from "./RequestAccess";
import { hasApproved, latestRequest, type GroupData } from "./shared";

const STATE: Record<Pod["state"], { text: string; tone?: "ok" | "low" | "out"; busy?: boolean }> = {
  starting: { text: "Starting", busy: true },
  running: { text: "Running", tone: "ok" },
  stopped: { text: "Stopped" },
  terminated: { text: "Terminated", tone: "out" },
};

function errorText(e: unknown): string {
  if (e instanceof ApiError && e.status === 409 && e.body && typeof e.body === "object" && "reason" in e.body) {
    const r = e.body as LaunchRefused;
    return r.detail;
  }
  return e instanceof Error ? e.message : "Something went wrong";
}

export function PodsPanel({ group, onChange }: { group: GroupData; onChange: () => Promise<void> }) {
  const { membership, pods, requests } = group;
  const [launching, setLaunching] = useState(false);
  const [openPod, setOpenPod] = useState<string | null>(null);
  const approved = hasApproved(requests, "runpod");

  return (
    <div className="panel">
      <h3>
        <span className="panel-icon"><IconGpu /></span>
        GPU pods
      </h3>
      <p className="muted">RunPod machines billed by the hour to this group's budget.</p>

      {!approved ? (
        <AccessState provider="runpod" groupId={membership.group_id} groupName={membership.group_name}
          request={latestRequest(requests, "runpod")} onChange={onChange} />
      ) : (
        <div className="panel-body">
          {pods.length === 0 && <p className="panel-empty">No pods yet.</p>}
          {pods.map((p) => (
            <PodItem key={p.id} pod={p} open={openPod === p.id} onToggle={() => setOpenPod(openPod === p.id ? null : p.id)}
              onChange={onChange} />
          ))}
          <div>
            <Button variant="primary" onClick={() => setLaunching(true)}>
              Launch a pod
            </Button>
          </div>
        </div>
      )}

      {launching && (
        <LaunchDialog
          groupId={membership.group_id}
          groupName={membership.group_name}
          available={group.budget.available}
          onClose={() => setLaunching(false)}
          onLaunched={async (pod) => {
            setLaunching(false);
            setOpenPod(pod.id);
            await onChange();
          }}
        />
      )}
    </div>
  );
}

function PodItem({ pod, open, onToggle, onChange }: { pod: Pod; open: boolean; onToggle: () => void; onChange: () => Promise<void> }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmTerminate, setConfirmTerminate] = useState(false);
  const state = STATE[pod.state];

  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    setError(null);
    try {
      await fn();
      await onChange();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="item">
      <div className="row">
        <span className="item-title grow">
          {pod.gpu_name} <span className="muted">({pod.cloud})</span>
        </span>
        <Tag tone={state.tone} busy={state.busy}>{state.text}</Tag>
      </div>
      <div className="item-meta">
        {perHour(pod.price_per_hour)} while running, {perHour(pod.storage_per_hour)} for its {pod.volume_gb} GB volume while
        stopped. {usd(pod.spent_this_month_usd)} spent this month.
        {pod.stopped_by === "watchdog" && " Stopped automatically because the budget ran out."}
        {pod.stopped_by === "professor" && " Stopped by your professor."}
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      <div className="row">
        {(pod.state === "running" || pod.state === "starting") && (
          <>
            <Button size="sm" variant={open ? "primary" : "secondary"} onClick={onToggle} aria-expanded={open}>
              {open ? "Hide connection" : "Connect"}
            </Button>
            <Button size="sm" busy={busy === "stop"} onClick={() => act("stop", () => stopPod(pod.id))}>
              Stop
            </Button>
          </>
        )}
        {pod.state === "stopped" && (
          <Button size="sm" busy={busy === "start"} onClick={() => act("start", () => startPod(pod.id))}>
            Start
          </Button>
        )}
        {pod.state !== "terminated" && (
          <Button size="sm" variant="quiet" onClick={() => setConfirmTerminate(true)}>
            Terminate
          </Button>
        )}
      </div>
      {open && (pod.state === "running" || pod.state === "starting") && (
        <Connection podId={pod.id} listSaysStarting={pod.state === "starting"} onReady={onChange} />
      )}

      <Modal
        open={confirmTerminate}
        title="Terminate this pod?"
        onClose={() => setConfirmTerminate(false)}
        footer={
          <>
            <Button onClick={() => setConfirmTerminate(false)}>Keep it</Button>
            <Button variant="danger" busy={busy === "terminate"}
              onClick={() => act("terminate", () => terminatePod(pod.id)).then(() => setConfirmTerminate(false))}>
              Terminate and delete its data
            </Button>
          </>
        }
      >
        <p>
          The pod and its {pod.volume_gb} GB volume are deleted, including everything in /workspace. This can't be
          undone. To pause without losing data, stop it instead.
        </p>
      </Modal>
    </div>
  );
}

/** Polls the pod until RunPod reports it ready, then shows how to connect. */
function Connection({
  podId,
  listSaysStarting,
  onReady,
}: {
  podId: string;
  listSaysStarting: boolean;
  onReady: () => Promise<void>;
}) {
  const [detail, setDetail] = useState<PodDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    // The list may still say "Starting" even if the pod is already up when we first ask
    // (a fast start, or details opened late): reload the list then too, or its tag stays wrong.
    let wasStarting = listSaysStarting;
    const poll = async () => {
      try {
        const d = await getPod(podId);
        if (!live) return;
        setDetail(d);
        setError(null);
        if (d.state === "starting") {
          wasStarting = true;
          timer = setTimeout(poll, 3000);
        } else if (wasStarting) {
          void onReady(); // the list should say "Running" now too
        }
      } catch (e) {
        if (live) setError(errorText(e));
      }
    };
    void poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [podId, listSaysStarting, onReady]);

  if (error) return <Notice kind="error">{error}</Notice>;
  if (!detail) return <p className="muted" role="status">Checking the pod…</p>;
  if (detail.state === "starting" || !detail.connection) {
    return (
      <div className="connection" role="status">
        <p>
          RunPod is starting the machine. SSH and Jupyter appear here when it's ready, usually within a minute. You're
          already billed while it starts.
        </p>
      </div>
    );
  }
  const c = detail.connection;
  return (
    <div className="connection">
      {c.jupyter_url && (
        <div>
          <div className="copy-label">Jupyter</div>
          <a href={c.jupyter_url} target="_blank" rel="noreferrer">
            Open Jupyter in a new tab
          </a>
        </div>
      )}
      {c.jupyter_password && <CopyField label="Jupyter password" value={c.jupyter_password} secret />}
      {c.ssh_direct && <CopyField label="SSH (direct)" value={c.ssh_direct} />}
      {c.ssh_proxy && <CopyField label="SSH (through RunPod, works on any network)" value={c.ssh_proxy} />}
      {detail.gpu_util_percent !== null && <p className="item-meta">GPU in use: {detail.gpu_util_percent}%</p>}
    </div>
  );
}

function LaunchDialog({
  groupId,
  groupName,
  available,
  onClose,
  onLaunched,
}: {
  groupId: string;
  groupName: string;
  available: string;
  onClose: () => void;
  onLaunched: (pod: Pod) => Promise<void>;
}) {
  const gpus = useAsync(() => listGpus(groupId), [groupId]);
  const [cloud, setCloud] = useState<Cloud>("community");
  const [choice, setChoice] = useState<string | null>(null);
  const [volume, setVolume] = useState(10);
  const [jupyter, setJupyter] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const options = useMemo(() => (gpus.data ?? []).filter((g) => g.cloud === cloud), [gpus.data, cloud]);
  const launchable = (g: GpuOption) => g.availability !== "none" && (g.affordable_hours === null || Number(g.affordable_hours) >= 1);
  const chosen = options.find((g) => g.gpu_type_id === choice && launchable(g)) ?? null;

  const launch = async () => {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      const pod = await launchPod({ group_id: groupId, gpu_type_id: chosen.gpu_type_id, cloud, volume_gb: volume, jupyter });
      await onLaunched(pod);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      title={`Launch a pod in ${groupName}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={!chosen} onClick={launch}>
            {chosen ? `Launch ${chosen.name}` : "Launch"}
          </Button>
        </>
      }
    >
      <p className="muted">
        You have {usd(available)} to spend in this group. A pod needs at least one hour of budget to start, and keeps
        costing storage while it's stopped.
      </p>
      {error && <Notice kind="error">{error}</Notice>}

      <div className="field">
        <span id="cloud-label" style={{ fontWeight: 600, fontSize: 14 }}>Cloud</span>
        <div className="segmented" role="group" aria-labelledby="cloud-label">
          {(["community", "secure"] as const).map((c) => (
            <button key={c} type="button" aria-pressed={cloud === c} onClick={() => setCloud(c)}>
              {c === "community" ? "Community (cheaper)" : "Secure (more reliable)"}
            </button>
          ))}
        </div>
      </div>

      <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>GPU</legend>
        {gpus.loading && !gpus.data && <p className="muted" role="status">Loading prices…</p>}
        {gpus.error && <Notice kind="error">{errorText(gpus.error)}</Notice>}
        <div className="gpu-list">
          {options.map((g) => {
            const can = launchable(g);
            const why =
              g.availability === "none"
                ? "None free right now"
                : !can
                  ? "Your budget covers less than an hour"
                  : g.affordable_hours !== null
                    ? `Your budget covers about ${g.affordable_hours} h`
                    : `Availability: ${g.availability}`;
            return (
              <label key={`${g.cloud}-${g.gpu_type_id}`} className="gpu">
                <input type="radio" name="gpu" value={g.gpu_type_id} disabled={!can}
                  checked={choice === g.gpu_type_id} onChange={() => setChoice(g.gpu_type_id)} />
                <span>
                  <span style={{ fontWeight: 600 }}>{g.name}</span> <span className="sub">{g.vram_gb} GB VRAM</span>
                  <br />
                  <span className="sub">{why}</span>
                </span>
                <span className="price">{perHour(g.price_per_hour)}</span>
              </label>
            );
          })}
        </div>
      </fieldset>

      <div className="row" style={{ alignItems: "end" }}>
        <div className="field" style={{ width: 180 }}>
          <label htmlFor="volume">Volume (GB)</label>
          <input id="volume" className="input" type="number" min={10} max={200} value={volume}
            onChange={(e) => setVolume(Math.max(10, Math.min(200, Number(e.target.value) || 10)))} />
        </div>
        <label className="row" style={{ gap: 8, paddingBottom: 8 }}>
          <input type="checkbox" checked={jupyter} onChange={(e) => setJupyter(e.target.checked)} />
          Start Jupyter
        </label>
      </div>
      <p className="hint muted" style={{ fontSize: 13 }}>
        Files in /workspace survive a stop. Everything else is reset when the pod restarts.
      </p>
    </Modal>
  );
}
