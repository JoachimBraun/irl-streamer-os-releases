#!/usr/bin/env bash
# IRL Streamer OS - Update-Check gegen das oeffentliche Release-Repo
#
# Nutzerwunsch 2026-09-09: der Kunde soll sehen, wenn eine neuere Version
# von IRL Streamer OS verfuegbar ist, und per Dialog selbst entscheiden
# koennen, ob er jetzt aktualisieren moechte - kein automatisches,
# unangekuendigtes Update.
#
# ZWEI-REPO-MODELL:
# - Privates Repo (irl-streamer-os2): laufende Entwicklung.
# - Oeffentliches Repo (github.com/JoachimBraun/irl-streamer-os-releases):
#   enthaelt AUSSCHLIESSLICH vom Betreiber freigegebene Versionen. Kunden-
#   Geraete ziehen NUR von hier.
#
# VERSIONSSPRUNG-SICHERHEIT: ein Update wendet IMMER den kompletten Ziel-
# Zustand der neuen Version an (idempotentes provision.sh + docker compose
# up -d --build), keine Kette von Zwischen-Patches.
#
# V1.85 - SIGNIERTE + ATOMARE UPDATES MIT ROLLBACK:
#   1. Remote-VERSION muss ^[0-9]+\.[0-9]+$ sein und STRIKT groesser.
#   2. Tag v<VER> in ein mktemp-Verzeichnis klonen.
#   3. provision/lib/verify-release.py (die BEREITS INSTALLIERTE Kopie,
#      nie die aus dem Klon) prueft RELEASE_MANIFEST.json(.sig) gegen den
#      root-eigenen Release-Key /etc/irl-streamer-os/release-pubkey.b64:
#      Signatur, Version, sha256 jeder Datei, keine Zusatzdateien.
#      Fehlschlag -> Abbruch, NICHTS wurde veraendert.
#   4. Backup von provision/ docker/ remote-access/ VERSION nach
#      /var/lib/irl-streamer-os/rollback/<alte-version>/ (nur 2 behalten).
#   5. rsync --delete der drei Payload-Ordner - lokal erzeugte Zustands-/
#      Geheimnisdateien sind explizit ausgenommen (PRESERVE_* unten).
#   6. provision.sh + docker compose up -d --build + Health-Check
#      (Container laufen, Dashboard /healthz, Caddy antwortet).
#   7. Fehler in 5/6 -> Backup zurueckspielen, provision.sh + compose der
#      alten Version erneut, Fehlerdialog. VERSION wird NUR bei Erfolg
#      geschrieben (naechster Timer-Lauf versucht es sonst erneut).
#
# Laeuft TAEGLICH per systemd-Timer irl-streamer-update-check.timer (siehe
# provision.sh, Abschnitt 8b0) und manuell per Desktop-Icon (--manual).
# Parallele Laeufe (Timer + Icon) verhindert flock auf LOCK_FILE.

set -euo pipefail

MANUAL_MODE=0
[ "${1:-}" = "--manual" ] && MANUAL_MODE=1

PROJECT_DIR="${IRL_PROJECT_DIR:-/opt/irl-streamer-os}"
RELEASES_REPO_URL="https://raw.githubusercontent.com/JoachimBraun/irl-streamer-os-releases/main"
RELEASES_GIT_URL="https://github.com/JoachimBraun/irl-streamer-os-releases.git"
LOCAL_VERSION_FILE="${PROJECT_DIR}/VERSION"
STATE_DIR="${PROJECT_DIR}/state"
UPDATE_DISMISSED_FILE="${STATE_DIR}/update-dismissed-for"
TARGET_USER="streamer"
LOG_PREFIX="[irl-streamer-update-check]"
LICENSE_SERVER_URL="${LICENSE_SERVER_URL:-https://lizenz.irlstreameros.de}"
FINGERPRINT_SCRIPT="${PROJECT_DIR}/provision/licensing/collect-fingerprint.sh"
VERIFY_HELPER="${PROJECT_DIR}/provision/lib/verify-release.py"
RELEASE_PUBKEY_FILE="${IRL_RELEASE_PUBKEY_FILE:-/etc/irl-streamer-os/release-pubkey.b64}"
ROLLBACK_ROOT="${IRL_ROLLBACK_ROOT:-/var/lib/irl-streamer-os/rollback}"
LOG_DIR="${IRL_LOG_DIR:-/var/log/irl-streamer-os}"
LOCK_FILE="${IRL_UPDATE_LOCK_FILE:-/run/irl-streamer-os-update.lock}"
# Nur fuer Tests (tests/test_update_check.py) ueberschreibbar - im Betrieb
# setzt niemand diese IRL_*-Variablen (systemd-Unit/sudoers ohne env).
RUNTIME_BASE="${IRL_RUNTIME_BASE:-/run/user}"
PAYLOAD_DIRS="provision docker remote-access"
HEALTH_TIMEOUT_S="${IRL_UPDATE_HEALTH_TIMEOUT_S:-120}"
VERSION_RE='^[0-9]+\.[0-9]+$'

