#!/usr/bin/env bash
# =============================================================================
# deploy.sh — deploy the NeoCharge local-control stack onto the HA VM.
#
# Deploys the dedicated anonymous MQTT broker (eclipse-mosquitto:2) + the HA
# package, then config-checks and (optionally) restarts HA. Idempotent: re-run
# to push config changes.
#
# Does NOT touch gatekeeper firewall — that's a separate, deliberate step:
# see tooling/gatekeeper-dnat.md (and run it manually so the redirect is auditable).
#
# Run from the repo root:  ./tooling/deploy.sh
# Access: ssh jp@10.0.0.10 (HA VM). HA token via env HA_TOKEN -> cache -> vault.
# =============================================================================
set -euo pipefail

HA_VM="jp@10.0.0.10"
HA_URL="https://homeassistant.local:8123"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Deploying NeoCharge broker config to HA VM"
ssh "$HA_VM" "sudo mkdir -p /share/neocharge-mqtt"
ssh "$HA_VM" "sudo tee /share/neocharge-mqtt/mosquitto.conf >/dev/null" \
  < "$REPO_ROOT/tooling/neocharge-mqtt.conf"

echo "==> Ensuring neocharge-mqtt container is running"
if ssh "$HA_VM" "sudo docker ps -a --format '{{.Names}}' | grep -qx neocharge-mqtt"; then
  echo "    container exists -> restarting to pick up config"
  ssh "$HA_VM" "sudo docker restart neocharge-mqtt"
else
  echo "    creating container"
  ssh "$HA_VM" "sudo docker run -d --name neocharge-mqtt --restart unless-stopped \
    --network host \
    -v /mnt/data/supervisor/share/neocharge-mqtt:/mosquitto/config \
    eclipse-mosquitto:2"
fi

echo "==> Deploying HA package"
ssh "$HA_VM" "sudo tee /homeassistant/packages/neocharge.yaml >/dev/null" \
  < "$REPO_ROOT/ha/neocharge.yaml"

# --- HA token bootstrap (per ~/Projects/ha CLAUDE.md) ---
TOKEN="${HA_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$HOME/.cache/ha-token-tmp" ]; then
  TOKEN="$(cat "$HOME/.cache/ha-token-tmp")"
fi
if [ -z "$TOKEN" ] && command -v bw >/dev/null 2>&1; then
  TOKEN="$(bw get password ha-llat 2>/dev/null || true)"
fi
if [ -z "$TOKEN" ]; then
  echo "!! No HA token (env HA_TOKEN / ~/.cache/ha-token-tmp / vault 'ha-llat')."
  echo "   Config-check and restart skipped. Deploy of files succeeded."
  exit 0
fi

echo "==> Config check"
curl -sk -X POST "$HA_URL/api/config/core/check_config" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
echo

echo "==> Restart HA?  (recommended: manual MQTT entities subscribe instantly on restart"
echo "    vs ~10s lag on reload). Press y to restart, anything else to skip."
read -r ans
if [ "$ans" = "y" ]; then
  curl -sk -X POST "$HA_URL/api/services/homeassistant/restart" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
  echo "    restart requested"
else
  echo "    skipped — reload MQTT/automations manually if you don't restart"
fi

echo "==> Done. Verify: conntrack on gatekeeper + sensor.neocharge_uptime increasing."
