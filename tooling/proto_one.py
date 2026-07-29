#!/usr/bin/env python3
# =============================================================================
# NOTE: This is NOT a NeoCharge script. It is a tuya_local device-provisioning
# prototype, carried into this repo only because it was co-located in the
# neocharge-recon scratch dir. NeoCharge is NOT a Tuya device (see README §2) —
# this script does not touch it. Kept for reference of the HA config-flow-driving
# technique. NeoCharge's own helper is tooling/neocharge-cli.py.
# =============================================================================
"""Phase-1 prototype: add ONE tuya_local device via REST flow, verify local control, restore.
Usage: proto_one.py <device_id>   (reads tuya-keys.json in cwd; HA_TOKEN from cache)
Non-destructive: cloud entry left intact; only ADDS a parallel tuya_local entry."""
import json, os, sys, time, urllib.request
import tinytuya

BASE = "https://homeassistant.local:8123"
TOKEN = open(os.path.expanduser("~/.cache/ha-token-tmp")).read().strip()
did = sys.argv[1]
t = next(x for x in json.load(open("tuya-keys.json")) if x["device_id"] == did)
name, ip, key = t["name"], t["ip"], t["local_key"]
print(f"target: {name}  id={did}  ip={ip}  cat={t['category']}")

def req(path, body=None, method="GET", timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as x:
        return json.loads(x.read())
def post(p, b, t=120): return req(p, b, "POST", t)

# 1. probe protocol
ver = None
for v in (3.5, 3.3, 3.4, 3.1, 3.2, 3.22):
    try:
        d = tinytuya.Device(did, ip, key); d.set_version(v); d.set_socketTimeout(3)
        s = d.status()
        if isinstance(s, dict) and "dps" in s:
            ver = str(v); print(f"protocol probe: v{ver}  dps={list(s['dps'].keys())}"); break
    except Exception:
        pass
if not ver:
    print("PROBE FAILED"); sys.exit(1)

# 2. drive flow
flow = post("/api/config/config_entries/flow", {"handler": "tuya_local", "show_advanced_options": True})["flow_id"]
post(f"/api/config/config_entries/flow/{flow}", {"setup_mode": "manual"})
r = post(f"/api/config/config_entries/flow/{flow}",
         {"device_id": did, "host": ip, "local_key": key, "protocol_version": ver, "poll_only": False})
print("after device step:", r.get("step_id") or r.get("type"), "errors:", r.get("errors"), "reason:", r.get("reason"))
if r.get("type") == "abort":
    print("ABORT:", r.get("reason")); sys.exit(2)
prof = None
if r.get("step_id") == "select_type":
    opts = []
    for f in r.get("data_schema", []):
        if f.get("name") == "type":
            opts = f.get("options") or f.get("selector", {}).get("select", {}).get("options", [])
    best = opts[0]["value"] if isinstance(opts[0], dict) else opts[0]
    prof = best.split("||")[0]
    print(f"select_type -> {prof}  ({len(opts)} profiles offered)")
    r = post(f"/api/config/config_entries/flow/{flow}", {"type": best})
    print("after type step:", r.get("step_id") or r.get("type"))
if r.get("step_id") == "choose_entities":
    r = post(f"/api/config/config_entries/flow/{flow}", {"name": name + " LOCAL"})
    print("after choose_entities:", r.get("type"))
if r.get("type") != "create_entry":
    print("DID NOT CREATE ENTRY:", json.dumps(r)[:400]); sys.exit(3)
entry_id = r["result"]["entry_id"]
print(f"CREATED tuya_local entry: {entry_id}  profile={prof}  proto=v{ver}")

# 3. verify: find new entities tied to this entry, read state, toggle, restore
time.sleep(4)
ents = req("/api/states")  # all states
# match by friendly_name containing "LOCAL"
local_ents = [e for e in ents if " LOCAL" in (e.get("attributes", {}).get("friendly_name") or "")]
print(f"entities created (LOCAL): {[e['entity_id'] for e in local_ents]}")
ctrl = next((e for e in local_ents if e["entity_id"].split(".")[0] in ("light", "switch", "fan")), None)
if not ctrl:
    print("no controllable entity found yet (may need a moment); entry exists though"); sys.exit(0)
eid = ctrl["entity_id"]; dom = eid.split(".")[0]; orig = ctrl["state"]
print(f"control entity: {eid}  state={orig}")
# toggle: turn off then on (or opposite of current), read back, then restore
target_off = "turn_off" if orig == "on" else "turn_on"
restore = "turn_on" if orig == "on" else "turn_off"
post(f"/api/services/{dom}/{target_off}", {"entity_id": eid}); time.sleep(3)
mid = req(f"/api/states/{eid}")["state"]
post(f"/api/services/{dom}/{restore}", {"entity_id": eid}); time.sleep(3)
fin = req(f"/api/states/{eid}")["state"]
print(f"toggle test: {orig} -> {mid} -> {fin}  (restored={'OK' if fin==orig else 'CHECK'})")
print("VERIFY:", "LOCAL CONTROL CONFIRMED" if mid != orig else "state did not change (check)")
print("RESULT_JSON", json.dumps({"entry_id": entry_id, "entity": eid, "proto": ver, "profile": prof,
      "toggle": [orig, mid, fin], "local_control": mid != orig}))
