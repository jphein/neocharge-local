# Reproduction Guide — NeoCharge local control from scratch

> IPs, MACs, hostnames, and the PSK shown here are genericized placeholders — substitute your own.

This rebuilds the full-local takeover on a fresh device (or after a factory reset /
hardware swap). It assumes the reference homelab: gatekeeper (OpenWrt 25.12, fw4/nftables) at
`10.0.0.2`, HA VM at `10.0.0.10` (VLAN8 leg `10.0.8.10`, `ha-vlan8`), device on
VLAN 8 (`iot` zone). Adjust the `P12-XXX` UUID, `10.0.8.60` IP and `48:e7:29:…` MAC
if the unit differs.

Access used throughout: `ssh root@10.0.0.2` (gatekeeper), `ssh jp@10.0.0.10` (HA VM).

---

## Step 0 — Forensics / confirm the device is in scope

Confirm it's the plaintext-MQTT, anonymous, IP-direct device this whole approach
depends on. From gatekeeper (VLAN8 leg `br-lan.8`):

```sh
ssh root@10.0.0.2 "
  # identity
  ip neigh show 10.0.8.60            # expect lladdr 48:E7:29:00:00:01 (Espressif)
  # outbound: expect a long-lived session to 52.53.137.231:1883, plaintext MQTT
  timeout 60 tcpdump -ni br-lan.8 -s0 'host 10.0.8.60 and tcp port 1883'
"
```

Expect: keepalive PINGREQ/PINGRESP ~45 s, app-level PING/ACK_PING ~6 min, readable
JSON, destination `52.53.137.231:1883`. **No** TLS, **no** `*-ats.iot.*.amazonaws.com`.
A full TCP/UDP scan of `10.0.8.60` should show **all ports closed** and **no mDNS** —
confirming the cloud MQTT session is the only control surface.

---

## Step 1 — Stand up the local broker on the HA VM

The device connects **anonymously**, which HA's `core-mosquitto` (go-auth) rejects. So
run a dedicated `eclipse-mosquitto:2` container that accepts anonymous on `:1893` and
bridges the device's topics into core-mosquitto.

```sh
# deploy the broker config (from this repo: tooling/neocharge-mqtt.conf)
ssh jp@10.0.0.10 "sudo mkdir -p /share/neocharge-mqtt"
cat tooling/neocharge-mqtt.conf | ssh jp@10.0.0.10 "sudo tee /share/neocharge-mqtt/mosquitto.conf >/dev/null"

# run it: host network so it can both serve :1893 and reach core-mosquitto on 127.0.0.1:1883
ssh jp@10.0.0.10 "sudo docker run -d --name neocharge-mqtt --restart unless-stopped \
  --network host \
  -v /mnt/data/supervisor/share/neocharge-mqtt:/mosquitto/config \
  eclipse-mosquitto:2"
```

The bridge in that config forwards `log` + `error` **up** (device → core-mosquitto, for
HA sensors) and `P12-XXX` **down** (HA commands/PING → device). It authenticates to
core-mosquitto as `jp` (the existing addon login).

Verify the container is up:

```sh
ssh jp@10.0.0.10 "sudo docker ps --filter name=neocharge-mqtt; sudo docker logs --tail 20 neocharge-mqtt"
```

> `--network host`: the container's sockets live in the host netns, so `ss`/`netstat`
> from inside the SSH-addon container will falsely show nothing. Verify reachability
> from the HA host itself or via router conntrack, not from inside an addon.

---

## Step 2 — DNAT + hairpin SNAT on gatekeeper (the redirect)

This is the cloud-sever. Full rule text + rollback in
[`../tooling/gatekeeper-dnat.md`](../tooling/gatekeeper-dnat.md). Back up the firewall first
(per homelab convention):

```sh
ssh root@10.0.0.2 "uci export firewall > /tmp/fw-backup-\$(date +%Y%m%d-%H%M%S).conf"
```