# Lokal erzeugte Dateien INNERHALB der Payload-Ordner, die ein Update weder
# ueberschreiben noch loeschen darf (Pfade relativ zum jeweiligen Ordner,
# fuer rsync mit fuehrendem "/" verankert). Ermittelt per grep in
# provision.sh/license-vault-init.sh (alle Schreibziele unter docker/ und
# provision/). state/, icons/, .provisioned liegen ausserhalb der
# Payload-Ordner und werden ohnehin nie angefasst; Caddy-/Postgres-/
# Filebrowser-Daten sind Docker-Volumes.
# shellcheck disable=SC2034  # indirekt genutzt ueber local -n in preserve_args
PRESERVE_provision=(
    "/licensing/license-locate.py"   # pro Geraet generierter Tresor-Resolver
)
# shellcheck disable=SC2034
PRESERVE_docker=(
    "/irl-diagnostics.env"           # Dashboard-Hash + OBS-Passwort
    "/irl-diagnostics-data/"         # Dashboard-Daten (device_config.json ...)
    "/belabox/config.json"           # Belabox-/NOALBS-Config (vom Dashboard editiert)
    "/belabox/.env"
    "/filebrowser/config.yaml"       # gerendert mit Admin-Passwort
    "/guacamole/state/"              # PostgreSQL-Passwort
)
# shellcheck disable=SC2034
PRESERVE_remote_access=()

log() { echo "${LOG_PREFIX} $*"; }

# --- Logdatei (statt fester /tmp-Pfade) ------------------------------------
mkdir -p "${LOG_DIR}"
chmod 750 "${LOG_DIR}"
UPDATE_LOG="${LOG_DIR}/update.log"
touch "${UPDATE_LOG}"
chmod 640 "${UPDATE_LOG}"
exec > >(tee -a "${UPDATE_LOG}") 2>&1
log "===== $(date -Is) Start (manual=${MANUAL_MODE}) ====="

# --- Sperre gegen parallele Laeufe -----------------------------------------
exec 8>"${LOCK_FILE}"
if ! flock -n 8; then
    log "Ein anderer Update-Lauf ist bereits aktiv - beende."
    exit 0
fi

# --- Zenity im Kontext der grafischen Sitzung ------------------------------
# runuser statt sudo -u (root->streamer braucht so keine Authentifizierung,
# siehe Bugfix 13.09.2026). Rueckgabe = zenity-Exitcode; 90 = keine
# grafische Sitzung vorhanden (z.B. Timer-Lauf vor dem Login).
session_available() {
    local uid
    uid="$(id -u "${TARGET_USER}" 2>/dev/null || true)"
    [ -n "${uid}" ] && [ -S "${RUNTIME_BASE}/${uid}/bus" ] && [ -S "${RUNTIME_BASE}/${uid}/wayland-0" ]
}

zenity_as_user() {
    local uid rc=0
    uid="$(id -u "${TARGET_USER}" 2>/dev/null || true)"
    if [ -z "${uid}" ] || ! session_available; then
        return 90
    fi
    runuser -u "${TARGET_USER}" -- env \
        XDG_RUNTIME_DIR="/run/user/${uid}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
        WAYLAND_DISPLAY="wayland-0" \
        zenity "$@" 2>/dev/null || rc=$?
    return "${rc}"
}

# Nicht-blockierende Info/Fehlermeldung (Rueckgabewert egal).
notify_user() {
    local kind="$1" title="$2" text="$3"
    zenity_as_user "--${kind}" --title="${title}" --text="${text}" --width=480 --timeout=3600 || true
}

