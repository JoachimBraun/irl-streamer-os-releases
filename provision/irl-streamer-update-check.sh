#!/usr/bin/env bash
# IRL Streamer OS - Update-Check gegen das oeffentliche Release-Repo
#
# Nutzerwunsch 2026-09-09: der Kunde soll sehen, wenn eine neuere Version
# von IRL Streamer OS verfuegbar ist, und per Dialog selbst entscheiden
# koennen, ob er jetzt aktualisieren moechte - kein automatisches,
# unangekuendigtes Update.
#
# ZWEI-REPO-MODELL:
# - Privates Repo (irl-streamer-os2): laufende Entwicklung, ungetestete
#   Aenderungen, interne Architektur-/Netzwerk-Details in Kommentaren.
# - Oeffentliches Repo (irl-streamer-os-releases, github.com/JoachimBraun/
#   irl-streamer-os-releases): enthaelt AUSSCHLIESSLICH vom Betreiber
#   manuell freigegebene, getestete Versionen. Kunden-Geraete ziehen NUR
#   von hier, nie vom privaten Repo (das kennen sie nicht einmal).
#
# VERSIONSSPRUNG-SICHERHEIT (Nutzerwunsch: "eine oder zwei Versionen
# ueberspringen soll egal sein"): ein Update wendet IMMER den kompletten
# Ziel-Zustand der neuen Version an (idempotente Provisionierung via
# provision.sh + docker compose up -d --build), NICHT eine Kette von
# Zwischen-Patches. 1.65 -> 1.69 ist dadurch strukturell genauso sicher
# wie 1.68 -> 1.69 - es gibt keine sequenzielle Migrationslogik, die man
# durch Ueberspringen kaputt machen koennte.
#
# Laeuft TAEGLICH per systemd-Timer (Nutzerentscheidung 09.09., analog zum
# bestehenden Lizenz-Check-Muster) - siehe irl-sysmaint-update-check.timer
# in provision.sh.

set -uo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
RELEASES_REPO_URL="https://raw.githubusercontent.com/JoachimBraun/irl-streamer-os-releases/main"
RELEASES_GIT_URL="https://github.com/JoachimBraun/irl-streamer-os-releases.git"
LOCAL_VERSION_FILE="${PROJECT_DIR}/VERSION"
STATE_DIR="${PROJECT_DIR}/state"
UPDATE_DISMISSED_FILE="${STATE_DIR}/update-dismissed-for"
TARGET_USER="streamer"
LOG_PREFIX="[irl-streamer-update-check]"

log() { echo "${LOG_PREFIX} $*"; }

zenity_as_user() {
    local uid
    uid="$(id -u "${TARGET_USER}" 2>/dev/null || true)"
    [ -z "${uid}" ] && return 1
    sudo -u "${TARGET_USER}" \
        XDG_RUNTIME_DIR="/run/user/${uid}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
        WAYLAND_DISPLAY="wayland-0" \
        zenity "$@"
}

# --- 1. Lokale Version ermitteln --------------------------------------------
if [ ! -f "${LOCAL_VERSION_FILE}" ]; then
    log "Keine lokale VERSION-Datei gefunden (${LOCAL_VERSION_FILE}) - ueberspringe Update-Check."
    exit 0
fi
LOCAL_VERSION="$(cat "${LOCAL_VERSION_FILE}" | tr -d '[:space:]')"
if [ -z "${LOCAL_VERSION}" ]; then
    log "Lokale VERSION-Datei ist leer - ueberspringe Update-Check."
    exit 0
fi

# --- 2. Remote-Version abrufen (rein additiv - kein Netzwerk = kein Fehler) -
REMOTE_VERSION="$(curl -fsS --max-time 15 "${RELEASES_REPO_URL}/VERSION" 2>/dev/null | tr -d '[:space:]')"
if [ -z "${REMOTE_VERSION}" ]; then
    log "Release-Repo nicht erreichbar (kein Internet?) - naechster Versuch beim naechsten taeglichen Lauf."
    exit 0
fi

# --- 3. Versionen vergleichen (semantische Sortierung per sort -V, nicht --
# per String-Vergleich - "1.9" waere sonst faelschlich "groesser" als "1.10") -
if [ "${LOCAL_VERSION}" = "${REMOTE_VERSION}" ]; then
    log "Bereits auf der aktuellsten Version (${LOCAL_VERSION})."
    exit 0
fi

NEWER="$(printf '%s\n%s\n' "${LOCAL_VERSION}" "${REMOTE_VERSION}" | sort -V | tail -1)"
if [ "${NEWER}" != "${REMOTE_VERSION}" ]; then
    log "Lokale Version (${LOCAL_VERSION}) ist bereits neuer/gleich als Remote (${REMOTE_VERSION}) - nichts zu tun."
    exit 0
fi

log "Neue Version verfuegbar: ${LOCAL_VERSION} -> ${REMOTE_VERSION}"

# --- 4. Nicht bei jedem taeglichen Lauf erneut nerven, wenn der Nutzer diese
# konkrete Version bereits einmal abgelehnt hat (analog zum bestehenden
# WARN_SHOWN_FILE-Muster in license-daily-check.sh) - EINMAL pro Version.
mkdir -p "${STATE_DIR}"
LAST_DISMISSED=""
[ -f "${UPDATE_DISMISSED_FILE}" ] && LAST_DISMISSED="$(cat "${UPDATE_DISMISSED_FILE}")"
if [ "${LAST_DISMISSED}" = "${REMOTE_VERSION}" ]; then
    log "Update auf ${REMOTE_VERSION} wurde bereits abgelehnt - kein erneuter Dialog, bis eine noch neuere Version erscheint."
    exit 0
