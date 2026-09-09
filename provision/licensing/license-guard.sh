#!/usr/bin/env bash
# IRL Streamer OS - Lizenz-Sperr-Wächter
#
# Wird VOR dem eigentlichen OBS-Start aus dem bestehenden Autostart-Wrapper
# (irl-streamer-obs-launch.sh) aufgerufen.
#
# HAERTUNG (Nutzerwunsch 09.09.): Die reine Existenz-Pruefung der
# Sperr-Markierung (LOCK_FILE) liess sich frueher trivial umgehen, indem
# man diese eine Datei einfach loeschte - das machte den taeglichen Timer
# fuer bis zu mehrere Stunden wirkungslos. Jetzt wird ZUSAETZLICH die
# bereits vorhandene Ed25519-Signaturpruefung (license-check.py) LIVE bei
# jedem OBS-Start erneut ausgewertet - komplett offline (kein Netzwerk,
# keine Bandbreite), nur ein lokaler Signaturcheck (Millisekunden, keine
# spuerbare CPU-Last). Ein Angreifer kann also nicht mehr durch simples
# Loeschen der Lock-Datei wieder starten, solange die signierte
# license.json selbst "expired" sagt - nur eine neue, gueltig signierte
# Lizenz (die nur der Server ausstellen kann) hebt die Sperre wirklich auf.
#
# Verbleibende Luecke (bewusst in Kauf genommen, siehe README Sicherheits-
# modell): eine SERVER-SEITIGE Sperrung/Loeschung (Admin sperrt eine an
# sich noch nicht abgelaufene Lizenz) wird nur ueber LOCK_FILE erkannt,
# da die lokale license.json in diesem Fall inhaltlich unveraendert und
# weiterhin gueltig signiert bleibt (der Server aktualisiert sie erst bei
# erfolgreichem Online-Refresh) - das erfordert weiterhin den taeglichen
# Online-Abgleich, kann ohne Netzwerkzugriff bei jedem Start nicht lokal
# geschlossen werden.
#
# Exit-Code 0 = nicht gesperrt, OBS darf starten
# Exit-Code 1 = gesperrt, zeigt Hinweisdialog UND verweigert den Start

set -euo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
RESOLVER="${PROJECT_DIR}/provision/licensing/license-locate.py"
LICENSE_CHECK="${PROJECT_DIR}/provision/licensing/license-check.py"
LOCK_FILE="$(python3 "${RESOLVER}" lock_file)"
LICENSE_FILE="$(python3 "${RESOLVER}" license_file 2>/dev/null || true)"
TARGET_USER="streamer"

is_locked() {
    # 1) Server-revoked/explizit gesperrt: einzig ueber LOCK_FILE bekannt
    #    (siehe Kommentar oben).
    if [ -f "${LOCK_FILE}" ]; then
        return 0
    fi
    # 2) Natuerlicher Ablauf: live und lokal per Signaturpruefung erneut
    #    verifiziert, UNABHAENGIG von der (loeschbaren) Lock-Datei.
    if [ -n "${LICENSE_FILE}" ] && [ -f "${LICENSE_FILE}" ]; then
        local state
        state="$(python3 "${LICENSE_CHECK}" "${LICENSE_FILE}" 2>/dev/null \
            | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("state","invalid"))
except Exception:
    print("invalid")' 2>/dev/null || echo "invalid")"
        if [ "${state}" = "expired" ]; then
            return 0
        fi
    fi
    return 1
}

if ! is_locked; then
    exit 0
fi

if command -v zenity >/dev/null 2>&1; then
    uid="$(id -u "${TARGET_USER}" 2>/dev/null || true)"
    if [ -n "${uid}" ]; then
        XDG_RUNTIME_DIR="/run/user/${uid}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
        WAYLAND_DISPLAY="wayland-0" \
        zenity --error --title="IRL Streamer OS - gesperrt" \
            --text="Deine Test-/Lizenzphase ist abgelaufen.\n\nOBS wurde nicht gestartet. Bitte einen Aktivierungscode ueber das Desktop-Icon 'Lizenz aktivieren' eingeben." \
            --width=420 2>/dev/null || true
    fi
fi

exit 1