# --- 0. Installierte Version an den Lizenzserver melden --------------------
# Reine Bestandsaufnahme, fehlertolerant (darf den Check nie blockieren).
if [ -f "${LOCAL_VERSION_FILE}" ] && [ -x "${FINGERPRINT_SCRIPT}" ]; then
    REPORT_VERSION="$(tr -d '[:space:]' < "${LOCAL_VERSION_FILE}")"
    REPORT_FP="$(bash "${FINGERPRINT_SCRIPT}" 2>/dev/null || true)"
    if [[ "${REPORT_VERSION}" =~ ${VERSION_RE} ]] && [[ "${REPORT_FP}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        curl -fsS --max-time 15 -X POST "${LICENSE_SERVER_URL}/report-version" \
            -H "Content-Type: application/json" \
            -d "{\"device_fingerprint\":\"${REPORT_FP}\",\"installed_version\":\"${REPORT_VERSION}\"}" \
            >/dev/null 2>&1 \
            || log "Versionsmeldung an Lizenzserver fehlgeschlagen (kein Internet?) - nicht kritisch."
    fi
fi

# --- 1. Lokale Version -------------------------------------------------------
if [ ! -f "${LOCAL_VERSION_FILE}" ]; then
    log "Keine lokale VERSION-Datei gefunden (${LOCAL_VERSION_FILE}) - ueberspringe Update-Check."
    exit 0
fi
LOCAL_VERSION="$(tr -d '[:space:]' < "${LOCAL_VERSION_FILE}")"
if ! [[ "${LOCAL_VERSION}" =~ ${VERSION_RE} ]]; then
    log "Lokale VERSION '${LOCAL_VERSION}' hat ein unerwartetes Format - ueberspringe Update-Check."
    exit 0
fi

# --- 2. Remote-Version (kein Netzwerk = kein Fehler) -------------------------
REMOTE_VERSION="$(curl -fsS --max-time 15 "${RELEASES_REPO_URL}/VERSION" 2>/dev/null | head -c 64 | tr -d '[:space:]' || true)"
if [ -z "${REMOTE_VERSION}" ]; then
    log "Release-Repo nicht erreichbar (kein Internet?) - naechster Versuch beim naechsten Lauf."
    if [ "${MANUAL_MODE}" = "1" ]; then
        notify_user error "IRL Streamer OS - Update-Check" \
            "Der Update-Server konnte nicht erreicht werden.\n\nBitte die Internetverbindung pruefen und es spaeter erneut versuchen."
    fi
    exit 0
fi
if ! [[ "${REMOTE_VERSION}" =~ ${VERSION_RE} ]]; then
    log "Remote-VERSION hat ein ungueltiges Format - ignoriere (moeglicherweise manipuliert)."
    [ "${MANUAL_MODE}" = "1" ] && notify_user error "IRL Streamer OS - Update-Check" \
        "Der Update-Server hat eine ungueltige Versionsangabe geliefert. Es wurde nichts veraendert."
    exit 0
fi

# --- 3. Nur STRIKT hoehere Versionen (sort -V, "1.10" > "1.9") ---------------
NEWER="$(printf '%s\n%s\n' "${LOCAL_VERSION}" "${REMOTE_VERSION}" | sort -V | tail -1)"
if [ "${LOCAL_VERSION}" = "${REMOTE_VERSION}" ] || [ "${NEWER}" != "${REMOTE_VERSION}" ]; then
    log "Bereits aktuell (lokal ${LOCAL_VERSION}, remote ${REMOTE_VERSION})."
    [ "${MANUAL_MODE}" = "1" ] && notify_user info "IRL Streamer OS - Update-Check" \
        "Du hast bereits die aktuellste Version installiert (${LOCAL_VERSION})."
    exit 0
fi
log "Neue Version verfuegbar: ${LOCAL_VERSION} -> ${REMOTE_VERSION}"

# --- 4. Einmal pro Version im automatischen Modus fragen --------------------
mkdir -p "${STATE_DIR}"
LAST_DISMISSED=""
[ -f "${UPDATE_DISMISSED_FILE}" ] && LAST_DISMISSED="$(cat "${UPDATE_DISMISSED_FILE}" 2>/dev/null || true)"
if [ "${MANUAL_MODE}" != "1" ] && [ "${LAST_DISMISSED}" = "${REMOTE_VERSION}" ]; then
    log "Update auf ${REMOTE_VERSION} wurde bereits abgelehnt - kein erneuter Dialog."
    exit 0
fi

# Stream-Check: 0 = frei, 1 = aktiv, 2 = unbekannt.
stream_state() {
    local out rc=0
    [ -f "${PROJECT_DIR}/provision/lib/stream-active.py" ] || return 2
    out="$(timeout 30 python3 "${PROJECT_DIR}/provision/lib/stream-active.py" 2>&1)" || rc=$?
    log "Stream-Check: ${out}"
    case "${rc}" in 0) return 0 ;; 1) return 1 ;; *) return 2 ;; esac
}

