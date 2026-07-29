# NeoCharge Smart Splitter — Local Control (cloud severed)

> ## 🌐 Public edition — identifiers sanitized
>
> Every device identifier (UUID/MAC), LAN address, VLAN layout, SSID, and PSK in
> this repo is a **genericized placeholder** — substitute your own when
> reproducing. The firewall rules are real in *shape* but carry example addresses.
> `docs/security.md` documents the vulnerability classes found, using placeholder
> values as evidence.

---

## 1. What this is

The **NeoCharge Smart Splitter** (a 240 V load-sharing splitter that lets an EV
charger share a dryer/EV outlet) is a cloud-only appliance: no local API, no
documented integration, all 65,535 TCP ports closed. It phones home to
NeoCharge's own broker on a raw EC2 instance over **plaintext MQTT** and is
otherwise a black box.

This project **reverse-engineered that MQTT protocol and took the device to full
local control** — the cloud uplink is now **severed** and Home Assistant owns the
device end-to-end. Achieved, live, and surviving reboots:

- **Full telemetry in HA** — voltage, current-limit setpoint, branch rating,
  relay/contactor state, charging-paused state, WiFi RSSI, uptime, firmware rev.
- **Breaker-size / current-limit control** — `number.neocharge_breaker_size`
  (10/20/30/40/50 A) drives the device's software breaker via the reverse-engineered
  `SET_CURRENT_LIMIT` opcode.
- **Charge on/off (relay) control** — `switch.neocharge_charging` opens/closes the
  contactor via a synthesized `SCHEDULE_SYNC` schedule (the device does **not**
  validate the schedule's server-side hash, so arbitrary on/off is constructible
  locally).
- **Cloud cut off** — the device can no longer reach NeoCharge's EC2 broker; all
  its MQTT traffic is DNAT-redirected to a local broker on the LAN. As a bonus this
  **stops the device leaking the owner's WiFi password to the internet** (see §7).

The work was done 2026-06-13 by a dream team (Lucid = forensics, Nebula =
prior-art, Vesper = protocol/teardown, Somnia = broker/HA, Cirrus = firewall/DNAT,
orchestrated as team `ev-ops`). As far as the recon could find, **this is the first
public/private reverse-engineering of the NeoCharge Smart Splitter** — no HA
integration, HACS add-on, GitHub project, or community thread exists for it.

---

## 2. The device

| Property | Value |
|---|---|
| Product | NeoCharge Smart Splitter (240 V load-sharing splitter) |
| Wiring | PRI outlet (priority, e.g. dryer) / SEC outlet (e.g. EV). Here: right = JuiceBox EVSE, left = Bosch 16 A EV charger |
| UUID / serial token | **`P12-XXX`** (also the device's MQTT topic + client-id) |
| IP | **`10.0.8.60`** — DHCP hostname `neocharge-p12-xxx.lan`, **VLAN 8** (`iot` zone) |
| MAC | **`48:E7:29:00:00:01`** (OUI `48:e7:29` = Espressif) — in payloads as `48E72994C148` |
| MCU | **Espressif ESP32-class** SoC (HLW8110/HLW8112 power-metering IC does the 240 V sensing) |
| Firmware | **ESP-IDF `v4.2.5`** (`v4.2.5-1-g5f8de192fa`), branch `STABLE`, commit `2924086`, app rev `~1996` |
| Rated current | **50 A** (`deviceInfo.current: 50`); line `voltage: 240` |
| Prod date | `prodDate 240103` (2024-01-03) |
| Platform | **ESP-IDF + a NeoCharge-hosted Mosquitto broker on AWS** (Nomad/Consul/Terraform backend, Go services). **NOT Tuya, NOT AWS IoT Core.** |

**Why it is not Tuya and not AWS IoT Core** (this matters — it rules out
`tuya_local`, Cloudcutter, and the X.509-mutual-TLS broker-hijack dead end):

- MAC OUI is Espressif, not a modern Tuya BK7231 module.
- WiFi onboarding uses **Espressif's own "ESP BLE Provisioning" app** (per NeoCharge's
  own setup docs) — the calling card of a stock ESP-IDF / ESP-RainMaker-style stack.
