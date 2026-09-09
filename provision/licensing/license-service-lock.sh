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

CONTROLLED_CONTAINERS="belabox-receiver guacd guacamole irl-diagnostics"

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
