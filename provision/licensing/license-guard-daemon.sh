#!/usr/bin/env bash
# IRL Streamer OS - Schneller Waechter gegen manuelles Wieder-Starten
# gesperrter Dienste (Haertung, Nutzerwunsch 2026-09-05)
#
# Laeuft dauerhaft im Hintergrund (systemd-Service, kein Timer/Oneshot -
# siehe irl-sysmaint-guard.service in provision.sh) und prueft alle paar
# Sekunden die Existenz der Sperr-Markierung (license-locked) UND
# (HAERTUNG 09.09., Nutzerwunsch) zusaetzlich - aber nur alle 60s statt
# jede 15s, um die zusaetzliche lokale Signaturpruefung minimal zu halten
# (keine Netzwerklast, CPU-Kosten pro Pruefung < 1ms, Ed25519-Verify) -
# den aktuellen Signaturstatus der lokalen license.json. Damit reicht es
# nicht mehr, nur die Lock-Datei zu loeschen UND den Container manuell zu
# starten: eine tatsaechlich abgelaufene Lizenz wird binnen maximal 60s
# erneut erkannt und die Dienste werden wieder gestoppt, unabhaengig vom
# Zustand der (loeschbaren) Lock-Datei. Setzt dabei selbst auch die
# Lock-Datei neu, damit license-guard.sh (OBS-Start) denselben Zustand
# sofort sieht, ohne auf den naechsten 4h-Timer-Lauf warten zu muessen.
#
# Bewusst weiterhin KEIN Ersatz fuer license-daily-check.sh: dieser
# Waechter macht keinen Online-Refresh (kein Netzwerk hier) - eine
# server-seitige Sperrung einer eigentlich noch gueltigen Lizenz wird
# weiterhin nur vom taeglichen Timer erkannt (siehe dortiger Online-
# Abgleich).

set -uo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
RESOLVER="${PROJECT_DIR}/provision/licensing/license-locate.py"
LICENSE_CHECK="${PROJECT_DIR}/provision/licensing/license-check.py"
LOCK_FILE="$(python3 "${RESOLVER}" lock_file)"
LICENSE_FILE="$(python3 "${RESOLVER}" license_file 2>/dev/null)"
CHECK_INTERVAL_SECONDS=15
SIGNATURE_RECHECK_EVERY_N_CYCLES=4  # 4 x 15s = alle 60s

# shellcheck source=./license-service-lock.sh
source "${PROJECT_DIR}/provision/licensing/license-service-lock.sh"

CONTROLLED_CONTAINERS_GUARD="belabox-receiver guacd guacamole irl-diagnostics"

log() { echo "[irl-sysmaint-guard] $*"; }

license_expired_by_signature() {
    [ -n "${LICENSE_FILE}" ] && [ -f "${LICENSE_FILE}" ] || return 1
    local state
    state="$(python3 "${LICENSE_CHECK}" "${LICENSE_FILE}" 2>/dev/null \
        | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("state","invalid"))
except Exception:
    print("invalid")' 2>/dev/null)"
    [ "${state}" = "expired" ]
}

log "Gestartet - pruefe alle ${CHECK_INTERVAL_SECONDS}s auf aktive Sperre, alle $((CHECK_INTERVAL_SECONDS * SIGNATURE_RECHECK_EVERY_N_CYCLES))s zusaetzlich die Signatur."

cycle=0
while true; do
    cycle=$((cycle + 1))

    # Zusaetzlicher Signatur-Recheck (seltener, da etwas teurer als ein
    # reiner Datei-Existenz-Check) - erkennt einen geloeschten LOCK_FILE
    # trotz tatsaechlich abgelaufener Lizenz und setzt ihn konsequent neu.
    if [ "$((cycle % SIGNATURE_RECHECK_EVERY_N_CYCLES))" -eq 0 ]; then
        if [ ! -f "${LOCK_FILE}" ] && license_expired_by_signature; then
            log "Lock-Datei fehlt, aber Lizenz ist laut Signatur abgelaufen - setze Sperre neu."
            echo "expired:signature-recheck:$(date -Is)" > "${LOCK_FILE}"
        fi
    fi

    if [ -f "${LOCK_FILE}" ] && command -v docker >/dev/null 2>&1; then
        # Erst pruefen ob ueberhaupt ein gesperrter Container laeuft, bevor
        # lock_all_services() aufgerufen wird - vermeidet, dass dessen
        # eigenes Log ("Stoppe lizenzpflichtige Dienste: ...") bei jedem
        # 15-Sekunden-Takt erneut geschrieben wird, obwohl in den meisten
        # Faellen ohnehin schon alles gestoppt ist (nur der seltene Fall
        # "Kunde hat manuell etwas gestartet" soll ueberhaupt sichtbar
        # geloggt werden).
        any_running=0
        for c in ${CONTROLLED_CONTAINERS_GUARD}; do
            if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ]; then
                any_running=1
                break
            fi
        done
        if [ "${any_running}" -eq 1 ]; then
            log "Gesperrter Dienst wurde manuell gestartet - stoppe erneut."
            lock_all_services
        fi
    fi
    sleep "${CHECK_INTERVAL_SECONDS}"
done
