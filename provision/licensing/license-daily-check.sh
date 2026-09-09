#!/usr/bin/env bash
# IRL Streamer OS - Taeglicher Lizenz-/Testphasen-Check
#
# Laeuft periodisch per systemd-Timer (irl-license-check.timer, alle 4h
# + 5min nach jedem Boot) als root. Prueft den lokalen Lizenzstatus (komplett offline, siehe
# license-check.py) und:
#   - ab 7 Tagen vor Ablauf: zeigt dem eingeloggten Nutzer einen Warnhinweis
#   - nach Ablauf: setzt eine Sperr-Markierung, die OBS-Autostart und das
#     Diagnose-Dashboard auswerten (siehe license-guard.sh)
#
# Sperr-Markierung: /opt/irl-streamer-os/state/license-locked
# (Existenz dieser Datei = gesperrt; Inhalt ist nur zur Diagnose lesbar)
#
# Warum eine separate Sperr-Datei statt direkt bei jedem OBS-Start die volle
# Pruefung laufen zu lassen: OBS/Dashboard-Startskripte sollen schnell und
# ohne Netzwerkabhaengigkeit bleiben - ein einfacher Datei-Existenz-Check
# ist trivial und schnell, waehrend dieser Timer die eigentliche (etwas
# aufwendigere) Pruefung + Nutzerbenachrichtigung uebernimmt.

set -euo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
STATE_DIR="${PROJECT_DIR}/state"
RESOLVER="${PROJECT_DIR}/provision/licensing/license-locate.py"
LICENSE_FILE="$(python3 "${RESOLVER}" license_file)"
LOCK_FILE="$(python3 "${RESOLVER}" lock_file)"
WARN_SHOWN_FILE="${STATE_DIR}/license-warning-shown-for"
TARGET_USER="streamer"
WARNING_DAYS_BEFORE_EXPIRY=7
LOG_PREFIX="[irl-license-check]"

# shellcheck source=./license-service-lock.sh
source "${PROJECT_DIR}/provision/licensing/license-service-lock.sh"

log() { echo "${LOG_PREFIX} $*"; }

# Zenity-Dialog im Kontext der eingeloggten grafischen Sitzung anzeigen -
# gleiches Muster wie der Icon-Trust-Fix/die Abschluss-Zusammenfassung in
# provision.sh (sudo -u mit expliziter XDG_RUNTIME_DIR/DBUS/WAYLAND_DISPLAY).
show_dialog() {
    local title="$1" text="$2" icon="${3:-warning}"
    if ! command -v zenity >/dev/null 2>&1; then
        log "zenity nicht verfuegbar - Hinweis nur im Log: ${title}: ${text}"
        return 0
    fi
    local uid
    uid="$(id -u "${TARGET_USER}" 2>/dev/null || true)"
    [ -z "${uid}" ] && return 0
    sudo -u "${TARGET_USER}" \
        XDG_RUNTIME_DIR="/run/user/${uid}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
        WAYLAND_DISPLAY="wayland-0" \
        zenity --"${icon}" --title="${title}" --text="${text}" --width=420 2>/dev/null &
    disown
}

