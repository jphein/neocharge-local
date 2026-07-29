# gatekeeper DNAT / cloud-sever — exact live rules

> Production-shaped firewall rules with genericized addresses — substitute your own.

gatekeeper = OpenWrt **25.12**, firewall = **fw4 / nftables** (`table inet fw4`).
VLAN8 leg `br-lan.8` @ `10.0.8.2` (VRRP VIP `.1`), WAN `br-lan.38` @ `10.38.0.2`.
Access: `ssh root@10.0.0.2` (READ-ONLY for audits: `uci show`, `cat`, `nft list`,
`iptables -S` — never `uci set`/`commit` during an audit).

These rules pull the NeoCharge device's persistent cloud-MQTT session off NeoCharge's
EC2 broker and onto the local broker on the HA VM. They are **live and persistent**
(in `/etc/config/firewall`, survive reboot). Verified present and counting 2026-06-14.

---

## Persistent UCI sections (`/etc/config/firewall`)

```
config redirect 'neocharge_dnat'
        option name 'neocharge-mqtt-dnat'
        option src 'iot'
        option proto 'tcp'
        option src_ip '10.0.8.60'
        option src_dip '52.53.137.231'
        option src_dport '1883'
        option dest 'iot'
        option dest_ip '10.0.8.10'
        option dest_port '1893'
        option target 'DNAT'
        option family 'ipv4'

config nat 'neocharge_snat'
        option name 'neocharge-mqtt-hairpin'
        option src 'iot'
        option proto 'tcp'
        option src_ip '10.0.8.60'
        option dest_ip '10.0.8.10'
        option dest_port '1893'
        option target 'MASQUERADE'
        option family 'ipv4'
```

## Live nftables (`inet fw4`) — verbatim, with live counters

```
ip saddr 10.0.8.60 ip daddr 52.53.137.231 tcp dport 1883 counter packets 11 bytes 484 dnat ip to 10.0.8.10:1893 comment "!fw4: neocharge-mqtt-dnat"
ip saddr 10.0.8.60 ip daddr 10.0.8.10 tcp dport 1893 counter packets 11 bytes 484 masquerade comment "!fw4: neocharge-mqtt-hairpin"
```

## The forward auto-accept (why no manual forward rule is needed)

gatekeeper's `forward_iot` chain ends with `ct status dnat accept` before its reject,
so fw4 auto-accepts any DNAT'd flow regardless of destination zone:

```
chain forward_iot {
    ip daddr 10.0.0.120 tcp dport 443 ... jump accept_to_admin  comment "!fw4: IoT-to-HA"
    ip daddr 10.0.0.10 tcp dport 8123 ... jump accept_to_admin comment "!fw4: IoT-to-HA-Direct"
    ip daddr 10.0.0.11  tcp dport 443 ... jump accept_to_admin  comment "!fw4: IoT-to-Caddy"
    ... udp sport { 1900, 3702, 9999, 48899 } ... jump accept_to_admin comment "!fw4: Discovery-Reply-iot-admin"
    ip daddr 10.0.0.10 udp dport 8047 ... jump accept_to_cameras comment "!fw4: Allow-JuiceBox-to-JPP"
    jump accept_to_wan comment "!fw4: Accept iot to wan forwarding"
    ct status dnat accept comment "!fw4: Accept port forwards"   <-- auto-accepts the DNAT'd MQTT flow
    jump reject_to_iot
}
```

---

## Why the hairpin SNAT is mandatory (same-subnet trap)

Device `10.0.8.60` and broker `10.0.8.10` are on the **same subnet** (VLAN 8). After
a bare DNAT, the broker would reply **directly** to the device with src `10.0.8.10`,
but the device expects replies from `52.53.137.231` → asymmetric return path → device
silently rejects the connection. The `MASQUERADE` rewrites the DNAT'd flow's source to
gatekeeper's VLAN8 IP (`10.0.8.2`), so replies route back **through** gatekeeper and get
un-NAT'd in both directions. This same-subnet asymmetry is very likely what was
historically logged on this network as "cross-VLAN CONNACK silently fails."

Confirmed on the wire: conntrack reply tuple `src=10.0.8.10 dst=10.0.8.2 sport=1893`
(DNAT + hairpin both applied), CONNACK `20 02 00 00` (rc=0).

---

## Apply (from a workstation, NOT during a read-only audit)

Back up first (homelab rule):

```sh
ssh root@10.0.0.2 "uci export firewall > /tmp/fw-backup-\$(date +%Y%m%d-%H%M%S).conf"
```

```sh
ssh root@10.0.0.2 "
  uci -q delete firewall.neocharge_dnat; uci -q delete firewall.neocharge_snat
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

Then force the device to re-dial so the new rule catches it (the existing session is
`[OFFLOAD]` and bypasses netfilter):

```sh
ssh root@10.0.0.2 "conntrack -D -s 10.0.8.60 -p tcp --dport 1883"
```

> Note on counters: once the new session is established it becomes `[OFFLOAD]`
> (flow-offloaded fast path) and **bypasses the NAT rules**, so the rule counters only
> tick on connection setup. Low/static counters on an established session are
> expected/correct, not a sign the rule isn't working — verify via conntrack instead.

`conntrack` CLI is installed on gatekeeper (apk pkg `conntrack` + `kmod-nf-conntrack-netlink`).

---

## Verify

```sh
ssh root@10.0.0.2 "
  nft list ruleset | grep -iE '52.53|1893|10.0.8.60'
  conntrack -L -s 10.0.8.60 -p tcp --dport 1883
"
# expect reply tuple src=10.0.8.10 dst=10.0.8.2 sport=1893 ([OFFLOAD] = stable)
```

---

## Rollback (instant, reversible — restores cloud control)

```sh
ssh root@10.0.0.2 "
  uci -q delete firewall.neocharge_dnat; uci -q delete firewall.neocharge_snat
  uci commit firewall && fw4 reload 2>&1 | tail -2
  conntrack -D -s 10.0.8.60 -p tcp --dport 1883
"
```

Full restore: `uci import firewall < /tmp/fw-backup-<date>.conf && uci commit firewall && fw4 reload`.

> ⚠️ Rolling back re-enables the device's cleartext WiFi-PSK leak to NeoCharge's cloud
> (see `../docs/security.md`). Keep the redirect unless you specifically need cloud
> app/OTA features.