# Automatischer Lauf: bei aktivem ODER unklarem Stream gar nicht erst stoeren.
if [ "${MANUAL_MODE}" != "1" ]; then
    SS=0; stream_state || SS=$?
    if [ "${SS}" != "0" ]; then
        log "Stream aktiv oder Status unklar - automatischer Update-Dialog wird verschoben."
        exit 0
    fi
fi

# --- 5. Nutzer fragen ---------------------------------------------------------
# zenity --question: 0 = Ja, 1 = Nein, 5 = Timeout, sonst Fehler. NUR ein
# echtes "Nein" merkt sich die Ablehnung - fehlende Sitzung/Timeout nicht.
Q_RC=0
zenity_as_user --question --title="IRL Streamer OS - Update verfuegbar" \
    --text="Eine neue Version von IRL Streamer OS ist verfuegbar.\n\nAktuell installiert: ${LOCAL_VERSION}\nVerfuegbar: ${REMOTE_VERSION}\n\nJetzt aktualisieren? (dauert einige Minuten, laufende Streams bitte vorher beenden)" \
    --ok-label="Jetzt aktualisieren" --cancel-label="Spaeter" --width=460 --timeout=3600 || Q_RC=$?
case "${Q_RC}" in
    0) ;;
    1)
        log "Nutzer hat das Update auf ${REMOTE_VERSION} abgelehnt."
        echo "${REMOTE_VERSION}" > "${UPDATE_DISMISSED_FILE}"
        exit 0
        ;;
    5)  log "Update-Dialog ohne Antwort abgelaufen - naechster Versuch beim naechsten Lauf."; exit 0 ;;
    90) log "Keine grafische Sitzung - Update-Dialog kann nicht angezeigt werden, naechster Versuch spaeter."; exit 0 ;;
    *)  log "Update-Dialog konnte nicht angezeigt werden (zenity rc=${Q_RC}) - naechster Versuch spaeter."; exit 0 ;;
esac

# --- 6. Kein Update waehrend eines laufenden Streams --------------------------
SS=0; stream_state || SS=$?
if [ "${SS}" = "1" ]; then
    notify_user warning "IRL Streamer OS - Update" \
        "Es laeuft gerade ein Stream oder eine Aufnahme - das Update wird jetzt NICHT durchgefuehrt.\n\nBitte den Stream beenden und das Update danach erneut starten (Desktop-Icon 'Auf Updates pruefen')."
    log "Update abgebrochen - Stream ist aktiv."
    exit 0
elif [ "${SS}" = "2" ]; then
    C_RC=0
    zenity_as_user --question --title="IRL Streamer OS - Update" \
        --text="Es konnte nicht sicher festgestellt werden, ob gerade ein Stream laeuft.\n\nNur fortfahren, wenn KEIN Stream und KEINE Aufnahme laeuft.\n\nTrotzdem jetzt aktualisieren?" \
        --ok-label="Ja, kein Stream aktiv" --cancel-label="Abbrechen" --width=460 --timeout=600 || C_RC=$?
    if [ "${C_RC}" != "0" ]; then
        log "Stream-Status unklar und nicht bestaetigt - Update abgebrochen."
        exit 0
    fi
fi

# --- 7. Herunterladen + Signatur pruefen (noch KEINE Aenderung) -------------
WORK_DIR="$(mktemp -d /var/tmp/irl-update.XXXXXX)"
cleanup() { rm -rf "${WORK_DIR}"; }
trap cleanup EXIT

fail_before_change() {
    log "FEHLER: $1"
    notify_user error "IRL Streamer OS - Update fehlgeschlagen" \
        "Das Update auf Version ${REMOTE_VERSION} wurde abgebrochen:\n\n$1\n\nEs wurde NICHTS veraendert - das System laeuft unveraendert weiter.\n(Log: ${UPDATE_LOG})"
    exit 1
}

log "Klone Release-Repo (Tag v${REMOTE_VERSION})..."
if ! git clone --quiet --depth 1 --branch "v${REMOTE_VERSION}" "${RELEASES_GIT_URL}" "${WORK_DIR}/release"; then
    fail_before_change "Download fehlgeschlagen (Tag v${REMOTE_VERSION} nicht abrufbar)."