- It runs NeoCharge's **own** React-Native app (`io.neocharge.neoconnect`), not Smart Life.
- Its broker is a **raw EC2 host on port 1883 (plaintext MQTT)** — not
  `*-ats.iot.<region>.amazonaws.com` (which would be AWS IoT Core with per-device
  mutual TLS). NeoCharge's public GitHub forks include `mosquitto-auth-plug`,
  confirming a self-hosted user/pass Mosquitto rather than IoT Core.

Two **independent** control planes exist — don't conflate them:

1. **EV charge scheduling** = NeoCharge cloud ↔ the *vehicle OEM's* cloud API. The
   app's "start/stop charging" commands the **car**, not the splitter. Irrelevant here.
2. **Splitter relay / load control** = the ESP32 ↔ NeoCharge's MQTT broker. **This is
   what we took local.**

---

## 3. How it phones home (the dependency that was severed)

The device holds a **single long-lived outbound TCP session** to:

```
52.53.137.231 : 1883     (ec2-52-53-137-231.us-west-1.compute.amazonaws.com)
```

- **Plaintext MQTT** (port 1883, **no TLS, no cert pinning, no client cert**). This is
  the entire reason local control is feasible — there is no crypto to defeat.
- The device **connects anonymously**: a 21-byte MQTT CONNECT, client-id `P12-XXX`,
  clean-session, keepalive 90 s, **no username, no password**.
- It does **zero DNS lookups** — it dials the broker IP directly, so a DNS override
  alone would never work. **DNAT is the reliable redirect.**
- All 65,535 local TCP ports are closed; no UDP discovery, no mDNS, no
  `_esp_local_ctrl._tcp`. The cloud MQTT session is the device's **only** channel — and
  it is wide open.

Severing it: a DNAT rule on the router rewrites `10.0.8.60 → 52.53.137.231:1883` to a
**local broker on the LAN**, plus a hairpin SNAT so the same-subnet return path works.
The device thinks it's still talking to the cloud; it's talking to us.

---

## 4. Local-control architecture

```
                         VLAN 8 (10.0.8.0/24, "iot" zone)
   ┌──────────────────────┐
   │  NeoCharge Splitter   │  ESP32, UUID P12-XXX
   │  10.0.8.60           │  anonymous plaintext MQTT, client-id "P12-XXX"
   │  48:E7:29:00:00:01    │  SUB topic: P12-XXX   PUB topics: log, error
   └───────────┬───────────┘
               │ dials 52.53.137.231:1883 (thinks = cloud)
               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  gatekeeper (OpenWrt 25.12, fw4/nftables)  br-lan.8 @ 10.0.8.2  │
   │                                                                 │
   │  DNAT : saddr 10.0.8.60 daddr 52.53.137.231 tcp/1883           │
   │           → dnat to 10.0.8.10:1893                             │
   │  SNAT : saddr 10.0.8.60 daddr 10.0.8.10 tcp/1893              │
   │           → masquerade   (hairpin: same-subnet return fix)      │
   │  forward_iot chain auto-accepts via `ct status dnat accept`     │
   └───────────┬─────────────────────────────────────────────────────┘
               │  (cloud 52.53.137.231 now unreachable — SEVERED)
               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  HA VM 10.0.8.10 (ha-vlan8) — Docker, --network host           │
   │                                                                 │
   │  ┌──────────────────────────┐   bridge    ┌──────────────────┐ │
   │  │ neocharge-mqtt           │  log  out → │ core-mosquitto   │ │
   │  │ eclipse-mosquitto:2      │ error out → │ addon :1883      │ │
   │  │ listener :1893           │ P12-XXX in←─│ (go-auth, jp/…)  │ │
   │  │ allow_anonymous true     │             └────────┬─────────┘ │
   │  └──────────────────────────┘                      │           │
   │     (device-facing, no creds)                       │ MQTT      │
   │                                                     ▼           │
   │                                          ┌──────────────────┐  │
   │                                          │ Home Assistant   │  │
   │                                          │ packages/        │  │
   │                                          │  neocharge.yaml  │  │
   │                                          │ 8 sensors        │  │
   │                                          │ number (breaker) │  │
   │                                          │ switch (charge)  │  │
   │                                          │ 2 automations    │  │
   │                                          └──────────────────┘  │
   └───────────────────────────────────────────────────────────────┘
```

