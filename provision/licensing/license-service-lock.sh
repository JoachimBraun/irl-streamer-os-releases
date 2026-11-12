#!/usr/bin/env bash
# IRL Streamer OS - Dienste sperren/entsperren bei abgelaufener Lizenz
#
# Wird von license-daily-check.sh (Sperre setzen) und license-client.sh
# (Sperre bei erfolgreicher Aktivierung sofort aufheben) eingebunden -
# "source"-Datei, kein eigenstaendiges Skript (daher kein Shebang-Aufruf
# noetig, wird per "source" geladen).
#
# Gesperrte Dienste bei abgelaufener Lizenz (Nutzerwunsch): OBS-Start
# (siehe license-guard.sh, separat), SRTLA-Relay + NOALBS (beide im
# Container "belabox-receiver" gebuendelt, siehe docker/docker-compose.yml),
# Guacamole (Web-Oberflaeche "guacamole" + Proxy-Daemon "guacd" - die
# Datenbank "guacamole-db" bleibt bewusst laufen, ein sauberes Stoppen
# der App-Ebene reicht, um die NUTZUNG zu verhindern, ohne unnoetiges
# Postgres-Stop/Start-Risiko), IRL-Diagnostics-Dashboard.
#
# NICHT gestoppt: der lokale Caddy-HTTPS-Proxy selbst - bleibt bewusst
# aktiv, damit ein Aufruf von https://<geraet>/ wenigstens eine Fehler-
# antwort (502, Ziel nicht erreichbar) statt eines kompletten
# Verbindungsabbruchs liefert - etwas nachvollziehbarer fuer den Nutzer.

# V1.85: filebrowser ergaenzt (Datei-Upload ist ebenfalls lizenzpflichtige
# Funktion und war bisher trotz Sperre weiter oeffentlich erreichbar).
CONTROLLED_CONTAINERS="belabox-receiver guacd guacamole irl-diagnostics filebrowser"

_license_lock_log() { echo "[irl-license-lock] $*"; }

lock_all_services() {
    if ! command -v docker >/dev/null 2>&1; then
        _license_lock_log "WARNUNG: docker nicht verfuegbar - Dienste koennen nicht gesperrt werden."
        return 0
    fi
    _license_lock_log "Stoppe lizenzpflichtige Dienste: ${CONTROLLED_CONTAINERS}"
    for c in ${CONTROLLED_CONTAINERS}; do
        docker stop "${c}" >/dev/null 2>&1 || true
    done
}

unlock_all_services() {
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    _license_lock_log "Starte lizenzpflichtige Dienste wieder: ${CONTROLLED_CONTAINERS}"
    for c in ${CONTROLLED_CONTAINERS}; do
        docker start "${c}" >/dev/null 2>&1 || true
    done
}

# --- Stream-Aufschub der Sperre (Review 29.10.2026 B4, Review 11.11.2026 A3) ---------
# Gemeinsame Regel fuer license-daily-check.sh (apply_lock) UND den Sperr-Waechter
# license-guard-daemon.sh - frueher hatte nur der Daily-Check die Stream-Pruefung, der
# Waechter (60-s-Takt) kam ihm meist zuvor und stoppte den laufenden Stream ohne Aufschub.
# Regel: laeuft ein Stream (oder ist die Erkennung unklar, irl-stream-active-check.sh
# Exit != 0), wird die Sperre aufgeschoben - hoechstens <max> Sekunden ab dem ersten
# Aufschub (Marker-Datei mit Startzeit). Marker in der Zukunft, mit Unsinn oder aelter als
# die Hoechstdauer schieben NICHTS auf.
# shellcheck disable=SC2034  # beide werden von den einbindenden Skripten gelesen
LICENSE_LOCK_DEFER_AGE=0
LICENSE_LOCK_STREAM_ACTIVE=0

# Rueckgabe 0, wenn der Marker gueltig (Zahl, nicht in der Zukunft) und juenger als
# <max> ist; das Alter steht danach in LICENSE_LOCK_DEFER_AGE.
license_lock_defer_fresh() {
    local file="$1" max="$2" since
    since="$(cat "${file}" 2>/dev/null || true)"
    # >11 Ziffern: kein plausibler Zeitstempel (Schutz vor Ueberlauf der Bash-Arithmetik)
    case "${since}" in ''|*[!0-9]*|????????????*) return 1 ;; esac
    LICENSE_LOCK_DEFER_AGE=$(( $(date +%s) - since ))
    [ "${LICENSE_LOCK_DEFER_AGE}" -ge 0 ] && [ "${LICENSE_LOCK_DEFER_AGE}" -lt "${max}" ]
}

# Rueckgabe 0 = Sperre JETZT aufschieben (Stream laeuft, Hoechstdauer nicht erreicht;
# legt den Marker beim ersten Aufschub an), 1 = jetzt sperren. LICENSE_LOCK_STREAM_ACTIVE=1
# heisst: Stream laeuft, aber die Hoechstdauer ist erreicht (Sperre trotzdem).
# shellcheck disable=SC2034  # LICENSE_LOCK_STREAM_ACTIVE liest der Aufrufer
license_lock_defer_for_stream() {
    local file="$1" max="$2" check="$3" since
    LICENSE_LOCK_STREAM_ACTIVE=0
    [ -f "${check}" ] || return 1
    bash "${check}" >/dev/null 2>&1 && return 1
    LICENSE_LOCK_STREAM_ACTIVE=1
    since="$(cat "${file}" 2>/dev/null || true)"
    case "${since}" in ''|*[!0-9]*|????????????*) date +%s > "${file}" ;; esac
    license_lock_defer_fresh "${file}" "${max}"
}
