"""OpenRouter key-management probe (B3, Person 3): record REAL responses for docs/notes/openrouter.md.

Run from backend/ with OPENROUTER_MANAGEMENT_KEY in the repo's .env:
    python -m scripts.openrouter_probe

It costs nothing: it makes one key with a $0.10 limit, reads it, changes the limit, disables and
re-enables it, then deletes it (always, even after an error). The key is never used to call a model.
Every response is saved to docs/notes/openrouter_samples/ with secrets replaced by "[redacted]".
Finally it runs the adapter (app/adapters/openrouter.py) through the same life cycle.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from app.adapters.openrouter import BASE_URL, OpenRouterAdapter, OpenRouterError

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
OUT_DIR = REPO / "docs" / "notes" / "openrouter_samples"
_SECRET = re.compile(r"sk-or-[A-Za-z0-9_\-]+")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if k in ("key", "creator_user_id", "workspace_id") else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET.sub("[redacted]", value)
    return value


def main() -> int:
    mgmt = (dotenv_values(REPO / ".env").get("OPENROUTER_MANAGEMENT_KEY") or "").strip()
    if not mgmt:
        print("OPENROUTER_MANAGEMENT_KEY is not set in .env")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(timeout=30)
    headers = {"Authorization": f"Bearer {mgmt}", "Content-Type": "application/json"}

    def call(step: str, method: str, path: str, body: dict | None = None, auth: dict | None = None):
        r = client.request(method, BASE_URL + path, headers=auth or headers, json=body)
        try:
            payload = r.json() if r.content else None
        except ValueError:
            payload = r.text[:300]
        sample = {"request": {"method": method, "path": path, "body": body}, "status": r.status_code, "response": redact(payload)}
        (OUT_DIR / f"{step}.json").write_text(json.dumps(sample, indent=2) + "\n")
        print(f"{step:<22} {r.status_code}  fields: {sorted(payload) if isinstance(payload, dict) else payload}")
        return r.status_code, payload

    key_hash = None
    try:
        s, p = call("01_list", "GET", "/keys?offset=0")
        s, p = call("02_create", "POST", "/keys", {"name": "computewise-probe", "limit": 0.10})
        if s >= 400:
            print("Create failed; stopping. Is this a management key?")
            return 1
        key_hash = p["data"]["hash"]
        print("   secret returned in the response:", "key" in p, "| fields under data:", sorted(p["data"]))
        call("03_get", "GET", f"/keys/{key_hash}")
        call("04_set_limit", "PATCH", f"/keys/{key_hash}", {"limit": 0.20})
        call("05_disable", "PATCH", f"/keys/{key_hash}", {"disabled": True})
        call("06_enable", "PATCH", f"/keys/{key_hash}", {"disabled": False})
        call("07_unknown_key", "GET", "/keys/does-not-exist")
        call("08_bad_management_key", "GET", "/keys?offset=0", auth={"Authorization": "Bearer sk-or-v1-not-a-real-key"})
    finally:
        if key_hash:
            s, _ = call("09_delete", "DELETE", f"/keys/{key_hash}")
            if s >= 400:
                print(f"!! COULD NOT DELETE the probe key {key_hash}. Delete it by hand in OpenRouter > Settings > Keys.")

    print("\nAdapter check (issue, set limit, usage, disable, delete):")
    ad = OpenRouterAdapter(mgmt)
    issued = ad.issue_key("computewise-probe-adapter", 0.10)
    try:
        print("  issued:", issued)  # the secret is hidden from repr
        print("  set_limit:", ad.set_limit(issued.external_ref, 0.20).limit_usd)
        print("  usage:", ad.fetch_usage(issued.external_ref).usage_usd)
        print("  disabled:", ad.disable_key(issued.external_ref).disabled)
    except OpenRouterError as exc:
        print("  adapter error:", exc)
    finally:
        ad.delete_key(issued.external_ref)
        print("  probe key deleted")
    print(f"\nSamples saved in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
