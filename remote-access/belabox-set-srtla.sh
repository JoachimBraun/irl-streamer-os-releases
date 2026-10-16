#!/usr/bin/env bash
# IRL Streamer OS - SRTLA-Relay-Ziel in die belaUI-Konfiguration der Belabox eintragen.
#
# Wird per SSH automatisch vom Mini-PC-Skript aufgerufen (siehe
# provision/irl-streamer-fernzugriff-einrichten.sh), NICHT manuell.
# Aufruf: belabox-set-srtla.sh <srtla_host> <srtla_port>
# Setzt ausserdem IMMER die feste StreamID live/stream/belabox (so erwartet sie der
# Empfaenger, siehe docker/belabox/config.json.template) - auch wenn bereits ein
# anderer Wert eingetragen ist.
#
# belaUI liest config.json nur beim Start und schreibt sie bei jeder Aenderung
# komplett zurueck. Deshalb: belaUI stoppen, Felder srtla_addr/srtla_port/srt_streamid setzen
# (alle uebrigen Felder inkl. Passwort-Hashes bleiben unveraendert), belaUI starten.
# Laeuft gerade ein Stream (belacoder/srtla_send), wird NICHTS geaendert - ein
# belaUI-Neustart wuerde ihn beenden.
#
# Ausgabe (letzte Zeile, Marker fuer den Aufrufer):
#   BELABOX_SRTLA_SET | BELABOX_SRTLA_UNCHANGED | BELABOX_SRTLA_SKIPPED_STREAM_ACTIVE | BELABOX_SRTLA_FAILED
# Exit-Code ist immer 0 (die WireGuard-Einrichtung darf daran nicht scheitern).
#
# Test-Schalter: IRL_BELAUI_DIR (Default /opt/belaUI), IRL_SKIP_SERVICE=1 (kein systemctl/pgrep).
set -uo pipefail

HOST="${1:-}"
PORT="${2:-}"
STREAMID="live/stream/belabox"
DIR="${IRL_BELAUI_DIR:-/opt/belaUI}"
CFG="${DIR}/config.json"

fail() { echo "belabox-set-srtla: $*" >&2; echo "BELABOX_SRTLA_FAILED"; exit 0; }

[[ "${HOST}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || fail "ungueltiger Host '${HOST}'"
[[ "${PORT}" =~ ^[0-9]{1,5}$ ]] && [ "${PORT}" -ge 1 ] && [ "${PORT}" -le 65535 ] || fail "ungueltiger Port '${PORT}'"
[ -d "${DIR}" ] || fail "${DIR} existiert nicht (belaUI nicht installiert?)"
command -v node >/dev/null 2>&1 || fail "node nicht gefunden"

# Ein Node-Aufruf fuer Pruefen UND Schreiben (kein jq auf der Belabox noetig).
# Modus "check": Exit 0 = Ziel bereits gesetzt, 1 = Aenderung noetig.
# Modus "write": atomar schreiben (tmp + rename), Dateirechte bleiben erhalten.
NODE_JS='
const fs = require("fs");
const [cfgPath, mode, host, port, streamid] = process.argv.slice(1);
let c = {};
let existed = true;
try { c = JSON.parse(fs.readFileSync(cfgPath, "utf8")); }
catch (e) { if (e.code === "ENOENT") existed = false; else { console.error("config.json nicht lesbar: " + e.message); process.exit(2); } }
const same = existed && c.srtla_addr === host && Number(c.srtla_port) === Number(port) && c.srt_streamid === streamid && !c.relay_server && !c.relay_account;
if (mode === "check") process.exit(same ? 0 : 1);
c.srtla_addr = host;
c.srtla_port = Number(port);
c.srt_streamid = streamid;
delete c.relay_server;   // belaUI-Relay-Modus wuerde srtla_addr/srtla_port wieder loeschen
delete c.relay_account;  // ... und ein Relay-Account die StreamID
const tmp = cfgPath + ".irl-tmp";
let mode0 = 0o644;
try { mode0 = fs.statSync(cfgPath).mode & 0o777; } catch (e) {}
fs.writeFileSync(tmp, JSON.stringify(c), { mode: mode0 });
fs.renameSync(tmp, cfgPath);
'

if node -e "${NODE_JS}" "${CFG}" check "${HOST}" "${PORT}" "${STREAMID}"; then
  echo "BELABOX_SRTLA_UNCHANGED"
  exit 0
fi

if [ "${IRL_SKIP_SERVICE:-0}" != "1" ]; then
  if pgrep -x belacoder >/dev/null 2>&1 || pgrep -x srtla_send >/dev/null 2>&1; then
    echo "belabox-set-srtla: Stream laeuft - Konfiguration nicht geaendert" >&2
    echo "BELABOX_SRTLA_SKIPPED_STREAM_ACTIVE"
    exit 0
  fi
  systemctl stop belaUI.service 2>/dev/null || true
fi

node -e "${NODE_JS}" "${CFG}" write "${HOST}" "${PORT}" "${STREAMID}"
RC=$?

if [ "${IRL_SKIP_SERVICE:-0}" != "1" ]; then
  systemctl start belaUI.service 2>/dev/null || true
fi

[ "${RC}" -eq 0 ] || fail "Schreiben von ${CFG} fehlgeschlagen (rc=${RC})"
echo "BELABOX_SRTLA_SET"
exit 0