fi

# --- 5. Nutzer fragen -------------------------------------------------------
if ! zenity_as_user --question --title="IRL Streamer OS - Update verfuegbar" \
    --text="Eine neue Version von IRL Streamer OS ist verfuegbar.\n\nAktuell installiert: ${LOCAL_VERSION}\nVerfuegbar: ${REMOTE_VERSION}\n\nJetzt aktualisieren? (dauert einige Minuten, laufende Streams bitte vorher beenden)" \
    --ok-label="Jetzt aktualisieren" --cancel-label="Spaeter" --width=460; then
    log "Nutzer hat das Update auf ${REMOTE_VERSION} abgelehnt."
    echo "${REMOTE_VERSION}" > "${UPDATE_DISMISSED_FILE}"
    exit 0
fi

# --- 6. Update durchfuehren --------------------------------------------------
# Sicherheitsnetz (Nutzerwunsch: kein Update waehrend eines laufenden
# Streams) - gleiches Skript, das der Connectivity-Report-Timer schon
# nutzt, um stream-aktive Laeufe zu ueberspringen.
STREAM_CHECK="${PROJECT_DIR}/provision/systemd/irl-stream-active-check.sh"
if [ -x "${STREAM_CHECK}" ] && ! "${STREAM_CHECK}"; then
    zenity_as_user --warning --title="IRL Streamer OS - Update" \
        --text="Es scheint gerade ein Stream aktiv zu sein - das Update wird jetzt NICHT durchgefuehrt.\n\nBitte den Stream beenden und das Update spaeter erneut ueber diesen Dialog starten (erscheint automatisch wieder beim naechsten taeglichen Check)." \
        --width=480
    log "Update abgebrochen - Stream ist aktiv."
    exit 0
fi

zenity_as_user --info --title="IRL Streamer OS - Update" \
    --text="Update auf Version ${REMOTE_VERSION} wird jetzt im Hintergrund durchgefuehrt.\n\nDu bekommst eine Meldung, sobald es fertig ist." \
    --width=440 &
disown

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

log "Klone Release-Repo (Tag v${REMOTE_VERSION})..."
if ! git clone --quiet --depth 1 --branch "v${REMOTE_VERSION}" "${RELEASES_GIT_URL}" "${WORK_DIR}/release" 2>/tmp/irl-update-clone.log; then
    log "FEHLER: Konnte Tag v${REMOTE_VERSION} nicht klonen. Log:"
    cat /tmp/irl-update-clone.log
    zenity_as_user --error --title="IRL Streamer OS - Update fehlgeschlagen" \
        --text="Das Update auf Version ${REMOTE_VERSION} konnte nicht heruntergeladen werden.\n\nBitte spaeter erneut versuchen oder den Support kontaktieren." \
        --width=460
    exit 1
fi

# --- 7. Neue Version an ihren Platz kopieren + idempotent neu provisionieren
# rsync statt "rm -rf + cp": bewahrt bestehende Kunden-Zustandsdateien
# (state/, .git NICHT vorhanden im Release-Snapshot, license-Tresor liegt
# ohnehin ausserhalb von PROJECT_DIR) - --delete waere hier gefaehrlich,
# deshalb bewusst OHNE --delete, reines Ueberschreiben/Ergaenzen.
log "Kopiere neue Version nach ${PROJECT_DIR}..."
rsync -a --exclude='state/' --exclude='.git/' "${WORK_DIR}/release/" "${PROJECT_DIR}/" 2>&1 | tee -a /tmp/irl-update-rsync.log

log "Fuehre Provisionierung erneut aus (idempotent, stellt vollstaendigen Ziel-Zustand her)..."
if ! bash "${PROJECT_DIR}/provision/provision.sh" >/tmp/irl-update-provision.log 2>&1; then
    log "FEHLER: provision.sh ist fehlgeschlagen. Log siehe /tmp/irl-update-provision.log"
    zenity_as_user --error --title="IRL Streamer OS - Update fehlgeschlagen" \
        --text="Das Update auf Version ${REMOTE_VERSION} ist wahrend der Einrichtung fehlgeschlagen.\n\nDas System bleibt auf dem bisherigen Stand nutzbar. Bitte den Support kontaktieren (Log: /tmp/irl-update-provision.log)." \
        --width=480
    exit 1
fi

log "Baue/starte Docker-Container neu..."
if [ -f "${PROJECT_DIR}/docker/docker-compose.yml" ]; then
    (cd "${PROJECT_DIR}/docker" && docker compose up -d --build >/tmp/irl-update-docker.log 2>&1) \
        || log "WARNUNG: docker compose up fehlgeschlagen, siehe /tmp/irl-update-docker.log"
fi

rm -f "${UPDATE_DISMISSED_FILE}"
log "Update auf ${REMOTE_VERSION} abgeschlossen."
zenity_as_user --info --title="IRL Streamer OS - Update abgeschlossen" \
    --text="IRL Streamer OS wurde erfolgreich auf Version ${REMOTE_VERSION} aktualisiert." \
    --width=420
