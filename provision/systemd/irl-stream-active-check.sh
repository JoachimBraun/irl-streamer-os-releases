#!/usr/bin/env bash
# IRL Streamer OS - "Darf eine stoerende Hintergrundaktion JETZT laufen?"
#
# Exit 0 = KEIN Stream aktiv -> Aktion darf laufen
# Exit 1 = Stream aktiv (oder unklar, siehe --unknown) -> ueberspringen
#
# Geeignet als systemd-ExecCondition (Exit 1..254 = Lauf lautlos
# ueberspringen, kein Fehler). Genutzt von apt-daily-upgrade (nachts,
# Sicherheitsupdates) und irl-streamer-update-check.sh.
#
# V1.85: die eigentliche Erkennung steckt in provision/lib/stream-active.py
# (OBS-Websocket GetStreamStatus/GetRecordStatus + SLS-Publisher auf
# :8181/stats). Die fruehere RX-Queue-Heuristik (/proc/net/udp) war fast
# immer 0 -> Streams wurden faelschlich als "inaktiv" erkannt und ein
# Update konnte einen laufenden Stream abbrechen.
#
# --unknown=skip (Standard, fail-safe): Erkennung fehlgeschlagen -> wie
#                aktiv behandeln (lieber ein Update verschieben als einen
#                Stream abbrechen).
# --unknown=run: Erkennung fehlgeschlagen -> trotzdem laufen lassen.
set -uo pipefail

UNKNOWN_POLICY="skip"
case "${1:-}" in
    --unknown=run) UNKNOWN_POLICY="run" ;;
    --unknown=skip|"") UNKNOWN_POLICY="skip" ;;
esac

HELPER="/opt/irl-streamer-os/provision/lib/stream-active.py"
if [ ! -f "${HELPER}" ]; then
    echo "[irl-stream-active-check] ${HELPER} fehlt - Zustand unbekannt."
    [ "${UNKNOWN_POLICY}" = "run" ] && exit 0
    exit 1
fi

OUT="$(timeout 20 python3 "${HELPER}" 2>&1)"
RC=$?
echo "[irl-stream-active-check] ${OUT}"
case "${RC}" in
    0) exit 0 ;;
    1) exit 1 ;;
    *) [ "${UNKNOWN_POLICY}" = "run" ] && exit 0; exit 1 ;;
esac
