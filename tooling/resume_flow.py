#!/usr/bin/env python3
# =============================================================================
# NOTE: This is NOT a NeoCharge script. It is a tuya_local config-flow resume
# helper, carried into this repo only because it was co-located in the
# neocharge-recon scratch dir. NeoCharge is NOT a Tuya device (see README §2) —
# this script does not touch it. NeoCharge's own helper is tooling/neocharge-cli.py.
# =============================================================================
"""Resume a parked tuya_local flow at the device step (no katana probe -> no socket collision).
Usage: resume_flow.py <flow_id> <device_id> [proto]"""
import json, os, sys, time, urllib.request
BASE = "https://homeassistant.local:8123"
TOKEN = open(os.path.expanduser("~/.cache/ha-token-tmp")).read().strip()
flow_id, did = sys.argv[1], sys.argv[2]
proto = sys.argv[3] if len(sys.argv) > 3 else "auto"
t = next(x for x in json.load(open("tuya-keys.json")) if x["device_id"] == did)
name, ip, key = t["name"], t["ip"], t["local_key"]

def req(path, body=None, method="GET", timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as x:
        return json.loads(x.read())
def post(p, b): return req(p, b, "POST")

fp = f"/api/config/config_entries/flow/{flow_id}"
r = None
for attempt in range(1, 7):
    r = post(fp, {"device_id": did, "host": ip, "local_key": key, "protocol_version": proto, "poll_only": False})
    step = r.get("step_id") or r.get("type")
    errs = r.get("errors")
    print(f"  attempt {attempt}: step={step} errors={errs} reason={r.get('reason')}")
    if r.get("type") == "abort":
        sys.exit(2)
    if step != "local" or not errs:
        break
    time.sleep(8)
prof = None
if r.get("step_id") == "select_type":
    opts = []
    for f in r.get("data_schema", []):
        if f.get("name") == "type":
            opts = f.get("options") or f.get("selector", {}).get("select", {}).get("options", [])
    best = opts[0]["value"] if isinstance(opts[0], dict) else opts[0]
    prof = best.split("||")[0]
    print(f"  select_type -> {prof} ({len(opts)} offered)")
    r = post(fp, {"type": best})
    print("  after type:", r.get("step_id") or r.get("type"))
if r.get("step_id") == "choose_entities":
    r = post(fp, {"name": name + " LOCAL"})
    print("  after choose_entities:", r.get("type"))
if r.get("type") == "create_entry":
    print("CREATED entry:", r["result"]["entry_id"], "profile:", prof)
else:
    print("not created; ended at:", r.get("step_id") or r.get("type"), json.dumps(r)[:300])
