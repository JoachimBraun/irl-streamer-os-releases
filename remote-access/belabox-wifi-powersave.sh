#!/usr/bin/env bash
# IRL Streamer OS - WLAN-Energiesparen auf der Belabox dauerhaft ausschalten.
#
# Hintergrund (live gemessen 20.10.2026): Das Belabox-Image bringt
# /etc/NetworkManager/conf.d/default-wifi-powersave-on.conf mit "wifi.powersave = 3" (an) mit. Auf dem
# Realtek-8852BE-WLAN (wlan2, RTMP-Kameras ueber den GL-X2000) hing der Link zweimal an einem Tag:
# "verbunden", aber kein Paket kam durch, beide Kameras rissen gleichzeitig ab; erst ein Neuaufbau der
# Verbindung half. Energiesparen ist bei solchen Chips ein bekannter Ausloeser.
#
# Dieses Skript legt /etc/NetworkManager/conf.d/zz-irl-wifi-powersave.conf mit "wifi.powersave = 2" an (aus).
# NetworkManager liest conf.d alphabetisch, die spaetere Datei gewinnt - die Image-Datei bleibt unangetastet.
# Wirkt bei der naechsten Verbindung/Neustart. Zusaetzlich wird es sofort per "iw ... set power_save off" gesetzt,
# ABER NUR, wenn gerade keine Kamera/kein Stream laeuft (sonst Gefahr eines Abrisses) - dann wirkt die Datei erst
# beim naechsten Neuaufbau der Verbindung.
#
# Aufruf: belabox-wifi-powersave.sh            einschalten (idempotent)
#         belabox-wifi-powersave.sh --remove   wieder entfernen (Rueckbau)
#
# Ausgabe (letzte Zeile): BELABOX_WIFI_PS_SET | BELABOX_WIFI_PS_UNCHANGED | BELABOX_WIFI_PS_REMOVED | BELABOX_WIFI_PS_FAILED
# davor eine Zeile "live: <angewendet|uebersprungen_stream_aktiv|nicht_noetig|fehler|-> ..." 
# Exit-Code ist immer 0 (die Einrichtung des Fernzugriffs darf daran nicht scheitern).
#
# Test-Schalter: IRL_NM_CONF_DIR (Default /etc/NetworkManager/conf.d), IRL_SKIP_SERVICE=1 (kein nmcli-Reload).
set -uo pipefail

CONF_DIR="${IRL_NM_CONF_DIR:-/etc/NetworkManager/conf.d}"
CONF="${CONF_DIR}/zz-irl-wifi-powersave.conf"
WANT=$'# IRL Streamer OS: WLAN-Energiesparen AUS (Rueckbau: remote-access/belabox-wifi-powersave.sh --remove)\n[connection]\nwifi.powersave = 2\n'

fail() { echo "belabox-wifi-powersave: $*" >&2; echo "BELABOX_WIFI_PS_FAILED"; exit 0; }

reload_nm() {
  [ "${IRL_SKIP_SERVICE:-0}" = "1" ] && return 0
  command -v nmcli >/dev/null 2>&1 && nmcli general reload conf >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--remove" ]; then
  if [ -f "${CONF}" ]; then
    rm -f "${CONF}" || fail "konnte ${CONF} nicht loeschen"
    reload_nm
  fi
  echo "BELABOX_WIFI_PS_REMOVED"
  exit 0
fi

[ -d "${CONF_DIR}" ] || fail "${CONF_DIR} existiert nicht (NetworkManager nicht installiert?)"

CHANGED=0
if [ "$(cat "${CONF}" 2>/dev/null)" != "${WANT%$'\n'}" ]; then
  TMP="${CONF}.irl-tmp"
  printf '%s' "${WANT}" > "${TMP}" || fail "konnte ${TMP} nicht schreiben"
  chmod 644 "${TMP}" && mv -f "${TMP}" "${CONF}" || { rm -f "${TMP}"; fail "konnte ${CONF} nicht anlegen"; }
  CHANGED=1
  reload_nm
fi

# --- Sofort anwenden, nur wenn nichts streamt ----------------------------------
stream_active() {
  pgrep -x belacoder >/dev/null 2>&1 && return 0
  pgrep -x srtla_send >/dev/null 2>&1 && return 0
  # RTMP-Kamera verbunden (direkt oder ueber die Schutzschicht-Ports): Peer ist nicht localhost
  ss -H -tn state established 2>/dev/null | awk '
    { n = split($3, l, ":"); port = l[n]; if (port == 1935 || port == 19350 || port == 19351) { if ($4 !~ /^127\./) found = 1 } }
    END { exit found ? 0 : 1 }' && return 0
  return 1
}

LIVE="-"
if command -v iw >/dev/null 2>&1; then
  IFACES="$(iw dev 2>/dev/null | awk '$1 == "Interface" { print $2 }')"
  NEED=""
  for i in ${IFACES}; do
    iw dev "$i" get power_save 2>/dev/null | grep -q "Power save: on" && NEED="${NEED} ${i}"
  done
  if [ -z "${NEED}" ]; then
    LIVE="nicht_noetig"
  elif stream_active; then
    LIVE="uebersprungen_stream_aktiv (${NEED# } bleibt bis zum naechsten Verbindungsaufbau/Neustart an)"
  else
    LIVE="angewendet"
    for i in ${NEED}; do
      iw dev "$i" set power_save off >/dev/null 2>&1 || LIVE="fehler (${i})"
    done
  fi
fi
echo "live: ${LIVE}"

if [ "${CHANGED}" = "1" ]; then echo "BELABOX_WIFI_PS_SET"; else echo "BELABOX_WIFI_PS_UNCHANGED"; fi
exit 0
