#!/usr/bin/env bash
# IRL Streamer OS - Fernzugriff-Einrichtung auf der Belabox rueckgaengig
# machen. Exaktes Gegenstueck zu belabox-setup.sh, macht dessen drei
# Schritte in umgekehrter Reihenfolge rueckgaengig:
#   1. belaUI.js-Bonding-Patch entfernen (wg0 taucht wieder in der
#      Bonding-Liste auf, wie vor der Einrichtung)
#   2. WireGuard-Tunnel abbauen (Dienst, Config, Schluesselpaar, Paket)
#   3. Dauerhaftes SSH zuruecknehmen (Werkszustand: nach dem naechsten
#      Neustart wieder aus, nur ueber BelaUIs eigenen Schalter aktivierbar)
#
# Idempotent - kann gefahrlos mehrfach ausgefuehrt werden, auch wenn nur
# ein Teil der Einrichtung tatsaechlich vorhanden ist. Funktioniert
# unabhaengig davon, ob die Einrichtung ueber den automatischen
# Schluesseltausch (provision/irl-streamer-fernzugriff-einrichten.sh, seit
# 2026-08-31 Standard) oder den fruaeheren manuellen Weg erfolgte - Config/
# Schluessel/belaUI-Patch sind in beiden Faellen identisch aufgebaut.
#
# Aufruf auf der Belabox (per SSH):
#   sudo bash belabox-teardown.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Bitte mit sudo ausfuehren: sudo bash $0" >&2
  exit 1
fi

WG_IF="wg0"
WG_DIR="/etc/wireguard"
BELAUI_JS="/opt/belaUI/belaUI.js"

echo "=== 1/3: belaUI-Bonding-Patch rueckgaengig machen ==="
if [ -f "${BELAUI_JS}" ] && grep -q "name.match('^wg')" "${BELAUI_JS}"; then
  sed -i "s#name\.match('^l4tbr') || name.match('^wg')) continue;#name.match('^l4tbr')) continue;#" "${BELAUI_JS}"
  systemctl restart belaUI
  echo "belaUI.js zurueckgesetzt - wg0 taucht (nach dem Tunnelabbau unten) wieder normal als Interface auf."
else
  echo "Kein Patch gefunden, nichts zu tun."
fi

echo ""
echo "=== 2/3: WireGuard-Tunnel abbauen ==="
systemctl disable --now "wg-quick@${WG_IF}" 2>/dev/null || true
rm -f "${WG_DIR}/${WG_IF}.conf" "${WG_DIR}/privatekey" "${WG_DIR}/publickey"
if dpkg -l wireguard >/dev/null 2>&1 || dpkg -l wireguard-tools >/dev/null 2>&1; then
  apt-get remove -y wireguard wireguard-tools >/dev/null 2>&1 || true
fi
echo "Tunnel, Konfiguration, Schluesselpaar und Paket entfernt."
echo "HINWEIS: Falls diese Belabox spaeter erneut eingerichtet wird, entsteht"
echo "ein NEUES Schluesselpaar - der Mini-PC muss dann erneut mit dem neuen"
echo "Public Key dieser Belabox versorgt werden (siehe belabox-setup.sh)."

echo ""
echo "=== 3/3: Dauerhaftes SSH zuruecknehmen ==="
# Bewusst nur "disable", NICHT "stop" - stop wuerde die gerade laufende
# SSH-Sitzung kappen, ueber die dieses Skript vermutlich selbst ausgefuehrt
# wird. disable wirkt erst beim naechsten Neustart.
systemctl disable ssh 2>/dev/null || true
echo "SSH bleibt fuer diese Sitzung nutzbar, ist aber ab dem naechsten"
echo "Neustart wieder aus (Werkszustand) - danach nur noch ueber BelaUIs"
echo "eigenen SSH-Schalter aktivierbar, nicht mehr dauerhaft."

echo ""
echo "Fertig. Alle Aenderungen von belabox-setup.sh sind rueckgaengig gemacht."
