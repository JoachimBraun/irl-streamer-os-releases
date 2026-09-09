#!/usr/bin/env bash
# IRL Streamer OS - Lizenz-Client
#
# Kommuniziert mit dem Lizenzserver (192.168.10.9:8400 im Heimnetz des
# Betreibers, oeffentliche Adresse folgt noch) und verwaltet den lokalen
# Lizenzstatus. Wird von provision.sh (Ersteinrichtung), einem taeglichen
# systemd-Timer (Ablauf-Check) und dem "Lizenz aktivieren"-Desktop-Icon
# aufgerufen.
#
# Lokale Zustandsdatei: /opt/irl-streamer-os/state/license.json
#   { "device_fingerprint": "...", "kind": "trial"|"license",
#     "expires_at": "...", "payload": "...", "signature": "..." }
#
# WICHTIG zum Sicherheitsmodell: Die lokale Pruefung (license-check.py)
# verifiziert die Ed25519-Signatur des Servers gegen den fest eingebetteten
# Public Key - ein Aendern von license.json ohne den privaten Schluessel
# des Servers (der NUR auf dem Server liegt) macht die Signatur ungueltig.
# Das Systemdatum selbst kann zwar lokal manipuliert werden, aendert aber
# NICHT das im Token eingebettete, server-signierte expires_at - ein
# vorgestelltes Datum wuerde also nur dazu fuehren, dass das Geraet
# faelschlich glaubt, die Lizenz sei noch nicht abgelaufen ODER schon
# abgelaufen, je nach Richtung - das ist ein Restrisiko, das bewusst in
# Kauf genommen wird (siehe Projekt-README: keine perfekte DRM-Loesung,
# nur eine Aufwandshuerde).

set -euo pipefail

LICENSE_SERVER_URL="${LICENSE_SERVER_URL:-https://lizenz.irlstreameros.de}"
PROJECT_DIR="/opt/irl-streamer-os"
STATE_DIR="${PROJECT_DIR}/state"
RESOLVER="${PROJECT_DIR}/provision/licensing/license-locate.py"
LICENSE_FILE="$(python3 "${RESOLVER}" license_file)"
FINGERPRINT_SCRIPT="${PROJECT_DIR}/provision/licensing/collect-fingerprint.sh"
LOG_PREFIX="[irl-license-client]"
LOCK_FILE="$(python3 "${RESOLVER}" lock_file)"

# shellcheck source=./license-service-lock.sh
source "${PROJECT_DIR}/provision/licensing/license-service-lock.sh"

log() { echo "${LOG_PREFIX} $*"; }

get_fingerprint() {
    bash "${FINGERPRINT_SCRIPT}"
}

# ---------------------------------------------------------------------------
# trial_start: einmalig bei der Ersteinrichtung aufgerufen (provision.sh).
# Idempotent auf Server-Seite - ein erneuter Aufruf (z.B. nach einem
# abgebrochenen provision.sh-Lauf) verlaengert die Testphase NICHT, sondern
# liefert denselben bereits gesetzten Ablauf zurueck.
# ---------------------------------------------------------------------------
cmd_trial_start() {
    local fp response
    fp="$(get_fingerprint)"

    log "Starte Testphase beim Lizenzserver (${LICENSE_SERVER_URL})..."
    if ! response="$(curl -fsS --max-time 15 -X POST "${LICENSE_SERVER_URL}/trial/start" \
        -H "Content-Type: application/json" \
        -d "{\"device_fingerprint\":\"${fp}\"}" 2>&1)"; then
        log "WARNUNG: Lizenzserver nicht erreichbar - Testphase kann jetzt nicht gestartet werden."
        log "  Fehler: ${response}"
        log "  Naechster Versuch beim naechsten taeglichen Check. Bis dahin gilt KEINE aktive Sperre."
        return 1
    fi

    mkdir -p "${STATE_DIR}"
    echo "${response}" > "${LICENSE_FILE}"
    chmod 600 "${LICENSE_FILE}"

    local expires
    expires="$(echo "${response}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["expires_at"])')"
    log "Testphase aktiv bis: ${expires}"

    bash "${PROJECT_DIR}/provision/licensing/license-widget-status.sh" || true
}

