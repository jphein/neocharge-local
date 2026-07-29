# NeoCharge Smart Splitter — MQTT Protocol Reference

> Device UUID, MAC, IPs, SSID, and PSK shown here are genericized placeholders.

Reverse-engineered 2026-06-13 from passive captures (Lucid) plus a transparent
MITM capture of the NeoCharge app's commands (Vesper/Cirrus/Somnia, Approach B).
The application layer is **clear-text JSON over plaintext MQTT** (port 1883, no TLS,
no cert pinning). The backend is **Go** (`debug:"GoStatusUpdatePing"`,
`go-updater-*.elb.amazonaws.com` seen in DNS).

---

## 1. Transport

| Property | Value |
|---|---|
| Protocol | MQTT 3.1.1 (proto level `04`) over plaintext TCP |
| Cloud broker | `52.53.137.231:1883` (EC2 us-west-1, raw host — **not** AWS IoT Core) |
| Encryption | **None** — no TLS, no cert pinning, no client certificate |
| Auth | **Anonymous** — CONNECT carries client-id only, no username/password |
| CONNECT frame | 21 bytes: `10 13 00 04 4d515454 04 02 005a 00 07 5031322d393330` |
| ↳ decode | proto `MQTT`/level 4, flags `0x02` (clean session, user=0 pass=0), keepalive `0x005A`=90 s, client-id `P12-XXX` |
| Keepalive (MQTT PINGREQ) | ~45 s |
| App-level status cadence | server PING ~every 6 min → device replies full status |
| DNS | device does **zero** DNS lookups; dials the broker IP directly |

The anonymous, plaintext, unpinned, IP-direct connection is what makes a
DNAT-to-local-broker takeover trivial. There is no crypto and no credential to defeat;
when you run the broker, you accept whatever the device presents.

---

## 2. Topics

| Topic | Direction | Contents |
|---|---|---|
| **`P12-XXX`** | broker → device (device subscribes) | server PING + all commands. **Publish here to control.** |
| **`log`** | device → broker | full status report (`msg:100 ACK_PING`), sent as the reply to each server PING; also command ACKs |
| **`error`** | device → broker | full status report (`msg:41 MQTT_RECONNECT`, QoS 1), emitted **autonomously on reconnect** — useful to populate HA instantly without waiting for a PING |

The topic name **is** the device UUID `P12-XXX`. HA should subscribe to **both `log`
and `error`** to catch all telemetry; full status fields appear in both.

---

## 3. Message envelope

Every message is a JSON object keyed on the device UUID and a numeric opcode:

```json
{
  "UUID": "P12-XXX",
  "msg": 0,
  "hr_msg": "PING",
  "rev": 0,
  "time": 1781352013,
  "runId": "t4dLSTDZYM",
  "topic": "log"
}
```

| Field | Meaning |
|---|---|
| `UUID` | device serial/topic, `P12-XXX` |
| `msg` | **numeric opcode** — the device dispatches on this integer (NOT on `hr_msg`) |
| `hr_msg` | human-readable opcode name (cosmetic; device ignores it for dispatch) |
| `rev` | firmware/app revision (~1996 in status; commands send `1900`) |
| `time` | unix timestamp |
| `runId` | per-boot run id (in status reports) |
| `topic` | which topic the *reply* should go to (commands set `topic` to route the ACK) |

**Critical dispatch fact:** the device dispatches on the **integer `msg` opcode only**.
Tests sending `msg:0` (the safe PING opcode) with various `hr_msg` strings
(`SET_LIMIT`/`SET_CURRENT_LIMIT`/`SET_CURRENT`/`LIMIT`) were **all ignored** — `hr_msg`
is decorative. The SET opcode is a specific integer (`50`), discovered only via the
MITM capture.

---

## 4. Opcode map

Command `msg` = action opcode; the device's ACK opcode = command + 100.

| `msg` | `hr_msg` | Dir | Meaning / fields |
|---|---|---|---|
| `0` | `PING` / `GoStatusUpdatePing` | cloud → dev | telemetry poll (keepalive); device replies full status on `log` |
| `100` | `ACK_PING` | dev → cloud | full status report (topic `log`) |
| `41` | `MQTT_RECONNECT` | dev → cloud | full status report (topic `error`, QoS 1) on reconnect |
| `27` | `GET_CAL_OFFSET` | cloud → dev | read calibration offset (read-only GET, not a settable calibrate) |
| `127` | `ACK_GET_CAL_OFFSET` | dev → cloud | ack, carries `calOffset` |
| **`50`** | **`SET_CURRENT_LIMIT`** | **cloud → dev** | **set amperage** — field `limit` (A). Captured values 10, 50 |
| `150` | `ACK_CURRENT_LIMIT` | dev → cloud | ack; payload includes `limit` + a `limits[]` array |
| `250` | `CURRENT_LIMIT_OUT_OF_BOUNDS` | dev → cloud | rejected — amps not in {10,20,30,40,50} |
| **`11`** | **`SCHEDULE_SYNC`** | **cloud → dev** | **push schedule = the relay/charge control** |
| `70` | `SCHEDULE_SYNC_SUCCESSFUL` | dev → cloud | ack: schedule received |
| `71` | `SCHEDULE_ENABLED` | dev → cloud | schedule active |
| `73` | `SCHEDULE_RULE_ACTIVATED` | dev → cloud | a rule fired (carries the active rule + `switch_on`) |
| `99` | `RELAY_CLOSED` | dev → cloud | **relay ON (charging)** |
| `98` | `RELAY_OPEN` | dev → cloud | **relay OFF** |