Because the device and broker are **both on VLAN 8** (same subnet), a bare DNAT is a
same-subnet hairpin: the broker would reply directly to `10.0.8.60` with src
`10.0.8.10`, but the device expects replies from `52.53.137.231` → asymmetric return →
silent CONNECT failure. **The SNAT/MASQUERADE is mandatory** so replies route back
through gatekeeper and get un-NAT'd both ways.

```sh
ssh root@10.0.0.2 "
  uci -q delete firewall.neocharge_dnat; uci -q delete firewall.neocharge_snat
  # DNAT: device's cloud session -> local broker :1893
  uci set firewall.neocharge_dnat=redirect
  uci set firewall.neocharge_dnat.name='neocharge-mqtt-dnat'
  uci set firewall.neocharge_dnat.src='iot'
  uci set firewall.neocharge_dnat.proto='tcp'
  uci set firewall.neocharge_dnat.src_ip='10.0.8.60'
  uci set firewall.neocharge_dnat.src_dip='52.53.137.231'
  uci set firewall.neocharge_dnat.src_dport='1883'
  uci set firewall.neocharge_dnat.dest='iot'
  uci set firewall.neocharge_dnat.dest_ip='10.0.8.10'
  uci set firewall.neocharge_dnat.dest_port='1893'
  uci set firewall.neocharge_dnat.target='DNAT'
  uci set firewall.neocharge_dnat.family='ipv4'
  # SNAT/hairpin: same-subnet return-path fix
  uci set firewall.neocharge_snat=nat
  uci set firewall.neocharge_snat.name='neocharge-mqtt-hairpin'
  uci set firewall.neocharge_snat.src='iot'
  uci set firewall.neocharge_snat.proto='tcp'
  uci set firewall.neocharge_snat.src_ip='10.0.8.60'
  uci set firewall.neocharge_snat.dest_ip='10.0.8.10'
  uci set firewall.neocharge_snat.dest_port='1893'
  uci set firewall.neocharge_snat.target='MASQUERADE'
  uci set firewall.neocharge_snat.family='ipv4'
  uci commit firewall && fw4 reload 2>&1 | tail -2
"
```

No manual forward-accept rule is needed: gatekeeper's `forward_iot` chain ends with
`ct status dnat accept` before its reject, so fw4 auto-accepts any DNAT'd flow
regardless of destination zone.

---

## Step 3 — Force the device to reconnect (flush conntrack)

`flow_offloading` is on, so the **existing** cloud session is `[OFFLOAD]` (fast-path,
bypasses netfilter) — the new DNAT rule will NOT catch it until the device re-dials.
Destroy the conntrack entry to force a reconnect:

```sh
ssh root@10.0.0.2 "conntrack -D -s 10.0.8.60 -p tcp --dport 1883"
```

(`conntrack` is installed on gatekeeper — apk pkg `conntrack`, plus
`kmod-nf-conntrack-netlink`.) The device is a clean-session MQTT client; it auto-retries
(~14 s cadence observed). Verify it landed on the local broker:

```sh
ssh root@10.0.0.2 "conntrack -L -s 10.0.8.60 -p tcp --dport 1883"
# expect reply tuple: src=10.0.8.10 dst=10.0.8.2 sport=1893  (DNAT + hairpin good)
```

A successful CONNACK is `20 02 00 00` (rc=0). If you see `20 02 00 05` (rc=5
not-authorized), the broker is rejecting anonymous — check `allow_anonymous true` in
the broker config and that the DNAT points at `:1893` (the anon listener), not core
`:1883`.

---

## Step 4 — Deploy the HA package

```sh
cat ha/neocharge.yaml | ssh jp@10.0.0.10 "sudo tee /homeassistant/packages/neocharge.yaml >/dev/null"

# config check, then reload (token bootstraps from env/cache/vault per ha CLAUDE.md)
TOKEN=$(bw get password ha-llat)
curl -sk -X POST https://homeassistant.local:8123/api/config/core/check_config \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
```

