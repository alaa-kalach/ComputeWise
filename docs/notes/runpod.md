# RunPod notes (B2, Week 1)

Recorded on 2026-11-11 against the live API with `backend/scripts/runpod_probe.py`
(one RTX A2000 community pod, ~1 minute of GPU time). Every raw response, with secrets
redacted, is in [`runpod_samples/`](runpod_samples/). The mock RunPod adapter should
replay those shapes.

> **Use API v2 only.** REST v1 (`https://rest.runpod.io/v1`) is deprecated and retires
> on **2026-11-15**. Everything below is v2: `https://api.runpod.io/v2`.

## Things that will bite the adapter

1. **`status: RUNNING` does not mean ready.** Create returns `RUNNING` instantly (it is the
   desired state). The pod is actually up when **`runtime` is non-null** on
   `GET /pods/{id}` (here after ~30 s, `runtime.uptime` = 2). Until then there is no direct
   SSH and no port mapping. Show "starting" in the UI until `runtime` appears.
2. **`runtime` is always `null` in list responses** (`GET /pods`). Use `GET /pods/{id}` for
   connection details and live stats.
3. **Terminated pods disappear.** `terminate` returns `204` with no body and the pod then
   returns `404 pod not found`; it never shows `TERMINATED`. The sync job must treat
   404 on a known pod as terminated, and ComputeWise must keep its own history
   (`issued_resources`) because RunPod forgets the pod.
4. **Billing lags.** One minute after a pod was terminated, `GET /billing/pods?podId=...`
   had **no records** at all. The watchdog cannot rely on billing for live enforcement:
   estimate spend as `cost` × running time (plus storage), and reconcile with billing
   when it arrives. The probe confirmed the estimate: billing was per second and matched
   our running time (34 s vs ~33 s). See "Billing" below.
5. **Pod ids look like `s4gxa09xywsjml`**, not the `pod_abc123` shown in the docs examples.
6. **`gpu.memory` on a pod is host RAM (62 GB here), not VRAM.** VRAM is `memory` in the
   catalog (6 GB for the A2000). Don't show the pod's value as "GPU memory".
7. **`cost` is still reported on stopped pods** (`0.12` while `EXITED`) even though GPU
   compute is not billed then. Don't read `cost` as "currently burning".
8. **The pod env contains `JUPYTER_PASSWORD`** when Jupyter is on. The pod owner needs it
   to log in to Jupyter; nobody else should see it, and it must never be logged.
9. **Secure cloud is often out of cheap GPUs.** At probe time no secure GPU under
   $0.50/h was available (cheapest available: RTX A6000 at $0.59); community had the
   A2000 at $0.12. The pre-launch check must read availability per cloud.

## Auth, errors, limits

- Header: `Authorization: Bearer <api key>`.
- Errors are `application/problem+json`: `{"title", "status", "detail"}`, plus `errors[]` on 422.
  - `401` bad key: `"Subject token is invalid or expired"`
  - `403` missing permission: `"this API key does not grant catalog:read"`
  - `404` unknown pod: `"pod not found"`
  - `422` validation: `"$.action: value must be one of 'start', 'stop', 'restart', 'terminate'"`
  - Documented but not observed: `400` rule violation or **no capacity**, `402` **insufficient balance**.
- Rate limits (per user, all v2 routes): **180/min, 7,200/hour, 86,400/day**, reported in
  `RateLimit` and `RateLimit-Policy` headers; `429` carries `Retry-After`.
- Key permissions the adapter needs (scoped key, REST): **Pods read+write, Catalog read,
  Billing read** (Templates read optional). Catalog can only be chosen when the key is created.

## Calls the adapter needs

| Purpose | Call | Notes |
|---|---|---|
| GPU list + prices (pre-launch check) | `GET /catalog/gpus?include=AVAILABILITY&product=POD&cloud=SECURE\|COMMUNITY` | per GPU: `id`, `name`, `memory` (VRAM GB), `price.secure` / `price.community` ($/h per GPU), `availability` (`NONE/LOW/MEDIUM/HIGH`), `maxCount`, `minRam`, `minVcpuCount`, `cudaVersions[]`. Without `include=AVAILABILITY` you get prices but no availability. |
| Launch | `POST /pods` | body below; `201` returns the full pod |
| Status, connection details, live stats | `GET /pods/{id}` | `runtime` non-null only when actually running |
| List | `GET /pods` | `{"pods": [...], "pagination": {"hasNextPage", "nextCursor"}}`; follow the cursor |
| Stop / start / restart / terminate | `POST /pods/{id}/action` `{"action": "stop"}` | stop/start return `200` + pod; terminate returns `204` |
| Delete (alternative to terminate) | `DELETE /pods/{id}` | |
| Per-pod spend | `GET /billing/pods?podId=...&bucketSize=hour&lastN=24` | lags; see Billing |
| All pods' spend | `GET /billing/pods?bucketSize=day&lastN=N` | one record per pod per bucket |
| Account-wide spend | `GET /billing?bucketSize=day&lastN=N` | split by product (`podGpuAmount`, `podDiskAmount`, ...) |

### Launch body (what the probe used, accepted as-is)

```json
{
  "name": "computewise-probe",
  "image": "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404",
  "gpu": {"id": "NVIDIA RTX A2000", "count": 1},
  "cloud": "COMMUNITY",
  "disk": 10,
  "mounts": {"persistent": {"size": 10, "path": "/workspace"}},
  "ports": ["8888/http", "22/tcp"],
  "startJupyter": true
}
```

- `disk` = container disk (GB, wiped on stop). `mounts.persistent` = volume (min 10 GB,
  survives stop, billed while stopped).