---

## 5. Full status report (topic `log`, `msg:100`)

The device → broker status as captured on the wire (snaplen-0):

```json
{
  "UUID": "P12-XXX",
  "msg": 100,
  "hr_msg": "ACK_PING",
  "rev": 1996,
  "time": 1781352013,
  "runId": "t4dLSTDZYM",
  "deviceInfo": { "MAC": "48E72994C148", "prodDate": 240103, "current": 50 },
  "wifi": { "SSID": "iot-ssid", "PSWD": "REDACTED_EXAMPLE_PSK", "RSSI": -43, "Auth": "WPA2 PSK" },
  "dev": 0,
  "system_tz": "America/Los_Angeles",
  "uptime": 51542,
  "PAUSE": false,
  "RELAY": 1,
  "current": 50,
  "limit": 20,
  "voltage": 240,
  "calOffset": 0,
  "fw": { "branch": "STABLE", "commit": "2924086", "idf": "v4.2.5-1-g5f8de192fa" }
}
```

### Status field → HA mapping

| Field | Example | HA entity |
|---|---|---|
| `voltage` | `240` | `sensor.neocharge_voltage` (V) |
| `limit` | `20` | `sensor.neocharge_current_limit` (A) — the throttle setpoint |
| `current` | `50` | `sensor.neocharge_branch_current_rating` (A, diagnostic) — rated/branch current |
| `wifi.RSSI` | `-43` | `sensor.neocharge_wifi_signal` (dBm, diagnostic) |
| `uptime` | `51542` | `sensor.neocharge_uptime` (s → duration, diagnostic) |
| `rev` | `1996` | `sensor.neocharge_firmware_rev` (diagnostic) |
| `RELAY` | `1` | `binary_sensor.neocharge_relay` (1 = closed/on) |
| `PAUSE` | `false` | `binary_sensor.neocharge_charging_paused` (true = paused) |
| `deviceInfo.MAC` | `48E72994C148` | — |
| `wifi.SSID` / `wifi.PSWD` | `iot-ssid` / `REDACTED_EXAMPLE_PSK` | **deliberately NOT surfaced** — see `docs/security.md` |
| `calOffset`, `dev` | `0`, `0` | calibration / dev-mode flags (unused) |

---

## 6. Command templates

All publish to topic **`P12-XXX`**, QoS 0, JSON. All verified driving the device
**locally** (cloud severed).

### 6a. Telemetry PING (heartbeat)

Replay of the server's poll. Device answers with a full status report on `log`. This
is how HA keeps telemetry flowing now that the cloud is gone (every 5 min, see the
heartbeat automation in `ha/neocharge.yaml`).

```json
{"UUID":"P12-XXX","msg":0,"rev":0,"hr_msg":"PING","debug":"GoStatusUpdatePing","time":<unix>,"topic":"log"}
```

### 6b. Set breaker size / current limit (`msg:50`)

`limit` ∈ **{10, 20, 30, 40, 50}** A only; others → `msg:250 CURRENT_LIMIT_OUT_OF_BOUNDS`.

```json
{"UUID":"P12-XXX","msg":50,"hr_msg":"SET_CURRENT_LIMIT","limit":<AMPS>,"rev":1900,"version":2,"time":<unix>,"topic":"P12-XXX"}
```

The captured app command also carried a Go-internal `timestamp` field — **not required**;
replication omitting it works. Device replies on `log`:

```json
{"...":"...","msg":150,"hr_msg":"ACK_CURRENT_LIMIT","limit":<AMPS>,"limits":[3000,<...>]}
```

…and the status `limit` updates. Verified end-to-end: HA drove it to 30 A → device
ACKed `limit:30` → restored to 20 A, all local.

### 6c. Charge ON / relay closed (`msg:11 SCHEDULE_SYNC`)

`default_switch_on:true` + an always-on rule. The device requires **UUID-format ids**
and a **non-empty `rules[]`**; the SHA-256 `meta.version` etag is **NOT required** (see §7).

