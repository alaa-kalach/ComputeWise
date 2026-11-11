"""RunPod v2 API probe (Week 1, Person 1): record REAL responses for docs/notes/runpod.md.

Run from backend/ with RUNPOD_API_KEY in .env:
    python -m scripts.runpod_probe            # asks before creating the pod
    python -m scripts.runpod_probe --dry-run  # read-only: GPU catalog, pod list, billing, errors
    python -m scripts.runpod_probe --billing POD_ID   # later: has the pod's cost shown up yet?

What it does (about 10-20 minutes, typically well under $0.25):
  1. lists GPU types with prices and picks the cheapest available one under --max-price
  2. creates ONE small pod (PyTorch image, 10 GB container disk, 10 GB volume, Jupyter on)
  3. polls until RUNNING, records connection details (SSH/Jupyter) and runtime stats
  4. stops it, records what a stopped pod reports and costs, then starts it again
  5. terminates it, then reads pod billing
  6. records the error shapes (unknown pod, bad key)

The pod is ALWAYS terminated at the end, even after an error or Ctrl+C. If termination
fails, the script says so loudly: delete the pod by hand in the RunPod console.

Every response is saved (secrets redacted) to docs/notes/runpod_samples/, for the notes
and for the mock adapter. RunPod REST v1 (rest.runpod.io/v1) retires on 2026-11-15, so
this uses v2 (api.runpod.io/v2).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

BASE_URL = "https://api.runpod.io/v2"
IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_NAME = "computewise-probe"

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
OUT_DIR = REPO / "docs" / "notes" / "runpod_samples"

_SECRET_KEY = re.compile(r"(password|passwd|token|secret|api_?key|authorization)", re.IGNORECASE)


# ---------------------------------------------------------------- helpers
def redact(value: Any) -> Any:
    """Drop secret-looking values (and every env value) before anything is saved."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() == "env" and isinstance(v, dict):
                out[k] = {ek: "[redacted]" for ek in v}
            elif _SECRET_KEY.search(str(k)) and not isinstance(v, (dict, list)):
                out[k] = "[redacted]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass
class Recorder:
    out_dir: Path
    step: int = 0
    timings: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rate_limit: dict[str, str] = field(default_factory=dict)

    def save(self, name: str, resp: httpx.Response | None = None, *, body: Any = None, request: Any = None) -> None:
        self.step += 1
        record: dict[str, Any] = {"recorded_at": datetime.now(timezone.utc).isoformat()}
        if resp is not None:
            record["request"] = {"method": resp.request.method, "url": str(resp.request.url)}
            if request is not None:
                record["request"]["json"] = redact(request)
            record["status_code"] = resp.status_code
            record["content_type"] = resp.headers.get("content-type")
            try:
                record["body"] = redact(resp.json())
            except ValueError:
                record["body"] = resp.text[:2000]
            for h in ("ratelimit", "ratelimit-policy", "retry-after"):
                if h in resp.headers:
                    self.rate_limit[h] = resp.headers[h]
        else:
            record["body"] = redact(body)
        path = self.out_dir / f"{self.step:02d}_{name}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=False), encoding="utf-8")

    def note(self, text: str) -> None:
        print(f"  - {text}")
        self.notes.append(text)