**Why a dedicated second broker instead of just core-mosquitto:** HA's
`core-mosquitto` uses the go-auth plugin and **rejects anonymous clients** (CONNACK
rc=5). The device sends no credentials and cannot be made to (no firmware access).
Opening core-mosquitto to anonymous would expose HA's primary broker network-wide.
So a throwaway `eclipse-mosquitto:2` container (`allow_anonymous true`, listener
`:1893`) accepts the device and **bridges only its three topics** into core-mosquitto,
where HA's normal MQTT integration reads/writes them.

**Why same-VLAN is mandatory:** cross-VLAN MQTT is flaky on this network (CONNACK
silently fails / keepalive exceeded). The device (`10.0.8.60`) and broker
(`10.0.8.10`) are both on VLAN 8, intra-L2. A broker on VLAN 6 (e.g. `disks`) was
tried and stalls. The same-subnet placement is also *why* the hairpin SNAT is
required — see §6 / `docs/reproduction.md`.

---

## 5. Protocol summary

Clear-text JSON over MQTT. Three topics:

- **`P12-XXX`** — broker → device (device subscribes). **Publish commands here.**
- **`log`** — device → broker: full status (msg `100` `ACK_PING`) + command ACKs.
- **`error`** — device → broker: full status (msg `41` `MQTT_RECONNECT`, QoS 1)
  autonomously on reconnect.

Opcode grammar: command `msg` = action opcode; the device's ACK opcode = command + 100.

| Action | Cmd `msg` / `hr_msg` | Device ACK |
|---|---|---|
| Server PING (telemetry poll) | `0` / `PING` (`GoStatusUpdatePing`) | `100` `ACK_PING` (full status on `log`) |
| Get calibration offset (read-only) | `27` / `GET_CAL_OFFSET` | `127` `ACK_GET_CAL_OFFSET` |
| **Set current limit / breaker size** | **`50` / `SET_CURRENT_LIMIT`** | `150` `ACK_CURRENT_LIMIT` (or `250` `CURRENT_LIMIT_OUT_OF_BOUNDS`) |
| **Schedule sync = relay/charge on-off** | **`11` / `SCHEDULE_SYNC`** | `70`/`71` success (or fail); relay events `99` `RELAY_CLOSED`, `98` `RELAY_OPEN`, `73` `SCHEDULE_RULE_ACTIVATED` |

Valid breaker sizes: **{10, 20, 30, 40, 50} A only** (others → `msg:250`).

Full message envelopes, status field map, command templates, and the
`meta.version`-not-validated finding are in **[`docs/protocol.md`](docs/protocol.md)**.

---

## 6. Reproduction summary

To rebuild from scratch (full step-by-step in
**[`docs/reproduction.md`](docs/reproduction.md)**):

1. **Forensics** — confirm the device dials `52.53.137.231:1883` plaintext, anonymous.
2. **Stand up the local broker** — `eclipse-mosquitto:2` container on the HA VM, host
   net, listener `:1893`, `allow_anonymous true`, bridged into core-mosquitto
   (`tooling/neocharge-mqtt.conf`).
3. **DNAT + hairpin SNAT** on gatekeeper to redirect `10.0.8.60`'s cloud session to
   `10.0.8.10:1893` (`tooling/gatekeeper-dnat.md`).