- Unknown fields are rejected (422), so build the body exactly.
- `startSsh: true` requires SSH public keys registered on the account
  (`PUT /account/ssh-keys`). Without it the API still returned SSH proxy and direct
  details (below); actually connecting was not tested and may need a registered key.
- **Tagging with user and group (B4):** the pod has a `tags` field (`[]` here) and a free
  `name`. Not tested yet; until it is, put the ids in the name, e.g.
  `cw-<group_id8>-<user_id8>`, and rely on `issued_resources` as the source of truth.

### Pod lifecycle observed

| Moment | `status` | `actions` | `runtime` | SSH |
|---|---|---|---|---|
| right after create | `RUNNING` | stop, restart, terminate | `null` | proxy only |
| +30 s | `RUNNING` | same | uptime 2 s, GPU/CPU util, port map | proxy + direct |
| after `stop` (immediate) | `EXITED` | start, terminate | stale copy | |
| stopped +20 s | `EXITED` | start, terminate | `null` | proxy only |
| after `start` | `RUNNING` | stop, restart, terminate | `null` (booting) | proxy only |
| after `terminate` | 404 | | | |

`startedAt` resets on every start, so `now - startedAt` is the current session's run time.
The `start` call on the stopped pod was accepted immediately (same machine, volume kept),
but the pod was terminated 1 s later, so a full reboot to `runtime` was not observed. On a
busy machine RunPod may have no GPU free for it (documented as a `400`).

### Connection details for the user (C4 "see its connection details")

From `GET /pods/{id}` once `runtime` is set:

- **SSH via proxy** (always present, no public IP needed):
  `ssh.proxy.command` → `ssh s4gxa09xywsjml-64410f89@ssh.runpod.io`
- **Direct SSH** (only with `22/tcp` exposed and `runtime` set):
  `ssh.direct.command` → `ssh root@194.26.196.170 -p 22796`
- **Jupyter**: port `8888/http` → `runtime.ports[]` entry with `private: 8888, type: "http"`.
  HTTP ports are served through RunPod's proxy at `https://<podId>-8888.proxy.runpod.net`
  (RunPod convention; not returned by the API, so verify in B4). Login password = env
  `JUPYTER_PASSWORD`.

## Billing and storage

- `GET /billing/pods` record: `startTime`, `endTime`, `podId`, `totalAmount`, `gpuAmount`,
  `cpuAmount`, `diskAmount` (USD). `metadata.totals` sums them; `metadata.query` echoes the
  window (snapped to bucket boundaries).
- **Lag:** 0 records 1 minute after terminate (11:26 UTC); the record was there at the
  re-check about 7 hours later (`--billing`, 18:40 UTC). So the lag is somewhere between
  1 minute and ~7 hours: too slow for enforcement either way (hence estimate + reconcile).
  The record lands in the **hourly bucket** the pod ran in (`2026-11-11T11:00:00Z`), not
  per run. Sample: `runpod_samples/9x_billing_later_*`.
- **What the probe pod was billed** (total **$0.0013655**):
  - `gpuAmount` $0.0011341 = **34 s** at $0.12/h. Our own timeline had ~33 s of running
    time (32 s before stop + ~1 s after restart). **Billing is per second, and
    `cost` × running time matches it**, so the watchdog's estimate is sound.
  - `diskAmount` $0.0002315 = exactly 20 GB (10 container + 10 volume) × $0.10/GB/month
    for **5 minutes**, although the pod existed ~1 minute. Disk looks billed in 5-minute
    steps or with a minimum. One observation only: treat as unconfirmed.
  - `cpuAmount` 0.
- **Storage rates** (RunPod billing docs): volume disk **$0.20/GB/month while stopped**,
  container disk $0.10/GB/month while running, network volumes $0.07/GB/month. Billed per
  second. For our 10 GB volume: stopped cost ≈ 10 × 0.20 / 730 ≈ **$0.0027/h**.
- At **$0 balance RunPod stops all running pods** itself; pods without a network volume
  can be terminated and their data lost. Deploying needs at least one hour of credit for
  the chosen configuration.

## What this means for ComputeWise

- **Pre-launch check (B4):** catalog price for the chosen cloud × GPU count + storage, vs.
  the team's remaining budget for at least N hours; refuse if not covered. Also refuse if
  `availability` is `NONE`.
- **Watchdog (B5):** each sync, for every running pod: spend since last sync ≈
  `cost` × elapsed running time; for stopped pods add volume storage. Write those as
  `spend` ledger entries tied to the pod's `credential_id`, then reconcile against
  `/billing/pods` records once they appear (adjustment entries for the difference).
  This replaces "poll billing history per pod" in the design doc, because billing lags.
- **Stop vs terminate (open question 3):** stop keeps the volume at ~$0.20/GB/month,
  terminate deletes everything and RunPod forgets the pod. Stop + counted storage, as
  proposed, is consistent with what we saw.
- **Polling interval (open question 4):** the limits (180/min) are far above what a
  handful of pods needs; the interval is a cost-accuracy choice, not a rate-limit one.

## Probe run

| | |
|---|---|
| GPU | NVIDIA RTX A2000, community, $0.12/h (VRAM 6 GB, 6 vCPU, 62 GB RAM) |
| Timeline | created 11:24:14, real runtime by 11:24:45, stopped 11:24:46, started 11:25:09, terminated 11:25:10 (UTC) |
| Create call | 1.2 s |
| Billed | $0.0013655 (GPU $0.0011341 = 34 s; disk $0.0002315) |
| Result | terminated, `GET` → 404, nothing left running |
