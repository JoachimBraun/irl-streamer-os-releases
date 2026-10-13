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

# wg0 aus der belaUI-Verbindungsliste ausblenden - DAUERHAFT.
# belaUI.js baut seine Verbindungsliste aus ALLEN System-Netzwerkinterfaces und schliesst nur "lo", "docker*" und
# "l4tbr*" explizit aus; wg0 taucht sonst als vermeintlicher Sendeweg auf. Ein einmaliger sed-Patch reicht NICHT: ein
# Paket-Update von "belaui" (auf den Belabox-Images automatisch aktiv) ueberschreibt belaUI.js und damit den Patch
# (live erlebt am 12.10.2026: der Patch ging am 11.10. verloren, erst der naechste belaUI-Neustart machte es sichtbar).
# Deshalb: ein kleines idempotentes Skript, das systemd vor JEDEM Start von belaUI ausfuehrt (Drop-in ExecStartPre).
HIDE_WG="/usr/local/sbin/irl-hide-wg.sh"
HIDE_WG_DROPIN_DIR="/etc/systemd/system/belaUI.service.d"
BELAUI_JS="/opt/belaUI/belaUI.js"
cat > "${HIDE_WG}" <<'IRL_HIDE_WG'
#!/bin/sh
# IRL Streamer OS: blendet den WireGuard-Tunnel (wg*) in der belaUI-Verbindungsliste aus.
# Laeuft vor JEDEM Start von belaUI (systemd-Drop-in belaUI.service.d/10-irl-hide-wg.conf). Idempotent.
# Rueckbau: remote-access/belabox-teardown.sh
F="${IRL_BELAUI_JS:-/opt/belaUI/belaUI.js}"
[ -f "$F" ] || exit 0
if grep -q "name.match('^wg')" "$F"; then exit 0; fi
if ! grep -q "name.match('^l4tbr')) continue;" "$F"; then
  echo "irl-hide-wg: Zeile in belaUI.js nicht gefunden (neue belaUI-Version?) - wg0 wird NICHT ausgeblendet" >&2
  exit 0
fi
cp -p "$F" "$F.bak_vor_wg_patch" 2>/dev/null
sed -i "s#name\.match('^l4tbr')) continue;#name.match('^l4tbr') || name.match('^wg')) continue;#" "$F"
if grep -q "name.match('^wg')" "$F"; then echo "irl-hide-wg: Patch eingesetzt"; else echo "irl-hide-wg: Patch FEHLGESCHLAGEN" >&2; fi
exit 0
IRL_HIDE_WG
chmod 755 "${HIDE_WG}"
mkdir -p "${HIDE_WG_DROPIN_DIR}"
printf '[Service]\nExecStartPre=-/bin/sh %s\n' "${HIDE_WG}" > "${HIDE_WG_DROPIN_DIR}/10-irl-hide-wg.conf"
systemctl daemon-reload
# Sofort wirksam machen: war die Datei noch ungepatcht, patchen und belaUI neu starten (sonst laedt der laufende
# Prozess die Aenderung nicht).
if [ -f "${BELAUI_JS}" ] && ! grep -q "name.match('^wg')" "${BELAUI_JS}"; then
  sh "${HIDE_WG}" || true
  systemctl restart belaUI 2>/dev/null || true
fi

echo "BELABOX_WG_READY"