4. **Flush conntrack** to force the device to re-dial (the live session is offloaded
   and bypasses netfilter, so the rule won't catch it until reconnect).
5. **Deploy the HA package** (`ha/neocharge.yaml`), config-check, restart HA.
6. **Heartbeat** — HA replays the server PING every 5 min so telemetry keeps flowing
   with the cloud gone.
7. *(one-time, optional)* Brief transparent-MITM bridge to the real cloud to **capture
   the SET/SCHEDULE command formats** by tapping the app, then re-sever.

---

## 7. Security findings

The headline finding: **the device publishes the owner's WiFi PSK in cleartext to NeoCharge's
cloud on every status report.**

```json
"wifi":{"SSID":"iot-ssid","PSWD":"REDACTED_EXAMPLE_PSK","RSSI":-43,"Auth":"WPA2 PSK"}
```

NeoCharge's cloud — and any on-path observer between the device and EC2 — holds the owner's
WiFi password. Combined with plaintext MQTT and anonymous connect, this is poor device
hygiene. **The local redirect closes the leak** (the PSK now stays on the LAN's local
broker and no longer leaves the network). Full evidence + the other findings (no TLS,
anonymous connect, synthesizable control) in **[`docs/security.md`](docs/security.md)**.

---

## 8. Repo file map

```
neocharge-local/
├── README.md                  # this file — the writeup
├── CLAUDE.md                  # per-project guide for future Claude Code sessions
├── .gitignore
├── docs/
│   ├── protocol.md            # full MQTT protocol / message reference
│   ├── reproduction.md        # reproduce-from-scratch guide
│   └── security.md            # security findings + real evidence
├── ha/
│   └── neocharge.yaml         # HA package: sensors + breaker number + charge switch + heartbeats
└── tooling/
    ├── neocharge-mqtt.conf    # eclipse-mosquitto broker config (device-facing + bridge)
    ├── gatekeeper-dnat.md     # exact OpenWrt fw4 DNAT/SNAT rules (live, verbatim) + rollback
    ├── deploy.sh              # deploy broker + HA package + restart helper
    ├── neocharge-cli.py       # standalone publish/subscribe helper (PING / set-limit / charge on-off)
    ├── proto_one.py           # (tuya_local provisioning prototype — NOT NeoCharge; see header)
    └── resume_flow.py         # (tuya_local flow-resume prototype — NOT NeoCharge; see header)
```

---

## 9. Current status / what works / known limits

**Status: COMPLETE and LIVE (verified 2026-06-14).** Confirmed on-box:

- gatekeeper DNAT + hairpin SNAT rules present and counting in `inet fw4`.
- conntrack shows the device session offloaded onto the local broker
  (`reply src=10.0.8.10 sport=1893` = hairpin good).
- `neocharge-mqtt` container up (`eclipse-mosquitto:2`, host net, `--restart unless-stopped`).
- `packages/neocharge.yaml` deployed on the HA VM.

**Works:**

- Live telemetry (voltage / limit / branch rating / relay / paused / RSSI / uptime / fw rev).
- Breaker-size control (`number.neocharge_breaker_size`, {10,20,30,40,50} A) — verified
  driving the device to 30 A then restoring to 20 A, fully local.
- Charge on/off (`switch.neocharge_charging`) — verified flipping the relay both ways
  locally and via HA, surviving HA restart.
- Cloud severed; WiFi-PSK leak closed; rollback is one command (`tooling/gatekeeper-dnat.md`).
- Surfaced on the **Solar Arbitrage → "EV Charging"** dashboard view alongside the JuiceBox.

**Known limits / gotchas:**

- **No native relay toggle exists** in the protocol — on/off is synthesized via a
  `SCHEDULE_SYNC` always-on / always-off schedule. The relay also obeys **autonomous
  dryer-priority load-sensing**, so `RELAY` (and the switch's read-back state) can show
  OFF even when "charging" is enabled, if a higher-priority outlet draws power.
- **Toggling the relay power-cycles whatever is on the SECONDARY outlet** — the JuiceBox
  EVSE reboots when the contactor opens/closes.
- **Current limit is restricted to {10,20,30,40,50} A** by the device firmware; other
  values return `msg:250 CURRENT_LIMIT_OUT_OF_BOUNDS`.
- **Manual MQTT entities lag ~10 s** to subscribe their state topic after a reload; a
  full HA restart subscribes instantly.
- A `--network host` container's sockets live in the host netns — `ss` from inside the
  SSH-addon container falsely shows nothing; verify bridge connections via router
  conntrack.
- The live offloaded session **bypasses netfilter** (flow offloading), so the NAT-rule
  counters only tick on connection setup — that is expected/correct, not a failure.
```