```json
{"id":"11111111-1111-1111-1111-111111111111","msg":11,"hr_msg":"SCHEDULE_SYNC","payload":{"meta":{"schema":2},"schedule":{"id":"22222222-2222-2222-2222-222222222222","is_enabled":true,"default_switch_on":true,"tz":"America/Los_Angeles","rules":[{"id":"33333333-3333-3333-3333-333333333333","from_timestamp":"2026-01-01 00:00:00","to_timestamp":"2037-01-01 00:00:00","hour_minute":{"from":"00:00","to":"23:59"},"switch_on":true,"weekdays":[0,1,2,3,4,5,6]}]}}}
```

→ `SCHEDULE_SYNC_SUCCESSFUL` (70) → `RELAY_CLOSED` (99) → `RELAY:1`.

### 6d. Charge OFF / relay open (`msg:11 SCHEDULE_SYNC`)

Same payload with `default_switch_on:false` + the rule's `switch_on:false`.

```json
{"id":"11111111-1111-1111-1111-111111111111","msg":11,"hr_msg":"SCHEDULE_SYNC","payload":{"meta":{"schema":2},"schedule":{"id":"22222222-2222-2222-2222-222222222222","is_enabled":true,"default_switch_on":false,"tz":"America/Los_Angeles","rules":[{"id":"33333333-3333-3333-3333-333333333333","from_timestamp":"2026-01-01 00:00:00","to_timestamp":"2037-01-01 00:00:00","hour_minute":{"from":"00:00","to":"23:59"},"switch_on":false,"weekdays":[0,1,2,3,4,5,6]}]}}}
```

→ `RELAY_OPEN` (98) → `RELAY:0`.

> ⚠️ Toggling the relay **power-cycles whatever is on the SECONDARY outlet** (the
> JuiceBox EVSE reboots). The relay also obeys autonomous **dryer-priority
> load-sensing**, so `RELAY` can read 0 even with charging "enabled" if PRI draws power.

### 6e. Get calibration offset (`msg:27`, read-only)

```json
{"UUID":"P12-XXX","msg":27,"hr_msg":"GET_CAL_OFFSET"}
```

→ ACK `msg:127` with `calOffset`. A GET, not a settable calibrate — no HA control built.

---

## 7. The `meta.version` finding (why arbitrary on/off is synthesizable)

The real app's `SCHEDULE_SYNC` payloads carry a `meta.version` SHA-256 (and a rule-id
hash) — server-side **etags**. Empirically, **the device ignores them on inbound**:
constructed `SCHEDULE_SYNC` payloads with *no* `meta.version` (just `meta.schema:2` +
UUID-format ids + a rule) returned `SCHEDULE_SYNC_SUCCESSFUL` and flipped the relay
**both ways**. So HA can build **arbitrary** on/off schedules locally — no need to
reproduce the Go hash, and no need to capture the literal "turn off scheduling"
command. This is the key that turned a read-only telemetry win into full control.

The captured live sequence from the owner's 1-minute test schedule (Approach-B MITM,
09:38–09:40 PDT):

```
09:38:21  SCHEDULE_SYNC          (cloud → dev)
09:39:00  SCHEDULE_RULE_ACTIVATED (dev → cloud)
09:39:01  RELAY_CLOSED  (on)
09:40:03  RELAY_OPEN    (off)
```

---

## 8. What is NOT controllable

- **Relay/PAUSE have no dedicated SET opcode.** The app exposes no raw relay toggle;
  the relay is hardware-auto load-sensing. On/off is only reachable indirectly via the
  `SCHEDULE_SYNC` schedule trick above. `PAUSE` is read-only telemetry.
- **The app's "start/stop charging"** acts on the **vehicle via the OEM cloud**, not on
  the splitter — a separate, cloud-to-cloud control plane that does not touch this device.
- **No ESP local-control plane** (`_esp_local_ctrl._tcp` / RainMaker LAN control) is
  exposed — all TCP ports closed, no mDNS. MQTT injection is the only control channel.

---

## 9. Source captures (evidence)

On gatekeeper `10.0.0.2` under `/tmp` (retained):

- `neocharge.pcap` — 5-min host capture (snaplen 200)
- `neocharge-mqtt.pcap` — full-snaplen MQTT capture (185 s)
- `neocharge-connect.pcap` — first cutover, anonymous CONNECT + rc=5 reject loop
- `nc-1893.pcap` — rc=0 success + first PUBLISH on the local broker
- `neocharge-cmd-wan.pcap` — 52 KB, the 09:39 Approach-B WAN-leg command capture (opcodes 50/11)
- `neocharge-939-devleg.pcap` — 24 KB, device-leg of the same window