The package defines 8 sensors/binary_sensors, the breaker `number`, the charging
`switch`, and two automations (PING heartbeat + error→log mirror). **A full HA restart
is recommended** over a reload: manual MQTT entities lag ~10 s to subscribe their state
topic on reload (occasionally show "unknown"); a restart subscribes instantly and
re-reads the package cleanly.

```sh
curl -sk -X POST https://homeassistant.local:8123/api/services/homeassistant/restart \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
```

---

## Step 5 — Heartbeat (keeps telemetry alive post-cloud)

The device emits a full status on `log` **only in response to a server PING**. The cloud
is gone, so nothing pings it → telemetry would go quiet. The package's
`neocharge_ping_heartbeat` automation replays the PING to `P12-XXX` every 5 min (and on
HA start). The `neocharge_error_to_log_mirror` automation additionally mirrors the
autonomous `error`-topic status onto `log` so entities refresh **instantly** on a device
reconnect without waiting for the next 5-min PING.

Confirm telemetry is flowing: `sensor.neocharge_uptime` should keep increasing;
`sensor.neocharge_voltage` ≈ 240.

---

## Step 6 (one-time) — Capturing the command formats (Approach B)

If you ever need to re-derive the SET/SCHEDULE opcodes (e.g. a firmware update changes
them), use a **brief transparent MITM** instead of fuzzing the device:

1. Temporarily point the local broker's bridge **upstream to the real cloud**
   (`52.53.137.231:1883`) so the device keeps working and the app's commands flow
   through your broker.
2. On gatekeeper, capture the cloud-leg (`br-lan.38`) and device-leg (`br-lan.8`):
   ```sh
   ssh root@10.0.0.2 "tcpdump -ni br-lan.38 -s0 -w /tmp/nc-cmd-wan.pcap 'host 52.53.137.231 and tcp port 1883'"
   ```
3. Have JP tap the control in the app (set amperage, set a 1-minute test schedule).
4. Read the cleartext `P12-XXX` commands out of the pcap; map the `msg` opcodes.
5. **Re-sever**: tear down the upstream bridge, restore the DNAT-to-local config, flush
   conntrack. Cloud exposure window closed.

This is how `msg:50 SET_CURRENT_LIMIT` and `msg:11 SCHEDULE_SYNC` were captured.
**Do not** blind-fuzz integer opcodes on a live 240 V mains device — reboot / OTA /
factory-reset risk, low payoff.

---

## Rollback (restores cloud control, instant + reversible)

```sh
ssh root@10.0.0.2 "
  uci -q delete firewall.neocharge_dnat; uci -q delete firewall.neocharge_snat
  uci commit firewall && fw4 reload 2>&1 | tail -2
  conntrack -D -s 10.0.8.60 -p tcp --dport 1883   # drop redirect so device re-dials cloud
"
```

Full firewall restore if ever needed:
`uci import firewall < /tmp/fw-backup-<date>.conf && uci commit firewall && fw4 reload`.

Optionally stop the broker container: `ssh jp@10.0.0.10 "sudo docker rm -f neocharge-mqtt"`.

> Note: rolling back **re-enables the WiFi-PSK cleartext leak** to NeoCharge's cloud
> (see `docs/security.md`). Keep the redirect in place unless you specifically need the
> cloud app/OTA.

---

## Validation checklist

- [ ] `conntrack -L -s 10.0.8.60 -p tcp` shows reply `src=10.0.8.10 sport=1893` (hairpin good).
- [ ] `docker ps` shows `neocharge-mqtt` Up.
- [ ] CONNACK rc=0 on reconnect (not rc=5).
- [ ] `sensor.neocharge_voltage` ≈ 240, `sensor.neocharge_uptime` increasing.
- [ ] `number.neocharge_breaker_size` drives the device (set 30 A → ACK `limit:30` → restore 20 A).
- [ ] `switch.neocharge_charging` flips `binary_sensor.neocharge_relay` both ways.
- [ ] Survives an HA restart (entities re-subscribe, heartbeat resumes).