# ---------------------------------------------------------------------------
# activate: Nutzer gibt einen per E-Mail erhaltenen Code ein.
# ---------------------------------------------------------------------------
cmd_activate() {
    local code="${1:?Nutzung: $0 activate CODE}"
    local fp response http_code

    fp="$(get_fingerprint)"
    log "Aktiviere Lizenzcode..."

    response="$(curl -sS --max-time 15 -w '\n%{http_code}' -X POST "${LICENSE_SERVER_URL}/activate" \
        -H "Content-Type: application/json" \
        -d "{\"code\":\"${code}\",\"device_fingerprint\":\"${fp}\"}" 2>&1)" || {
        echo "FEHLER: Lizenzserver nicht erreichbar. Bitte Internetverbindung pruefen und erneut versuchen." >&2
        return 2
    }

    http_code="$(echo "${response}" | tail -1)"
    body="$(echo "${response}" | sed '$d')"

    if [ "${http_code}" != "200" ]; then
        local detail
        detail="$(echo "${body}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail","Unbekannter Fehler"))' 2>/dev/null || echo "${body}")"
        echo "FEHLER: ${detail}" >&2
        return 1
    fi

    mkdir -p "${STATE_DIR}"
    echo "${body}" > "${LICENSE_FILE}"
    chmod 600 "${LICENSE_FILE}"

    # Sofortige Freischaltung nach erfolgreicher Aktivierung, statt bis
    # zum naechsten taeglichen Timer-Lauf zu warten - der Nutzer, der
    # gerade einen Code eingegeben hat, erwartet, dass OBS/Guacamole/
    # Diagnostics sofort danach wieder nutzbar sind.
    if [ -f "${LOCK_FILE}" ]; then
        unlock_all_services
        rm -f "${LOCK_FILE}"
    fi

    local expires
    expires="$(echo "${body}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["expires_at"])')"
    echo "Lizenz erfolgreich aktiviert. Gueltig bis: ${expires}"

    bash "${PROJECT_DIR}/provision/licensing/license-widget-status.sh" || true
}

# ---------------------------------------------------------------------------
# refresh: wird VOM TAEGLICHEN TIMER aufgerufen, BEVOR die lokale Offline-
# Signaturpruefung laeuft. Holt den aktuellen Serverstand und ueberschreibt
# die lokale license.json damit, falls erreichbar - so wirken sich
# Admin-Aktionen (Sperren/Entsperren/Laufzeit aendern/Loeschen) auf ein
# bereits aktiviertes Geraet aus, statt fuer immer am zuletzt ausgestellten
# Token haengen zu bleiben.
#
# Rein additiv: schlaegt der Aufruf fehl (kein Internet unterwegs), bleibt
# die vorhandene license.json unveraendert bestehen und wird weiterhin
# offline geprueft - IRL-Streaming ohne Netz ist davon nicht betroffen.
#
# Exit 0 = license.json aktualisiert (oder Server bestaetigt aktuellen Stand)
# Exit 1 = Server erreichbar, meldet aber "geloescht/nicht bekannt" (404)
#          oder "gesperrt" (403) - lock_all_services wird HIER NICHT
#          aufgerufen, das bleibt Aufgabe von license-daily-check.sh anhand
#          der (jetzt aktualisierten) license.json, damit die Sperr-Logik
#          an einer einzigen Stelle bleibt.
# Exit 2 = Server nicht erreichbar (kein Fehler im eigentlichen Sinne)
# ---------------------------------------------------------------------------
cmd_refresh() {
    local fp response http_code body

    fp="$(get_fingerprint)"

    response="$(curl -sS --max-time 15 -w '\n%{http_code}' -X POST "${LICENSE_SERVER_URL}/refresh" \
        -H "Content-Type: application/json" \
        -d "{\"device_fingerprint\":\"${fp}\"}" 2>&1)" || {
        log "Refresh nicht moeglich (kein Netzwerk) - lokale Lizenzdatei bleibt unveraendert."
        return 2
    }

    http_code="$(echo "${response}" | tail -1)"
    body="$(echo "${response}" | sed '$d')"

    if [ "${http_code}" = "403" ]; then
        log "Server meldet: Lizenz wurde gesperrt (HTTP 403)."
        return 1
    fi

    if [ "${http_code}" = "404" ]; then
        log "Server kennt dieses Geraet nicht mehr (HTTP 404, z.B. nach Loeschen der Lizenz)."
        return 1
    fi

    if [ "${http_code}" != "200" ]; then
        log "WARNUNG: Unerwartete Antwort vom Lizenzserver (HTTP ${http_code}) - lokale Datei bleibt unveraendert."
        return 2
    fi

    mkdir -p "${STATE_DIR}"
    echo "${body}" > "${LICENSE_FILE}"
    chmod 600 "${LICENSE_FILE}"
    log "Lizenzstand mit Server synchronisiert."
    return 0
}

# ---------------------------------------------------------------------------
# status: gibt den aktuellen lokalen Lizenzstatus als JSON auf stdout aus,
# fuer die Nutzung durch andere Skripte (OBS-Start-Check, Dashboard,
# taeglicher Warn-Timer). Ruft license-check.py auf, das die Signatur
# tatsaechlich kryptografisch prueft (nicht nur das Ablaufdatum liest -
# ein manipuliertes JSON ohne gueltige Signatur wird als "invalid"
# eingestuft, nicht als "valid mit falschem Datum").
# ---------------------------------------------------------------------------
cmd_status() {
    if [ ! -f "${LICENSE_FILE}" ]; then
        echo '{"state":"none"}'
        return 0
    fi
    python3 "${PROJECT_DIR}/provision/licensing/license-check.py" "${LICENSE_FILE}"
}

case "${1:-}" in
    trial-start) cmd_trial_start ;;
    activate) cmd_activate "${2:-}" ;;
    refresh) cmd_refresh ;;
    status) cmd_status ;;
    *)
        echo "Nutzung: $0 {trial-start|activate CODE|refresh|status}" >&2
        exit 1
        ;;
esac
