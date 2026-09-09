#!/usr/bin/env bash
# IRL Streamer OS - Lizenz aktivieren (interaktiver Dialog)
#
# Wird per Desktop-Icon "Lizenz aktivieren" gestartet (siehe
# IRL-Streamer-OS-Lizenz-aktivieren.desktop). Fragt den Aktivierungscode
# per Zenity ab, ruft license-client.sh activate auf, zeigt Erfolg/Fehler
# in einem weiteren Dialog.

set -euo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
LICENSE_CLIENT="${PROJECT_DIR}/provision/licensing/license-client.sh"

if ! command -v zenity >/dev/null 2>&1; then
    echo "FEHLER: zenity nicht verfuegbar." >&2
    exit 1
fi

CODE="$(zenity --entry \
    --title="IRL Streamer OS - Lizenz aktivieren" \
    --text="Bitte den Aktivierungscode eingeben, den du per E-Mail erhalten hast:\n\nFormat: XXXX-XXXX-XXXX-XXXX" \
    --width=420 2>/dev/null || true)"

if [ -z "${CODE}" ]; then
    exit 0
fi

if OUTPUT="$(sudo bash "${LICENSE_CLIENT}" activate "${CODE}" 2>&1)"; then
    EXPIRES="$(echo "${OUTPUT}" | grep -oP '(?<=Gueltig bis: ).*' || echo "")"
    zenity --info \
        --title="IRL Streamer OS" \
        --text="Lizenz erfolgreich aktiviert.${EXPIRES:+\n\nGueltig bis: ${EXPIRES}}" \
        --width=380 2>/dev/null || true
else
    ERROR_MSG="$(echo "${OUTPUT}" | grep -oP '(?<=FEHLER: ).*' | tail -1)"
    [ -z "${ERROR_MSG}" ] && ERROR_MSG="Unbekannter Fehler - bitte spaeter erneut versuchen."
    zenity --error \
        --title="IRL Streamer OS" \
        --text="Aktivierung fehlgeschlagen:\n\n${ERROR_MSG}" \
        --width=420 2>/dev/null || true
fi