main() {
    mkdir -p "${STATE_DIR}"

    # Widget-Anzeige am Ende IMMER aktualisieren, egal welcher Zweig unten
    # greift - simple trap statt an jeder Stelle einzeln aufzurufen.
    trap 'bash "${PROJECT_DIR}/provision/licensing/license-widget-status.sh" || true' EXIT

    # Online-Abgleich VOR der lokalen Offline-Pruefung: eine Admin-Aktion
    # (Sperren/Entsperren/Laufzeit aendern/Loeschen) auf dem Server wirkt
    # sich sonst NIE auf ein bereits aktiviertes Geraet aus, weil dieses
    # ausschliesslich seinen alten, lokal gespeicherten Token pruefen
    # wuerde. refresh() aktualisiert license.json bei Erfolg, oder meldet
    # per Exit-Code 1 explizit "Server sagt: nicht mehr gueltig".
    #
    # Rein additiv: kein Internet -> refresh() gibt 2 zurueck, wir machen
    # dann normal mit der bestehenden lokalen Datei weiter (Offline-Betrieb
    # unterwegs bleibt unveraendert funktionsfaehig).
    if [ -f "${LICENSE_FILE}" ]; then
        set +e
        bash "${PROJECT_DIR}/provision/licensing/license-client.sh" refresh
        local refresh_rc=$?
        set -e
        if [ "${refresh_rc}" -eq 1 ]; then
            # Server ist erreichbar und sagt explizit: dieser Code ist
            # gesperrt ODER wurde komplett geloescht. Sofort sperren, OHNE
            # auf den naechsten "expired"-Zustand der (jetzt evtl. noch
            # unveraendert "gueltigen") lokalen Datei zu warten - genau
            # das ist der Fall, den ein Admin-Sperren/Loeschen ueberhaupt
            # erst bewirken soll.
            if [ ! -f "${LOCK_FILE}" ]; then
                log "Server hat diese Lizenz gesperrt oder geloescht - aktiviere Sperre sofort."
                echo "server-revoked:$(date -Is)" > "${LOCK_FILE}"
                lock_all_services
                show_dialog "IRL Streamer OS - gesperrt" \
                    "Deine Lizenz wurde vom Betreiber gesperrt oder entfernt.\n\nOBS, Fernzugriff (Guacamole) und das Diagnose-Dashboard wurden gestoppt.\n\nBitte einen neuen Aktivierungscode ueber das Desktop-Icon 'Lizenz aktivieren' eingeben." \
                    "error"
            fi
            return 0
        fi
    fi

    if [ ! -f "${LICENSE_FILE}" ]; then
        log "Noch keine Lizenz-/Testphasen-Datei vorhanden - versuche Trial-Start erneut."
        bash "${PROJECT_DIR}/provision/licensing/license-client.sh" trial-start \
            || log "Trial-Start weiterhin nicht moeglich (kein Netzwerk?) - naechster Versuch morgen."
        return 0
    fi

    local status_json state kind expires_at days_remaining
    status_json="$(python3 "${PROJECT_DIR}/provision/licensing/license-check.py" "${LICENSE_FILE}")"
    state="$(echo "${status_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')"

    case "${state}" in
        valid)
            kind="$(echo "${status_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kind"])')"
            days_remaining="$(echo "${status_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["days_remaining"])')"

            # Sperre aufheben, falls sie durch eine zwischenzeitliche
            # Aktivierung nicht mehr mehr gerechtfertigt ist - UND die
            # zuvor gestoppten Dienste wieder starten, falls sie gesperrt
            # waren (unlock_all_services ist ein No-Op, wenn nichts zu tun
            # ist - docker start auf einen bereits laufenden Container
            # schadet nicht).
            if [ -f "${LOCK_FILE}" ]; then
                unlock_all_services
            fi
            rm -f "${LOCK_FILE}"

            if [ "${days_remaining}" -le "${WARNING_DAYS_BEFORE_EXPIRY}" ]; then
                # Nur EINMAL pro verbleibendem Tageswert warnen, nicht bei
                # jedem taeglichen Lauf erneut mit identischem Text -
                # WARN_SHOWN_FILE merkt sich den zuletzt angezeigten Wert.
                local last_shown=""
                [ -f "${WARN_SHOWN_FILE}" ] && last_shown="$(cat "${WARN_SHOWN_FILE}")"
                if [ "${last_shown}" != "${days_remaining}" ]; then
                    local noun="Testphase"
                    [ "${kind}" = "license" ] && noun="Lizenz"
                    show_dialog "IRL Streamer OS" \
                        "Deine ${noun} laeuft in ${days_remaining} Tag(en) ab.\n\nBei einer Testphase: einen Aktivierungscode per E-Mail anfordern und ueber das Desktop-Icon 'Lizenz aktivieren' eingeben.\nBei einer bestehenden Lizenz: rechtzeitig verlaengern." \
                        "warning"
                    echo "${days_remaining}" > "${WARN_SHOWN_FILE}"
                fi
            fi
            log "Status: gueltig (${kind}), noch ${days_remaining} Tag(e)."
            ;;

        expired)
            kind="$(echo "${status_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kind"])')"
            if [ ! -f "${LOCK_FILE}" ]; then
                local noun="Testphase"
                [ "${kind}" = "license" ] && noun="Lizenz"
                log "${noun} abgelaufen - aktiviere Sperre (OBS, SRTLA-Relay, NOALBS, Guacamole, IRL-Diagnostics)."
                echo "expired:${kind}:$(date -Is)" > "${LOCK_FILE}"
                lock_all_services
                show_dialog "IRL Streamer OS - gesperrt" \
                    "Deine ${noun} ist abgelaufen.\n\nOBS, Fernzugriff (Guacamole) und das Diagnose-Dashboard wurden gestoppt.\n\nBitte einen Aktivierungscode ueber das Desktop-Icon 'Lizenz aktivieren' eingeben, um IRL Streamer OS weiter zu nutzen." \
                    "error"
            fi
            ;;

        invalid|*)
            # Ungueltige Signatur/kaputte Datei wird wie "kein Trial
            # gestartet" behandelt, NICHT automatisch als Sperre - ein
            # neuer Trial-Start-Versuch klaert das (liefert entweder den
            # bereits bestehenden Server-Trial erneut aus, oder startet
            # einen neuen, falls dieses Geraet dem Server noch unbekannt
            # ist). Verhindert, dass ein einmaliger Uebertragungsfehler
            # (z.B. abgebrochener Schreibvorgang bei Stromausfall) das
            # Geraet faelschlich dauerhaft sperrt.
            log "Lokale Lizenzdatei ungueltig (${state}) - versuche erneuten Trial-Start."
            bash "${PROJECT_DIR}/provision/licensing/license-client.sh" trial-start \
                || log "Trial-Start weiterhin nicht moeglich."
            ;;
    esac
}

main "$@"
