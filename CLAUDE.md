# neocharge-local — project guide for Claude Code

> 🌐 **Public edition — sanitized.** Device identifiers (UUID/MAC), LAN addresses,
> SSID, and the leaked PSK are genericized placeholders — substitute your own.
> Full-fidelity ops copies live in the maintainer's private workspace.

## What this is

The reverse-engineering + local-control takeover of the **NeoCharge Smart Splitter**
(P12-XXX), a cloud-only 240 V EV/dryer outlet splitter. The cloud uplink is **severed**;
Home Assistant now owns the device locally over a reverse-engineered MQTT protocol.
Done and deployed 2026-06-13. This repo is the durable engineering record + the live
config/tooling.

## Key facts (anchor)

- Device: NeoCharge Smart Splitter, UUID **`P12-XXX`**, IP **`10.0.8.60`**,
  MAC **`48:E7:29:00:00:01`** (Espressif ESP32, ESP-IDF v4.2.5), rated **50 A**, **VLAN 8**.
- **Not Tuya, not AWS IoT Core.** Only channel = plaintext anonymous MQTT (1883, no TLS)
  to NeoCharge's EC2 broker `52.53.137.231`.
- Topics: device SUBSCRIBES `P12-XXX` (commands in), PUBLISHES `log` + `error` (status out).
- Control opcodes: `msg:50 SET_CURRENT_LIMIT` (breaker size, {10,20,30,40,50} A);
  `msg:11 SCHEDULE_SYNC` (relay/charge on/off; device ignores `meta.version` → arbitrary
  on/off synthesizable).
- Local path: gatekeeper DNAT (`10.0.8.60`→`52.53.137.231:1883` ⇒ `10.0.8.10:1893`) +
  hairpin SNAT → dedicated `eclipse-mosquitto:2` broker on HA VM (`:1893`, anon) bridged
  into core-mosquitto → HA package.

## Key files

| File | What |
|---|---|
| `README.md` | The writeup (start here). |
| `docs/protocol.md` | Full MQTT protocol/opcode/command reference. |
| `docs/reproduction.md` | Rebuild-from-scratch guide. |
| `docs/security.md` | Security findings (incl. the cleartext WiFi-PSK leak). |
| `ha/neocharge.yaml` | The deployed HA package (sensors + breaker number + charge switch + heartbeats). **Canonical copy** — mirror of `~/Projects/ha/packages/neocharge.yaml`. |
| `tooling/neocharge-mqtt.conf` | The dedicated broker config. Mirror of `~/Projects/ha/tools/neocharge-mqtt.conf`. |
| `tooling/gatekeeper-dnat.md` | Exact live OpenWrt fw4 DNAT/SNAT rules + apply/rollback. |
| `tooling/deploy.sh` | Deploy broker + HA package + config-check + restart. |
| `tooling/neocharge-cli.py` | Standalone protocol client (watch / ping / set-limit / charge on-off). |
| `tooling/proto_one.py`, `resume_flow.py` | **NOT NeoCharge** — tuya_local prototypes co-located in the recon scratch; kept for reference only (headers explain). |

## How to redeploy

```bash
cd ~/Projects/neocharge-local
./tooling/deploy.sh                 # broker + HA package + config-check + (prompted) restart
# then apply / verify the firewall redirect (separate, auditable step):
#   see tooling/gatekeeper-dnat.md   (ssh root@10.0.0.2)
```

Note: the canonical operational copies live in `~/Projects/ha/` (`packages/neocharge.yaml`,
`tools/neocharge-mqtt.conf`); the copies here are the documented snapshot. If you change
one, sync the other.

## Access

- gatekeeper (OpenWrt, DNAT): `ssh root@10.0.0.2` — READ-ONLY for audits (`uci show`,
  `cat`, `nft list`, `iptables -S`). Mutations only when deliberately redeploying; back
  up firewall first (`uci export firewall > /tmp/fw-backup-<date>.conf`).
- HA VM (broker + package): `ssh jp@10.0.0.10`.
- HA API: `https://homeassistant.local:8123`, token in vault as `ha-llat`.
- Standalone control without HA: `tooling/neocharge-cli.py` (paho-mqtt) against the
  broker `10.0.8.10:1893`.

## Verify it's live

```bash
ssh root@10.0.0.2 "conntrack -L -s 10.0.8.60 -p tcp --dport 1883"   # reply src=10.0.8.10 sport=1893
ssh jp@10.0.0.10 "sudo docker ps --filter name=neocharge-mqtt"      # Up
# HA: sensor.neocharge_uptime increasing, sensor.neocharge_voltage ~240
```

## Gotchas (carried from recon)

- Cross-VLAN MQTT is flaky on this net → broker MUST be same-VLAN (8) as the device.
- The hairpin SNAT is mandatory (same-subnet return-path) — a bare DNAT silently fails.
- The live session is `[OFFLOAD]` → bypasses netfilter; NAT counters only tick on setup.
  Verify via conntrack, not rule counters.
- Manual MQTT entities lag ~10 s on reload; full HA restart subscribes instantly.
- `--network host` container sockets are in the host netns; `ss` inside an addon lies.
- Toggling the relay power-cycles the JuiceBox on the secondary outlet.
