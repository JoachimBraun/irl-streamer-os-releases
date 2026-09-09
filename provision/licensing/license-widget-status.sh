#!/usr/bin/env bash
# IRL Streamer OS - Erzeugt die Statuszeile fuer das Desktop-Lizenz-Widget
# (GNOME-Shell-Erweiterung irl-license-widget@irlstreameros.de, zeigt den
# Text unten rechts auf dem Desktop an).
#
# Wird aufgerufen von: license-daily-check.sh (nach jeder periodischen
# Pruefung), license-client.sh (sofort nach trial-start/activate, damit
# das Widget nicht erst auf den naechsten taeglichen Lauf warten muss),
# und einmalig aus provision.sh nach dem ersten Trial-Start.
#
# Schreibt reinen Text (eine Zeile, UTF-8) nach state/license-widget-text.txt.
# Die Erweiterung liest diese Datei periodisch selbst - dieses Skript weiss
# nichts von GNOME Shell und die Erweiterung weiss nichts von Lizenzlogik,
# sauber getrennt ueber die Datei.

set -euo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
STATE_DIR="${PROJECT_DIR}/state"
RESOLVER="${PROJECT_DIR}/provision/licensing/license-locate.py"
LICENSE_FILE="$(python3 "${RESOLVER}" license_file)"
LOCK_FILE="$(python3 "${RESOLVER}" lock_file)"
OUT_FILE="${STATE_DIR}/license-widget-text.txt"

mkdir -p "${STATE_DIR}"

# Versionsanzeige (Nutzerwunsch 2026-09-09): der Kunde soll jederzeit auf
# dem Desktop nachschauen koennen, welche IRL-Streamer-OS-Version installiert
# ist - angehaengt an denselben dezenten Text unten rechts, auf dem bereits
# die Lizenzgueltigkeit steht (kein zusaetzliches, separates Widget noetig).
# VERSION-Datei enthaelt "1.69" (siehe iso-build/build-desktop-autoinstall-
# iso.sh) - wird hier zu "V1_69" umformatiert, dem auf dem ISO-Dateinamen
# vertrauten Schema (siehe z.B. IRL-Streamer-OS-2.0_V1_69_...iso), damit der
# Kunde die Desktop-Anzeige direkt mit einer ihm evtl. genannten Versions-
# nummer (Support, Changelog) abgleichen kann.
VERSION_FILE="${PROJECT_DIR}/VERSION"
VERSION_SUFFIX=""
if [ -f "${VERSION_FILE}" ]; then
    RAW_VERSION="$(cat "${VERSION_FILE}" | tr -d '[:space:]')"
    if [ -n "${RAW_VERSION}" ]; then
        VERSION_SUFFIX=" · V${RAW_VERSION//./_}"
    fi
fi

# ALARM-Praefix (Nutzerwunsch 2026-09-04): fuer gesperrt/abgelaufen soll der
# Text in der Desktop-Anzeige knallrot UND dauerhaft blinkend dargestellt
# werden, statt nur dezent grau wie im Normalfall. Dieses Skript kennt
# weiterhin keinerlei GNOME-Shell-Details (bleibt sauber getrennt, siehe
# Modul-Kommentar oben) - es haengt lediglich ein einzelnes Steuerzeichen
# (!ALARM!) als Praefix vor kritische Texte, das extension.js beim
# Einlesen erkennt/abtrennt und rein fuer die Styling-Entscheidung nutzt.
write_text() {
    printf '%s%s' "$1" "${VERSION_SUFFIX}" > "${OUT_FILE}"
    chmod 644 "${OUT_FILE}"
}

write_alarm_text() {
    printf '!ALARM!%s%s' "$1" "${VERSION_SUFFIX}" > "${OUT_FILE}"
    chmod 644 "${OUT_FILE}"
}