fi
rm -rf "${WORK_DIR}/release/.git"

if [ ! -f "${VERIFY_HELPER}" ]; then
    fail_before_change "Pruefprogramm ${VERIFY_HELPER} fehlt."
fi
log "Pruefe Signatur und Pruefsummen des Releases..."
if ! python3 "${VERIFY_HELPER}" --release-dir "${WORK_DIR}/release" \
        --expected-version "${REMOTE_VERSION}" --pubkey-file "${RELEASE_PUBKEY_FILE}"; then
    fail_before_change "Die Signaturpruefung des Updates ist fehlgeschlagen (Release unvollstaendig oder manipuliert)."
fi

# --- 8. Backup der aktuellen Version ---------------------------------------
BACKUP_DIR="${ROLLBACK_ROOT}/${LOCAL_VERSION}"
log "Sichere aktuelle Version nach ${BACKUP_DIR}..."
mkdir -p "${ROLLBACK_ROOT}"
# Nur der Rollback-Ordner wird 700 - /var/lib/irl-streamer-os selbst enthaelt
# auch license-clock.json/installed-at (von license-check.py gelesen).
chmod 700 "${ROLLBACK_ROOT}"
rm -rf "${BACKUP_DIR}.tmp"
mkdir -p "${BACKUP_DIR}.tmp"
for d in ${PAYLOAD_DIRS}; do
    if [ -d "${PROJECT_DIR}/${d}" ]; then
        rsync -a --exclude='__pycache__/' --exclude='/irl-diagnostics-data/' \
            "${PROJECT_DIR}/${d}/" "${BACKUP_DIR}.tmp/${d}/" \
            || fail_before_change "Backup der aktuellen Version fehlgeschlagen (Speicherplatz?)."
    fi
done
cp -a "${LOCAL_VERSION_FILE}" "${BACKUP_DIR}.tmp/VERSION"
rm -rf "${BACKUP_DIR}"
mv "${BACKUP_DIR}.tmp" "${BACKUP_DIR}"

# rsync-Argumente fuer einen Payload-Ordner (inkl. Schutzliste).
preserve_args() {
    local d="$1" p
    local -n _list="PRESERVE_${d//-/_}"
    PRESERVE_ARGS=(--exclude='__pycache__/')
    for p in "${_list[@]}"; do
        PRESERVE_ARGS+=(--exclude="${p}")
    done
}

sync_tree() {
    # sync_tree <quelle-root> -> PROJECT_DIR, mit --delete + Schutzliste
    local src="$1" d
    for d in ${PAYLOAD_DIRS}; do
        [ -d "${src}/${d}" ] || continue
        preserve_args "${d}"
        # --checksum: rsyncs Schnellvergleich (Groesse+mtime) uebersprang gleich
        # grosse Dateien mit identischer Sekunde (in CI reproduziert) - bei
        # Update UND Rollback darf keine alte Datei stehen bleiben.
        rsync -a --checksum --delete "${PRESERVE_ARGS[@]}" "${src}/${d}/" "${PROJECT_DIR}/${d}/" || return 1
    done
}

run_provision() {
    IRL_PROVISION_SKIP_REBOOT_PROMPT=1 IRL_UPDATE_LOCK_HELD=1 \
        bash "${PROJECT_DIR}/provision/provision.sh" </dev/null
}

compose_up() {
    [ -f "${PROJECT_DIR}/docker/docker-compose.yml" ] || return 1
    (cd "${PROJECT_DIR}/docker" && docker compose up -d --build --remove-orphans)
}

license_locked() {
    local lf
    lf="$(python3 "${PROJECT_DIR}/provision/licensing/license-locate.py" lock_file 2>/dev/null || true)"
    [ -n "${lf}" ] && [ -f "${lf}" ]
}

