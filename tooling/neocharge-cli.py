#!/usr/bin/env python3
# =============================================================================
# neocharge-cli.py — standalone NeoCharge Smart Splitter control over MQTT.
#
# Talks the reverse-engineered NeoCharge protocol directly to the local broker
# (publishes to topic P12-XXX, subscribes to log/error). Use it to drive or
# probe the device WITHOUT Home Assistant — handy for verification, debugging,
# or re-deriving behavior after a firmware change.
#
# Requires: paho-mqtt  (pip install paho-mqtt)
# Broker:   the dedicated anonymous broker on the HA VM, 10.0.8.10:1893
#           (or core-mosquitto 10.0.8.10:1883 with creds, via the bridge).
#
# Examples:
#   ./neocharge-cli.py watch                       # subscribe + pretty-print status
#   ./neocharge-cli.py ping                         # poll once, print the status reply
#   ./neocharge-cli.py set-limit 30                 # set breaker/current limit (10/20/30/40/50)
#   ./neocharge-cli.py charge on                    # close relay (charge enabled)
#   ./neocharge-cli.py charge off                   # open relay (charge disabled)
#   ./neocharge-cli.py --host 10.0.8.10 --port 1893 watch
#
# Protocol reference: ../docs/protocol.md
# =============================================================================
import argparse, json, sys, time, uuid

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("need paho-mqtt:  pip install paho-mqtt")

UUID = "P12-XXX"          # device serial == its subscribe topic == its client-id
CMD_TOPIC = "P12-XXX"     # broker -> device (publish commands here)
STATUS_TOPICS = ["log", "error"]   # device -> broker (subscribe for telemetry)
VALID_AMPS = {10, 20, 30, 40, 50}

# Anonymous broker (after the bridge / DNAT). Override with --host/--port.
DEFAULT_HOST = "10.0.8.10"
DEFAULT_PORT = 1893


def now():
    return int(time.time())


def cmd_ping():
    return {"UUID": UUID, "msg": 0, "rev": 0, "hr_msg": "PING",
            "debug": "GoStatusUpdatePing", "time": now(), "topic": "log"}


def cmd_set_limit(amps):
    if amps not in VALID_AMPS:
        sys.exit(f"invalid limit {amps}; device accepts only {sorted(VALID_AMPS)} A "
                 f"(others -> msg:250 CURRENT_LIMIT_OUT_OF_BOUNDS)")
    return {"UUID": UUID, "msg": 50, "hr_msg": "SET_CURRENT_LIMIT", "limit": amps,
            "rev": 1900, "version": 2, "time": now(), "topic": "P12-XXX"}


def cmd_schedule(on):
    """SCHEDULE_SYNC (msg:11). Relay on/off via an always-on / always-off rule.
    Device does NOT validate meta.version; UUID-format ids + non-empty rules[] required."""
    sw = bool(on)
    return {
        "id": str(uuid.UUID(int=0x11111111111111111111111111111111)),
        "msg": 11, "hr_msg": "SCHEDULE_SYNC",
        "payload": {
            "meta": {"schema": 2},
            "schedule": {
                "id": str(uuid.UUID(int=0x22222222222222222222222222222222)),
                "is_enabled": True, "default_switch_on": sw,
                "tz": "America/Los_Angeles",
                "rules": [{
                    "id": str(uuid.UUID(int=0x33333333333333333333333333333333)),
                    "from_timestamp": "2026-01-01 00:00:00",
                    "to_timestamp": "2037-01-01 00:00:00",
                    "hour_minute": {"from": "00:00", "to": "23:59"},
                    "switch_on": sw, "weekdays": [0, 1, 2, 3, 4, 5, 6],
                }],
            },
        },
    }


def make_client(args):
    c = mqtt.Client(client_id=f"neocharge-cli-{now()}", clean_session=True)
    if args.username:
        c.username_pw_set(args.username, args.password)
    c.connect(args.host, args.port, keepalive=30)
    return c


def publish(args, payload, label):
    c = make_client(args)
    c.loop_start()
    c.publish(CMD_TOPIC, json.dumps(payload), qos=0)
    print(f"-> {label}: {json.dumps(payload)}")
    time.sleep(1.5)
    c.loop_stop(); c.disconnect()


def watch(args, oneshot_ping=False):
    seen = {"done": False}

    def on_connect(client, userdata, flags, rc):
        for t in STATUS_TOPICS:
            client.subscribe(t, qos=0)
        if oneshot_ping:
            client.publish(CMD_TOPIC, json.dumps(cmd_ping()), qos=0)
            print("-> PING (waiting for status reply)")

    def on_message(client, userdata, msg):
        try:
            d = json.loads(msg.payload.decode())
        except Exception:
            print(f"[{msg.topic}] {msg.payload[:200]!r}"); return
        # redact the leaked WiFi PSK before printing (see docs/security.md)
        if isinstance(d.get("wifi"), dict) and "PSWD" in d["wifi"]:
            d["wifi"]["PSWD"] = "<redacted>"
        m = d.get("msg")
        rel = d.get("RELAY"); lim = d.get("limit"); volt = d.get("voltage")
        print(f"[{msg.topic}] msg={m} {d.get('hr_msg','')} "
              f"RELAY={rel} limit={lim} voltage={volt} uptime={d.get('uptime')}")
        if oneshot_ping and m in (100, 41):
            print(json.dumps(d, indent=2))
            seen["done"] = True

    c = make_client(args)
    c.on_connect = on_connect
    c.on_message = on_message
    c.loop_start()
    if oneshot_ping:
        for _ in range(100):
            if seen["done"]:
                break
            time.sleep(0.1)
        time.sleep(0.5)
    else:
        print(f"watching {STATUS_TOPICS} on {args.host}:{args.port} (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    c.loop_stop(); c.disconnect()


def main():
    ap = argparse.ArgumentParser(description="NeoCharge Smart Splitter MQTT control")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--username", default=None, help="set if pointing at core-mosquitto :1883")
    ap.add_argument("--password", default=None)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("watch", help="subscribe to log/error and print status")
    sub.add_parser("ping", help="send one PING and print the status reply")
    p_lim = sub.add_parser("set-limit", help="set breaker/current limit (A)")
    p_lim.add_argument("amps", type=int)
    p_chg = sub.add_parser("charge", help="relay on/off via SCHEDULE_SYNC")
    p_chg.add_argument("state", choices=["on", "off"])
    args = ap.parse_args()

    if args.action == "watch":
        watch(args)
    elif args.action == "ping":
        watch(args, oneshot_ping=True)
    elif args.action == "set-limit":
        publish(args, cmd_set_limit(args.amps), f"SET_CURRENT_LIMIT {args.amps}A")
    elif args.action == "charge":
        on = args.state == "on"
        publish(args, cmd_schedule(on), f"SCHEDULE_SYNC charge {'ON' if on else 'OFF'}")


if __name__ == "__main__":
    main()