class Probe:
    def __init__(self, client: httpx.Client, rec: Recorder, *, sleep: Callable[[float], None] = time.sleep,
                 poll_every: float = 10.0, poll_timeout: float = 900.0) -> None:
        self.c = client
        self.rec = rec
        self.sleep = sleep
        self.poll_every = poll_every
        self.poll_timeout = poll_timeout
        self.pod_id: str | None = None
        self.terminated = False

    # ------------------------------------------------------------ api calls
    def call(self, name: str, method: str, path: str, *, json_body: Any = None, params: dict | None = None,
             save: bool = True) -> httpx.Response:
        resp = self.c.request(method, path, json=json_body, params=params)
        if resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", "5"))
            self.rec.note(f"rate limited on {name}; waiting {wait:.0f}s")
            self.sleep(wait)
            resp = self.c.request(method, path, json=json_body, params=params)
        if save:
            self.rec.save(name, resp, request=json_body)
        return resp

    def wait_for(self, want: set[str], label: str) -> dict[str, Any] | None:
        """Poll GET /pods/{id} until status is in `want`. Saves each new status seen."""
        seen: set[str] = set()
        start = time.monotonic()
        waited = 0.0
        while waited <= self.poll_timeout:
            resp = self.call(f"{label}_poll", "GET", f"/pods/{self.pod_id}", save=False)
            if resp.status_code != 200:
                self.rec.save(f"{label}_poll_error", resp)
                self.rec.note(f"polling returned HTTP {resp.status_code}")
                return None
            pod = resp.json()
            status = pod.get("status", "?")
            if status not in seen:
                seen.add(status)
                self.rec.save(f"{label}_status_{status.lower()}", resp)
                print(f"    status: {status}  (after {waited:.0f}s)")
            if status in want:
                self.rec.timings[f"{label}_seconds"] = round(max(time.monotonic() - start, waited), 1)
                return pod
            if status in ("ERROR", "TERMINATED") and status not in want:
                self.rec.note(f"pod went to {status} while waiting for {sorted(want)}")
                return pod
            self.sleep(self.poll_every)
            waited += self.poll_every
        self.rec.note(f"timed out after {self.poll_timeout:.0f}s waiting for {sorted(want)}")
        return None

    def action(self, action: str) -> httpx.Response:
        return self.call(f"action_{action}", "POST", f"/pods/{self.pod_id}/action", json_body={"action": action})

    # ------------------------------------------------------------ steps
    def catalog(self, max_price: float) -> list[dict[str, Any]]:
        print("1. GPU catalog")
        self.call("catalog_gpus_plain", "GET", "/catalog/gpus")
        candidates: list[dict[str, Any]] = []
        for cloud in ("SECURE", "COMMUNITY"):
            resp = self.call(f"catalog_gpus_{cloud.lower()}", "GET", "/catalog/gpus",
                             params={"include": "AVAILABILITY", "product": "POD", "cloud": cloud})
            if resp.status_code == 403:
                print(f"    RunPod refused the GPU catalog: {resp.json().get('detail', resp.text)[:120]}")
                print("    -> Edit the API key in RunPod (Settings -> API Keys) and allow REST API 'Catalog: read'.")
                return []
            if resp.status_code != 200:
                continue
            key = cloud.lower()
            for g in resp.json().get("gpus", []):
                price = (g.get("price") or {}).get(key)
                if price and g.get(key) and g.get("availability") not in (None, "NONE") and price <= max_price:
                    candidates.append({"id": g["id"], "name": g.get("name"), "cloud": cloud, "price": price,
                                       "memory": g.get("memory"), "availability": g.get("availability")})
        candidates.sort(key=lambda g: (g["price"], g["cloud"] != "SECURE"))
        self.rec.save("chosen_candidates", body=candidates[:10])
        for g in candidates[:5]:
            print(f"    {g['price']:.3f} $/h  {g['cloud']:<9} {g['id']}  ({g['availability']})")
        return candidates

    def create(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        print("2. Create pod")
        for g in candidates[:4]:
            body = {
                "name": POD_NAME,
                "image": IMAGE,
                "gpu": {"id": g["id"], "count": 1},
                "cloud": g["cloud"],
                "disk": 10,
                "mounts": {"persistent": {"size": 10, "path": "/workspace"}},
                "ports": ["8888/http", "22/tcp"],
                "startJupyter": True,
            }
            t0 = time.monotonic()
            resp = self.call("create_pod", "POST", "/pods", json_body=body)
            self.rec.timings["create_call_seconds"] = round(time.monotonic() - t0, 2)
            if resp.status_code in (200, 201):
                pod = resp.json()
                self.pod_id = pod["id"]
                print(f"    created {self.pod_id} on {g['id']} ({g['cloud']}) at {pod.get('cost')} $/h")
                return pod
            self.rec.note(f"create on {g['id']} failed with HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (401, 402, 403, 422):
                break  # not a capacity problem: trying other GPUs won't help
        return None

    def lifecycle(self) -> None:
        print("3. Wait for RUNNING")
        pod = self.wait_for({"RUNNING"}, "start")
        if not pod or pod.get("status") != "RUNNING":
            return
        self.sleep(30)
        self.call("pod_running_after_30s", "GET", f"/pods/{self.pod_id}")
        self.call("list_pods_while_running", "GET", "/pods")

        print("4. Stop")
        self.action("stop")
        stopped = self.wait_for({"EXITED"}, "stop")
        if stopped:
            self.sleep(20)
            self.call("pod_stopped_after_20s", "GET", f"/pods/{self.pod_id}")

            print("5. Start again")
            resp = self.action("start")
            if resp.status_code in (200, 201, 202, 204):
                self.wait_for({"RUNNING"}, "restart")
            else:
                self.rec.note(f"restarting a stopped pod failed with HTTP {resp.status_code} (GPU may be taken)")

    def terminate(self) -> None:
        if not self.pod_id or self.terminated:
            return
        print("6. Terminate")
        for attempt in range(3):
            resp = self.action("terminate") if attempt == 0 else self.call("delete_pod", "DELETE", f"/pods/{self.pod_id}")
            if resp.status_code in (200, 201, 202, 204):
                break
            self.sleep(5)
        for _ in range(12):
            r = self.call("after_terminate", "GET", f"/pods/{self.pod_id}", save=False)
            if r.status_code == 404 or (r.status_code == 200 and r.json().get("status") == "TERMINATED"):
                self.rec.save("after_terminate", r)
                self.terminated = True
                print("    terminated")
                return
            self.sleep(5)
        # last resort
        r = self.call("delete_pod_final", "DELETE", f"/pods/{self.pod_id}")
        if r.status_code in (200, 202, 204, 404):
            self.terminated = True

    def billing(self) -> None:
        print("7. Billing")
        if self.pod_id:
            self.call("billing_this_pod_hourly", "GET", "/billing/pods",
                      params={"podId": self.pod_id, "bucketSize": "hour", "lastN": 3})
        self.call("billing_all_pods_daily", "GET", "/billing/pods", params={"bucketSize": "day", "lastN": 3})
        self.call("billing_aggregated", "GET", "/billing", params={"bucketSize": "day", "lastN": 3})

    def error_shapes(self, api_key_valid: bool = True) -> None:
        print("8. Error shapes")
        self.call("error_unknown_pod", "GET", "/pods/pod_doesnotexist000")
        self.call("error_bad_action", "POST", "/pods/pod_doesnotexist000/action", json_body={"action": "fly"})
        bad = httpx.Client(base_url=self.c.base_url, transport=getattr(self.c, "_transport", None),
                           headers={"Authorization": "Bearer not-a-real-key"}, timeout=30)
        with bad:
            self.rec.save("error_bad_key", bad.get("/pods"))


def billing_check(pod_id: str, api_key: str, out_dir: Path, transport: httpx.BaseTransport | None) -> int:
    """Re-read billing for a terminated pod to see how long RunPod takes to record the cost."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {api_key}"},
                      timeout=60, transport=transport) as c:
        rec = Recorder(out_dir, step=90)
        r1 = c.get("/billing/pods", params={"podId": pod_id, "bucketSize": "hour", "lastN": 24})
        rec.save(f"billing_later_{stamp}_pod_hourly", r1)
        r2 = c.get("/billing/pods", params={"podId": pod_id, "bucketSize": "day", "lastN": 2})
        rec.save(f"billing_later_{stamp}_pod_daily", r2)
    if r1.status_code != 200:
        print(f"Billing call failed: HTTP {r1.status_code} {r1.text[:200]}")
        return 1
    body = r1.json()
    total = body.get("metadata", {}).get("totals", {})
    print(f"Pod {pod_id}: {len(body.get('records', []))} hourly record(s), totals {total}")
    for r in body.get("records", []):
        print(f"  {r.get('startTime')}  total ${r.get('totalAmount')}  gpu ${r.get('gpuAmount')}  disk ${r.get('diskAmount')}")
    print(f"Saved to {out_dir}")
    return 0


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None,
         sleep: Callable[[float], None] = time.sleep, out_dir: Path = OUT_DIR, api_key: str | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read-only calls; never creates a pod")
    ap.add_argument("--yes", action="store_true", help="don't ask before creating the pod")
    ap.add_argument("--billing", metavar="POD_ID", help="only read billing for an earlier pod (no cleanup of samples)")
    ap.add_argument("--max-price", type=float, default=0.50, help="max $/hour for the test GPU (default 0.50)")
    args = ap.parse_args(argv)

    if api_key is None:
        env = {**dotenv_values(REPO / ".env"), **dotenv_values(BACKEND / ".env")}
        api_key = (env.get("RUNPOD_API_KEY") or "").strip()
    if not api_key:
        print("RUNPOD_API_KEY is missing from .env")
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    if args.billing:
        return billing_check(args.billing, api_key, out_dir, transport)
    for old in out_dir.glob("*.json"):
        old.unlink()
    rec = Recorder(out_dir)
    client = httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {api_key}"},
                          timeout=60, transport=transport)
    probe = Probe(client, rec, sleep=sleep)
    started = time.monotonic()
    code = 0
    try:
        auth = probe.call("list_pods_before", "GET", "/pods")
        if auth.status_code in (401, 403):
            print(f"RunPod rejected the key (HTTP {auth.status_code}). Check RUNPOD_API_KEY and its permissions.")
            return 3
        candidates = probe.catalog(args.max_price)
        if args.dry_run:
            probe.billing()
            probe.error_shapes()
            return 0
        if not candidates:
            print(f"No usable GPU found (see above, or none available under ${args.max_price:.2f}/h). "
                  "Nothing was created.")
            return 4
        g = candidates[0]
        if not args.yes:
            answer = input(f"\nCreate one pod on {g['id']} ({g['cloud']}) at ${g['price']:.3f}/h for ~15 min? "
                           "It will be terminated automatically. Type yes: ")
            if answer.strip().lower() != "yes":
                print("Cancelled. No pod was created.")
                return 0
        if probe.create(candidates) is None:
            print("Could not create a pod (see notes above). Nothing is running.")
            code = 5
        else:
            probe.lifecycle()
    except KeyboardInterrupt:
        print("\nInterrupted: cleaning up...")
        code = 130
    except Exception as e:  # noqa: BLE001 - we must still clean up
        print(f"\nError: {type(e).__name__}: {e}. Cleaning up...")
        rec.note(f"script error: {type(e).__name__}: {e}")
        code = 1
    finally:
        try:
            probe.terminate()
        except Exception as e:  # noqa: BLE001
            rec.note(f"termination error: {e}")
        if probe.pod_id and not probe.terminated:
            print("\n" + "!" * 70)
            print(f"!! COULD NOT CONFIRM POD {probe.pod_id} IS TERMINATED.")
            print("!! Open https://console.runpod.io/pods and terminate it by hand NOW.")
            print("!" * 70)
            code = code or 6
        if probe.pod_id:
            if probe.terminated:
                sleep(60)  # give billing a moment to record the pod
            try:
                probe.billing()
                probe.error_shapes()
            except Exception as e:  # noqa: BLE001
                rec.note(f"billing/error probe failed: {e}")
        rec.timings["total_seconds"] = round(time.monotonic() - started, 1)
        rec.save("summary", body={"pod_id": probe.pod_id, "terminated": probe.terminated, "timings": rec.timings,
                                  "rate_limit_headers": rec.rate_limit, "notes": rec.notes, "exit_code": code})
        client.close()
        print(f"\nSaved {rec.step} responses to {out_dir}")
    return code


if __name__ == "__main__":
    sys.exit(main())