# Health-Check nach dem Update. Bei aktiver Lizenzsperre sind die
# lizenzpflichtigen Container absichtlich gestoppt - dann nur die uebrigen
# pruefen (sonst wuerde jedes Update eines gesperrten Geraets zurueckgerollt).
health_check() {
    local deadline expected c running missing code caddy_ok dash_ok
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
    if license_locked; then
        expected="guacamole-db caddy"
    else
        expected="belabox-receiver irl-diagnostics guacd guacamole-db guacamole filebrowser caddy"
    fi
    if grep -q '^  docker-socket-proxy:' "${PROJECT_DIR}/docker/docker-compose.yml" 2>/dev/null; then
        expected="${expected} docker-socket-proxy"
    fi
    while :; do
        running="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
        missing=""
        for c in ${expected}; do
            grep -qx "${c}" <<<"${running}" || missing="${missing} ${c}"
        done
        caddy_ok=0
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:5003/root-ca.crt 2>/dev/null || true)"
        [ -n "${code}" ] && [ "${code}" != "000" ] && caddy_ok=1
        dash_ok=1
        if ! license_locked; then
            dash_ok=0
            code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8300/healthz 2>/dev/null || true)"
            [ "${code}" = "200" ] && dash_ok=1
        fi
        if [ -z "${missing}" ] && [ "${caddy_ok}" = "1" ] && [ "${dash_ok}" = "1" ]; then
            log "Health-Check OK (${expected})."
            return 0
        fi
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            log "Health-Check FEHLGESCHLAGEN: fehlende Container:${missing:- keine}, caddy_ok=${caddy_ok}, dashboard_ok=${dash_ok}"
            return 1
        fi
        sleep 5
    done
}

rollback() {
    local reason="$1"
    log "ROLLBACK auf ${LOCAL_VERSION}: ${reason}"
    if sync_tree "${BACKUP_DIR}"; then
        run_provision || log "WARNUNG: provision.sh der alten Version meldete einen Fehler."
        compose_up || log "WARNUNG: docker compose up der alten Version fehlgeschlagen."
    else
        log "KRITISCH: Zuruecksichern aus ${BACKUP_DIR} fehlgeschlagen!"
    fi
    notify_user error "IRL Streamer OS - Update fehlgeschlagen" \
        "Das Update auf Version ${REMOTE_VERSION} ist fehlgeschlagen:\n${reason}\n\nDie bisherige Version ${LOCAL_VERSION} wurde automatisch wiederhergestellt.\nBitte den Support kontaktieren (Log: ${UPDATE_LOG})."
    exit 1
}

# --- 9. Installieren -------------------------------------------------------
notify_user info "IRL Streamer OS - Update" \
    "Update auf Version ${REMOTE_VERSION} wird jetzt durchgefuehrt (einige Minuten).\n\nDu bekommst eine Meldung, sobald es fertig ist." &
disown || true

log "Kopiere neue Version nach ${PROJECT_DIR} (rsync --delete, Zustandsdateien geschuetzt)..."
sync_tree "${WORK_DIR}/release" || rollback "Kopieren der neuen Dateien fehlgeschlagen."

log "Fuehre Provisionierung aus..."
run_provision || rollback "Einrichtung (provision.sh) fehlgeschlagen."

log "Baue/starte Docker-Container..."
compose_up || rollback "Docker-Container konnten nicht gestartet werden."

log "Pruefe Dienste..."
health_check || rollback "Dienste nach dem Update nicht gesund (Health-Check)."

# --- 10. Erfolg: VERSION erst jetzt schreiben (atomar) -----------------------
rm -f "${UPDATE_DISMISSED_FILE}"
printf '%s\n' "${REMOTE_VERSION}" > "${LOCAL_VERSION_FILE}.tmp"
mv -f "${LOCAL_VERSION_FILE}.tmp" "${LOCAL_VERSION_FILE}"
log "Update auf ${REMOTE_VERSION} abgeschlossen."

# Nur die 2 neuesten Rollback-Staende behalten.
if [ -d "${ROLLBACK_ROOT}" ]; then
    find "${ROLLBACK_ROOT}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | head -n -2 \
        | while read -r old; do
            [[ "${old}" =~ ${VERSION_RE} ]] && rm -rf "${ROLLBACK_ROOT:?}/${old}"
        done
fi

# Neustart-Abfrage erst NACH dem VERSION-Schreiben (siehe Bugfix 18.09.2026).
R_RC=0
zenity_as_user --question --title="IRL Streamer OS - Update abgeschlossen" \
    --text="IRL Streamer OS wurde erfolgreich auf Version ${REMOTE_VERSION} aktualisiert.\n\nEin Neustart wird empfohlen, damit alle Aenderungen vollstaendig greifen.\n\nJetzt neu starten?" \
    --ok-label="Jetzt neu starten" --cancel-label="Spaeter" --width=460 --timeout=3600 || R_RC=$?
if [ "${R_RC}" = "0" ]; then
    log "Neustart nach Update auf Nutzerwunsch..."
    systemctl reboot
else
    log "Neustart nach Update verschoben."
fi
