# Security Findings — NeoCharge Smart Splitter

> This document demonstrates a **cleartext WiFi-PSK leak** using placeholder
> credentials as evidence. The SSID/PSK/addresses shown are genericized — the
> vulnerability class is real, the values are not.

Device: NeoCharge Smart Splitter, UUID `P12-XXX`, `10.0.8.60`, MAC
`48:E7:29:00:00:01`, ESP32 (ESP-IDF v4.2.5), VLAN 8 (`iot` zone). Findings from the
2026-06-13 reverse-engineering effort.

---

## Finding 1 (HEADLINE) — WiFi PSK published in cleartext to the cloud

**Severity: High.** On **every** status report, the device publishes the WiFi network
name **and password in cleartext** over plaintext MQTT to NeoCharge's cloud broker.

Captured verbatim off the wire (snaplen-0, topic `log`, device → `52.53.137.231:1883`):

```json
"wifi": { "SSID": "iot-ssid", "PSWD": "REDACTED_EXAMPLE_PSK", "RSSI": -43, "Auth": "WPA2 PSK" }
```

**Impact:**

- **NeoCharge's cloud** stores/handles the owner's WiFi PSK in plaintext.
- **Any on-path observer** between the device and EC2 us-west-1 (LAN segment, ISP,
  transit, AWS edge) can read `SSID:"iot-ssid"` / `PSWD:"REDACTED_EXAMPLE_PSK"` because the transport
  is unencrypted MQTT on port 1883 — no TLS.
- A status report lands roughly every 6 minutes whenever the device is online, so the
  exposure is continuous, not one-time at provisioning.

**Mitigation applied:** the gatekeeper DNAT redirect (this project) sends all of the
device's MQTT traffic to a **local broker on the LAN** (`10.0.8.10:1893`). The PSK now
**never leaves the network** — it lands only on the local brokers. This closes the
internet-facing leak and is a standalone reason to keep the redirect in place
regardless of the Home Assistant goal.

**Residual exposure after mitigation:**

- The PSK still travels in cleartext **on the LAN** (device → gatekeeper → local
  broker) and lands on both the dedicated `neocharge-mqtt` broker and (via the bridge)
  `core-mosquitto`. It is **not** surfaced as an HA entity — the package deliberately
  maps only `wifi.RSSI`, never `wifi.PSWD`. Confined to VLAN 8 + the HA VM.
- Anyone who can sniff VLAN 8 or read the local broker can still recover it. The device
  is at least VLAN-isolated (`iot` zone, `input=REJECT`/`forward=REJECT`, only
  `iot→wan` plus the few explicit HA forwards).
- **Recommended follow-up:** rotate the `iot-ssid` PSK (it was exposed to NeoCharge's cloud
  for the device's entire online history pre-redirect), and consider a broker-side ACL /
  topic filter that strips `wifi.PSWD` before it reaches `core-mosquitto`.

---

## Finding 2 — Plaintext MQTT, no TLS, no cert pinning

The device → cloud transport is **MQTT over plaintext TCP, port 1883** — not 8883/TLS,
not AWS IoT Core mutual TLS. There is no server-certificate verification to defeat and
no client certificate. Consequences:

- All telemetry (including Finding 1's PSK) is readable by any on-path party.
- A DNAT/DNS redirect to an attacker-or-owner-controlled broker is **trivially
  accepted** by the device — there is no crypto check that the broker is legitimate.
- This is precisely what made the local takeover feasible (and what would make a
  malicious takeover feasible for anyone with on-path position).

---

## Finding 3 — Anonymous broker connect (no device credentials)

The device's MQTT CONNECT is a 21-byte frame carrying a **client-id only** (`P12-XXX`),
clean-session, **no username, no password**:

```
CONNECT: 10 13 00 04 4d515454 04 02 005a 00 07 5031322d393330
         └ flags 0x02 = clean session, username=0, password=0
         └ keepalive 0x005A = 90s, client-id "P12-XXX"
```

Confirmed across multiple reconnect attempts — the device **never** presents
credentials. Authentication of the device to its broker is therefore effectively the
**device serial alone** (which doubles as the topic and is printed on the unit / in
every payload). Anyone who knows the serial can impersonate the device's identity to a
NeoCharge-style broker, or — as here — stand up a broker that accepts it.

---

## Finding 4 — Control messages are unauthenticated and synthesizable

The broker → device command channel has **no authentication or integrity protection**
beyond the plaintext JSON itself:

- Commands are accepted on topic `P12-XXX` with no signature the device verifies.
- The `SCHEDULE_SYNC` (`msg:11`) payload carries a SHA-256 `meta.version` etag, but the
  **device does not validate it** — payloads with no `meta.version` (just UUID-format
  ids + a non-empty rule) are accepted and flip the relay both ways. So **arbitrary
  on/off and arbitrary current-limit commands are fully constructible** by anyone who
  can publish to the device's topic.
- Combined with Findings 2–3, an on-path attacker who redirects the device's session
  (DNAT/DNS) can **control a 240 V / 50 A contactor** — open/close the relay,
  power-cycle the secondary outlet (rebooting an attached EVSE), and set the software
  breaker current limit. This is the same capability JP now uses benignly from HA.

Bounds that limit (not eliminate) abuse: current limit is firmware-clamped to
{10,20,30,40,50} A (`msg:250` otherwise), and the relay still obeys autonomous
dryer-priority load-sensing in hardware.

---

## Finding 5 — Device hardening / posture (the good parts)

- All 65,535 local TCP ports **closed**; no local web UI / API / MQTT listener; no
  UDP discovery, no mDNS, no `_esp_local_ctrl._tcp`. The local attack surface is
  minimal — the **only** channel is the outbound cloud MQTT session.
- The device is **VLAN-isolated** on VLAN 8 (`iot` zone): `input=REJECT`,
  `forward=REJECT`, forwarding limited to `iot→wan` plus explicit HA destinations.
- Firmware is stock ESP-IDF `v4.2.5` STABLE; production ESP-IDF images commonly burn
  Secure Boot V2 + Flash Encryption (not verified here — would need an opened unit's
  eFuses), which is why a reflash path was rejected as high-risk.

---

## Net assessment

The splitter's security model is **"trust the network."** It relies entirely on the
cloud broker being unreachable by attackers and on no one sniffing the path — there is
no transport encryption, no device auth, and no command integrity. For a device that
switches **240 V / 50 A** mains and **broadcasts the home WiFi password in cleartext**,
this is poor hygiene. this deployment compensates with **VLAN isolation + a local
broker redirect that severs the cloud and keeps the PSK on-LAN** — turning the device's
own weak design into the mechanism for safe local ownership. Recommended residual
actions: rotate the `iot-ssid` PSK; add a broker-side filter to strip `wifi.PSWD` from
bridged payloads.