# WARN-Praefix (Nutzerwunsch 2026-09-04): eine noch aktive Jahreslizenz,
# die in weniger als 2 Wochen ablaeuft, soll rechtzeitig auffallen, aber
# NICHT wie eine bereits gesperrte/abgelaufene Lizenz behandelt werden -
# also knalliges Gelb statt Rot, und bewusst OHNE Blinken (Nutzerwunsch:
# "kein Blinken einfach nur knalliges Gelb"). extension.js unterscheidet
# "!ALARM!" und "!WARN!" als zwei getrennte, exklusive Praefixe.
WARN_DAYS_THRESHOLD=14

write_warn_text() {
    printf '!WARN!%s%s' "$1" "${VERSION_SUFFIX}" > "${OUT_FILE}"
    chmod 644 "${OUT_FILE}"
}

# Sperr-Markierung hat IMMER Vorrang vor dem reinen Token-Status (Bugfix
# 2026-09-04, Nutzer-Fund: nach einem Admin-Sperren im Lizenzportal stand
# auf dem Desktop weiterhin "Lizenz gültig bis..." - obwohl Guacamole/
# IRL-Diagnostics bereits korrekt gestoppt waren). Ursache: dieses Skript
# pruefte bisher AUSSCHLIESSLICH die lokale, offline signierte Token-Datei
# (license-check.py) - die bleibt bei einer Server-Sperre unveraendert
# gueltig (Signatur/Ablaufdatum aendern sich durch ein Sperren nicht), die
# Sperr-Markierung selbst (state/license-locked, siehe license-service-
# lock.sh) wurde hier nie abgefragt. Erst pruefen, dann ggf. sofort mit
# einem eindeutigen Sperr-Text abbrechen, ohne ueberhaupt erst den
# (weiterhin technisch "gueltigen") Token auszuwerten.
if [ -f "${LOCK_FILE}" ]; then
    if grep -q '^server-revoked:' "${LOCK_FILE}" 2>/dev/null; then
        write_alarm_text "Lizenz wurde vom Betreiber gesperrt. Bitte Kontakt aufnehmen"
    else
        write_alarm_text "Lizenz/Testphase abgelaufen - bitte verlängern"
    fi
    exit 0
fi

if [ ! -f "${LICENSE_FILE}" ]; then
    write_text "Testversion wird eingerichtet..."
    exit 0
fi

STATUS_JSON="$(python3 "${PROJECT_DIR}/provision/licensing/license-check.py" "${LICENSE_FILE}")"
STATE="$(echo "${STATUS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')"

case "${STATE}" in
    valid)
        KIND="$(echo "${STATUS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kind"])')"
        if [ "${KIND}" = "license" ]; then
            EXPIRES_AT="$(echo "${STATUS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["expires_at"])')"
            DE_DATE="$(python3 -c "from datetime import datetime; print(datetime.fromisoformat('${EXPIRES_AT}').strftime('%d.%m.%Y'))")"
            LIC_DAYS="$(echo "${STATUS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["days_remaining"])')"
            if [ "${LIC_DAYS}" -lt "${WARN_DAYS_THRESHOLD}" ]; then
                if [ "${LIC_DAYS}" -eq 1 ]; then
                    write_warn_text "Lizenz läuft in 1 Tag ab (gültig bis ${DE_DATE})"
                else
                    write_warn_text "Lizenz läuft in ${LIC_DAYS} Tagen ab (gültig bis ${DE_DATE})"
                fi
            else
                write_text "Lizenz gültig bis ${DE_DATE}"
            fi
        else
            DAYS="$(echo "${STATUS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["days_remaining"])')"
            if [ "${DAYS}" -eq 1 ]; then
                write_text "Testversion noch 1 Tag gültig"
            else
                write_text "Testversion noch ${DAYS} Tage gültig"
            fi
        fi
        ;;
    expired)
        KIND="$(echo "${STATUS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kind"])')"
        if [ "${KIND}" = "license" ]; then
            write_alarm_text "Lizenz abgelaufen - bitte verlängern"
        else
            write_alarm_text "Testversion abgelaufen - bitte aktivieren"
        fi
        ;;
    *)
        write_text "Lizenzstatus unbekannt"
        ;;
esac
