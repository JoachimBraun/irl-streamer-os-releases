#!/usr/bin/env bash
# IRL Streamer OS - WireGuard-Tunnelkonfiguration auf der Belabox final
# schreiben und aktivieren.
#
# Wird per SSH automatisch vom Mini-PC-Skript aufgerufen (siehe
# provision/irl-streamer-fernzugriff-einrichten.sh), NICHT manuell.
# Erwartet 3 Argumente: <minipc_host> <minipc_port> <minipc_pubkey>
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Bitte mit sudo ausfuehren." >&2
  exit 1
fi

MINIPC_HOST="${1:?Mini-PC-Host fehlt}"
MINIPC_PORT="${2:?Mini-PC-Port fehlt}"
MINIPC_PUBKEY="${3:?Mini-PC-Public-Key fehlt}"

WG_IF="wg0"
WG_DIR="/etc/wireguard"
WG_ADDR="10.10.10.2/24"
MINIPC_TUNNEL_IP="10.10.10.1"

cat > "${WG_DIR}/${WG_IF}.conf" <<EOF
[Interface]
PrivateKey = $(cat "${WG_DIR}/privatekey")
Address = ${WG_ADDR}

[Peer]
PublicKey = ${MINIPC_PUBKEY}
Endpoint = ${MINIPC_HOST}:${MINIPC_PORT}
# Bewusst NUR die Tunnel-IP des Mini-PCs, nicht 0.0.0.0/0 - der
# SRTLA-Stream-Traffic darf diesen Tunnel nicht nehmen (Bonding wuerde sonst
# kaputtgehen).
AllowedIPs = ${MINIPC_TUNNEL_IP}/32
PersistentKeepalive = 25
EOF
chmod 600 "${WG_DIR}/${WG_IF}.conf"

# WICHTIG (Nutzerfehler 2026-08-31, live reproduziert): "enable --now ||
# restart" greift NICHT zuverlaessig, wenn der Dienst schon vorher lief (z.B.
# erneute Ausfuehrung nach einer Fehlkonfiguration) - "enable --now" gibt
# dann bereits Exit-Code 0 zurueck, der "||"-Fallback zum Neuladen wird also
# NIE ausgeloest. Die neu geschriebene wg0.conf (mit neuem Peer-Key) liegt
# zwar korrekt auf der Platte, aber der laufende WireGuard-Kernel-Zustand
# behaelt weiterhin den ALTEN Peer-Key. Fix: enable und restart IMMER beide
# ausfuehren.
systemctl enable "wg-quick@${WG_IF}" 2>/dev/null || true
systemctl restart "wg-quick@${WG_IF}"

# BelaUI aus dem Bonding ausblenden lassen: belaUI.js baut seine
# Bonding-Interface-Liste aus ALLEN System-Netzwerkinterfaces und schliesst
# dabei nur "lo", "docker*" und "l4tbr*" explizit aus - wg0 fehlt in dieser
# Liste, taucht also automatisch als vermeintlicher Bonding-Kanal auf. Patch
# idempotent (grep -q Check).
BELAUI_JS="/opt/belaUI/belaUI.js"
if [ -f "${BELAUI_JS}" ] && ! grep -q "name.match('^wg')" "${BELAUI_JS}"; then
  sed -i "s#name\\.match('^l4tbr')) continue;#name.match('^l4tbr') || name.match('^wg')) continue;#" "${BELAUI_JS}"
  systemctl restart belaUI 2>/dev/null || true
fi

echo "BELABOX_WG_READY"
