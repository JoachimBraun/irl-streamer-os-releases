#!/usr/bin/env bash
# IRL Streamer OS - Relay-Tunnel-Client (Umbau auf reines Relay-only-Modell,
# siehe docs zur Zwei-Zustands-Ampel rot/gruen).
#
# JEDER Kunde (nicht mehr nur CGNAT-Faelle) bekommt automatisch einen
# WireGuard-Relay-Tunnel + eine generierte Subdomain <slug>.irlstreameros.de
# - das ist der einzige Zugriffsweg von aussen. Dieses Skript baut den
# Tunnel beim ersten Lauf auf (POST /relay/token beim Lizenzserver ->
# POST /provision beim relay-provisioner -> Config einspielen -> POST
# /verify) und haelt/prueft ihn bei jedem weiteren (stuendlichen) Lauf
# erneut.
#
# Es gibt nur noch zwei moegliche Endzustaende, die relay-provision.json
# (state-Datei) beschreibt:
#   verified=true  -> Ampel GRUEN (Tunnel steht und wurde per
#                      Handshake+Echo-Test verifiziert)
#   verified=false/fehlt -> Ampel ROT (Tunnel noch nicht aufgebaut oder
#                      Verifikation zuletzt fehlgeschlagen - der naechste
#                      stuendliche Timer-Lauf versucht es automatisch
#                      erneut, siehe irl-connectivity-report.timer)
#
# KEIN GET /connectivity/preference mehr, KEIN Unterschied zwischen
# "rot"/"gelb" mehr - jedes Geraet provisioniert immer, unabhaengig vom
# technischen Erreichbarkeitsstatus des Heimnetzanschlusses (der wird gar
# nicht mehr lokal geprueft, siehe entfernte connectivity-checker.sh).
set -uo pipefail

LICENSE_SERVER_URL="${LICENSE_SERVER_URL:-https://lizenz.irlstreameros.de}"
RELAY_PROVISIONER_URL="${RELAY_PROVISIONER_URL:-https://relay.irlstreameros.de}"
PROJECT_DIR="/opt/irl-streamer-os"
STATE_DIR="${PROJECT_DIR}/state"
RESOLVER="${PROJECT_DIR}/provision/licensing/license-locate.py"
LICENSE_FILE="$(python3 "${RESOLVER}" license_file)"
FINGERPRINT_SCRIPT="${PROJECT_DIR}/provision/licensing/collect-fingerprint.sh"
RELAY_CONFIG_FILE="/etc/wireguard/wg-relay.conf"
RELAY_STATE_FILE="${STATE_DIR}/relay-provision.json"
LOG_PREFIX="[irl-connectivity-report]"

log() { echo "${LOG_PREFIX} $*"; }

mkdir -p "${STATE_DIR}"

# ---------------------------------------------------------------------------
# 0. Voraussetzungen
# ---------------------------------------------------------------------------
if [ ! -f "${LICENSE_FILE}" ]; then
    log "FEHLER: ${LICENSE_FILE} nicht gefunden - noch keine Lizenz/Testphase aktiviert. Ueberspringe Relay-Provisionierung."
    exit 0
fi

# ---------------------------------------------------------------------------
# V1.87 (Block B): Lastreduktion. Jede Minute wird nur LOKAL geprueft (Tunnel
# aktiv? Relay-Gateway 10.8.0.1 per Ping erreichbar?). Server-Requests gibt es
# nur noch fuer: stuendlichen Heartbeat (/verify, +Geraete-Jitter), Erholung
# nach Ausfall, Neu-Provisionierung - Fehler laufen mit Backoff+Jitter.
# Ausfall -> Ampel ROT innerhalb von <= 2 Minuten (Timer 1 min + Ping ~3 s).
# Vorher: /relay/token + /verify JEDE Minute (1440+ Requests/Tag/Geraet).
# Entscheidungslogik + Tests: provision/lib/relay-health.py, tests/test_relay_health.py
# ---------------------------------------------------------------------------
HEALTH_PY="${PROJECT_DIR}/provision/lib/relay-health.py"
TUNNEL_ACTIVE=0
if systemctl is-active --quiet wg-quick@wg-relay 2>/dev/null; then TUNNEL_ACTIVE=1; fi
ACTION="full"
if [ -f "${HEALTH_PY}" ]; then
    ACTION="$(python3 "${HEALTH_PY}" decide --state-dir "${STATE_DIR}" \
        --config-file "${RELAY_CONFIG_FILE}" --tunnel-active "${TUNNEL_ACTIVE}" 2>/dev/null)" || ACTION="full"
fi
case "${ACTION}" in
    ok)   log "Tunnel lokal gesund (Gateway erreichbar), kein Server-Kontakt noetig."; exit 0 ;;
    wait) log "Backoff aktiv - Server-Kontakt spaeter."; exit 0 ;;
    red)  log "Relay gateway nicht erreichbar - Ampel ROT. Naechster Lauf prueft erneut."; exit 0 ;;
    verify|full) ;;
    *)    ACTION="full" ;;
esac
if [ "${ACTION}" = "full" ]; then
    # Tunnel neu aufbauen (wg-quick restart) unterbricht einen laufenden Stream
    # -> nur wenn KEIN Stream aktiv ist (Erkennung unklar = wie aktiv behandeln).
    if ! bash "${PROJECT_DIR}/provision/systemd/irl-stream-active-check.sh" >/dev/null 2>&1; then
        log "Stream aktiv (oder unklar) - Tunnel-Neuaufbau wird verschoben."
        exit 0
    fi
fi
RESULT_RECORDED=0
record_result() { # $1 = verified|unverified|error
    RESULT_RECORDED=1
    [ -f "${HEALTH_PY}" ] && python3 "${HEALTH_PY}" record --state-dir "${STATE_DIR}" --result "$1" \
        ${2:+--slug "$2"} ${3:+--fingerprint "$3"} >/dev/null 2>&1 || true
}
# Jeder Abbruch ohne Ergebnis (Token/Provision/Verify-Fehler) zaehlt als Fehler -> Backoff.
trap '[ "${RESULT_RECORDED}" = "1" ] || record_result error' EXIT

if [ "${ACTION}" = "verify" ]; then
    # Schnellpfad: Fingerprint aus relay-provision.json, KEIN /relay/token.
    RELAY_FINGERPRINT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("device_fingerprint",""))' "${RELAY_STATE_FILE}" 2>/dev/null || true)"
    FAST_SLUG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("subdomain_slug",""))' "${RELAY_STATE_FILE}" 2>/dev/null || true)"
    if [ -n "${RELAY_FINGERPRINT}" ]; then
        if VERIFY_RESPONSE="$(curl -fsS --max-time 20 -X POST "${RELAY_PROVISIONER_URL}/verify" \
                -H "Content-Type: application/json" -d "{\"device_fingerprint\":\"${RELAY_FINGERPRINT}\"}" 2>&1)"; then
            if [ "$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("verified", False))' <<<"${VERIFY_RESPONSE}")" = "True" ]; then
                record_result verified "${FAST_SLUG}" "${RELAY_FINGERPRINT}"
                log "Relay-Tunnel verifiziert (Heartbeat/Erholung) - Ampel GRUEN."
            else
                record_result unverified "${FAST_SLUG}" "${RELAY_FINGERPRINT}"
                log "Relay meldet nicht verifiziert (${VERIFY_RESPONSE}) - Ampel ROT, Backoff."
            fi
            exit 0
        fi
        log "WARNUNG: /verify fehlgeschlagen (${VERIFY_RESPONSE}) - Backoff."
        record_result error
        exit 0
    fi
    # kein Fingerprint im State -> voller Ablauf
fi

FINGERPRINT="$(bash "${FINGERPRINT_SCRIPT}")"
SUBDOMAIN_SLUG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("subdomain_slug",""))' "${LICENSE_FILE}" 2>/dev/null || echo "")"

# Bestandsgeraete, deren Lizenz VOR der subdomain_slug-Einfuehrung (02.09.)
# aktiviert wurde, haben lokal noch kein subdomain_slug-Feld in
# license.json - per /check nachholen, der Lizenzserver vergibt es dort
# bei Bedarf automatisch nach (siehe main.py check_status()) und liefert
# es zurueck, OHNE die lokale Token-Datei selbst neu ausstellen zu muessen.
if [ -z "${SUBDOMAIN_SLUG}" ]; then
    log "subdomain_slug fehlt lokal - hole ihn per /check nach..."
    if CHECK_RESPONSE="$(curl -fsS --max-time 15 -X POST "${LICENSE_SERVER_URL}/check" \
            -H "Content-Type: application/json" -d "{\"device_fingerprint\":\"${FINGERPRINT}\"}" 2>&1)"; then
        SUBDOMAIN_SLUG="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("subdomain_slug",""))' "${CHECK_RESPONSE}" 2>/dev/null || echo "")"
        if [ -n "${SUBDOMAIN_SLUG}" ]; then
            log "subdomain_slug nachgeholt: ${SUBDOMAIN_SLUG} - ergaenze lokale license.json"
            # V1.85: unter derselben Sperre wie license-client.sh und atomar
            # (tmp + os.replace) - kein halb geschriebenes license.json mehr.
            (
                flock -w 30 9 || true
                python3 -c '
import json, os, sys, tempfile
path, slug = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
data["subdomain_slug"] = slug
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".lic-")
with os.fdopen(fd, "w") as f:
    json.dump(data, f)
os.chmod(tmp, os.stat(path).st_mode & 0o777)
os.replace(tmp, path)
' "${LICENSE_FILE}" "${SUBDOMAIN_SLUG}"
            ) 9>/run/irl-streamer-os-license.lock
        fi
    else
        log "WARNUNG: /check fehlgeschlagen, subdomain_slug bleibt unbekannt: ${CHECK_RESPONSE}"
    fi
fi

if [ -z "${SUBDOMAIN_SLUG}" ]; then
    log "FEHLER: subdomain_slug fehlt in ${LICENSE_FILE} - kann Relay nicht provisionieren. Naechster Lauf versucht /check erneut."
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Relay-Token holen (Ed25519-signiert, NUR bei gueltiger Lizenz/Trial -
#    siehe main.py /relay/token). Kein Token -> keine Provisionierung,
#    unabhaengig vom Zustand des Heimnetzanschlusses (Nutzerwunsch 02.09.:
#    "nur Kunden mit aktivierter Lizenz oder Testlizenz duerfen diesen
#    Tunnel bauen").
# ---------------------------------------------------------------------------
if ! TOKEN_RESPONSE="$(curl -fsS --max-time 15 -X POST "${LICENSE_SERVER_URL}/relay/token" \
        -H "Content-Type: application/json" -d "{\"device_fingerprint\":\"${FINGERPRINT}\"}" 2>&1)"; then
    log "FEHLER: Relay-Token konnte nicht abgerufen werden (evtl. Lizenz/Testphase abgelaufen): ${TOKEN_RESPONSE}"
    exit 1
fi

# WICHTIG (Bug gefunden 05.09.): Der Lizenzserver HASHT den rohen
# device_fingerprint nochmal (siehe hash_fingerprint_input() in main.py
# dort), BEVOR er ihn in der Lizenz-Datenbank nachschlaegt UND als
# "device_fingerprint" im Token zurueckgibt. Das bedeutet: der
# relay-provisioner (siehe /provision, /verify, /toggle-port dort) kennt
# und speichert IMMER diesen gehashten Wert, NIE den rohen FINGERPRINT von
# collect-fingerprint.sh direkt. Deshalb ab hier konsequent den aus
# TOKEN_RESPONSE extrahierten (gehashten) Wert verwenden.
RELAY_FINGERPRINT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["device_fingerprint"])' "${TOKEN_RESPONSE}")"

# ---------------------------------------------------------------------------
# 2. Falls bereits ein Tunnel besteht: nur verifizieren (kein Neu-Aufbau
#    noetig, /provision auf der Serverseite ist ohnehin idempotent, aber
#    ein bereits verbundener Tunnel muss nicht neu konfiguriert werden).
# ---------------------------------------------------------------------------
if [ -f "${RELAY_CONFIG_FILE}" ] && systemctl is-active --quiet wg-quick@wg-relay 2>/dev/null; then
    log "wg-relay-Tunnel existiert bereits und ist aktiv - ueberspringe Neu-Provisionierung, pruefe nur Verifizierung."
else
    log "Kein aktiver wg-relay-Tunnel - provisioniere neu..."
    PROVISION_PAYLOAD="$(python3 -c '
import json, sys
tok = json.loads(sys.argv[1])
print(json.dumps({
    "device_fingerprint": tok["device_fingerprint"],
    "subdomain_slug": tok["subdomain_slug"],
    "payload": tok["payload"],
    "signature": tok["signature"],
}))
' "${TOKEN_RESPONSE}")"

    if ! PROVISION_RESPONSE="$(curl -fsS --max-time 30 -X POST "${RELAY_PROVISIONER_URL}/provision" \
            -H "Content-Type: application/json" -d "${PROVISION_PAYLOAD}" 2>&1)"; then
        log "FEHLER: Relay-Provisionierung fehlgeschlagen: ${PROVISION_RESPONSE}"
        exit 1
    fi

    mkdir -p "${STATE_DIR}"
    echo "${PROVISION_RESPONSE}" > "${RELAY_STATE_FILE}"
    chmod 600 "${RELAY_STATE_FILE}"

    # WireGuard-Config einspielen. Interface-Name IMMER "wg-relay" (siehe
    # relay-provisioner main.py Hinweiskommentar in der gelieferten Config,
    # NICHT wg0 - Kollisionsgefahr mit bestehenden Tunneln wie dem
    # Belabox-Fernzugriffstunnel, live reproduziert 02.09.).
    RAW_CONFIG="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["wireguard_config"])' <<<"${PROVISION_RESPONSE}")"
    # V1.85: strikte Whitelist-Pruefung (provision/lib/validate-wg-config.py) -
    # wg-quick fuehrt PostUp/PreUp & Co. als root aus, AllowedIPs/DNS koennten
    # Traffic umleiten. Nur gepruefte, normalisierte Zeilen werden geschrieben
    # (tmp + mv); bei Ablehnung bleibt eine bestehende Config unveraendert.
    RELAY_HOST="$(printf '%s' "${RELAY_PROVISIONER_URL}" | sed -E 's#^[a-z]+://##; s#[:/].*$##')"
    WG_TMP="$(mktemp /etc/wireguard/.wg-relay.XXXXXX)"
    chmod 600 "${WG_TMP}"
    if ! printf '%s\n' "${RAW_CONFIG}" \
            | python3 "${PROJECT_DIR}/provision/lib/validate-wg-config.py" --endpoint-host "${RELAY_HOST}" > "${WG_TMP}"; then
        rm -f "${WG_TMP}"
        log "FEHLER: WireGuard-Config vom Relay-Server abgelehnt (siehe Grund oben) - Tunnel wird NICHT eingerichtet."
        exit 1
    fi
    mv -f "${WG_TMP}" "${RELAY_CONFIG_FILE}"
    chmod 600 "${RELAY_CONFIG_FILE}"

    sudo systemctl enable "wg-quick@wg-relay" 2>/dev/null || true
    sudo systemctl restart "wg-quick@wg-relay"
    log "wg-relay-Tunnel eingerichtet, warte kurz auf Handshake..."
    sleep 5
fi

# ---------------------------------------------------------------------------
# 3. Verifizieren (Handshake + Echo-Test durch den Tunnel). Das Ergebnis
#    (verified: true/false) wird 1:1 in relay-provision.json persistiert -
#    das Diagnose-Dashboard und alle anderen Anzeigen lesen NUR diese
#    lokale Datei, kein weiterer Server-Roundtrip noetig (siehe
#    _check_connectivity() in docker/irl-diagnostics-src/main.py).
# ---------------------------------------------------------------------------
if ! VERIFY_RESPONSE="$(curl -fsS --max-time 20 -X POST "${RELAY_PROVISIONER_URL}/verify" \
        -H "Content-Type: application/json" -d "{\"device_fingerprint\":\"${RELAY_FINGERPRINT}\"}" 2>&1)"; then
    log "WARNUNG: Verifizierung fehlgeschlagen (Tunnel evtl. noch nicht vollstaendig aufgebaut, naechster Lauf versucht es mit Backoff erneut): ${VERIFY_RESPONSE}"
    record_result error
    exit 0
fi

VERIFIED="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("verified", False))' <<<"${VERIFY_RESPONSE}")"

python3 -c '
import json, sys
path, slug, verified, fingerprint = sys.argv[1], sys.argv[2], sys.argv[3] == "True", sys.argv[4]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {}
data["subdomain_slug"] = slug
data["verified"] = verified
# device_fingerprint mitspeichern (Nutzerwunsch 05.09.: Ein/Aus-Schalter
# pro Relay-Dienst auf der Konfigurationsseite) - das Diagnose-Dashboard
# (main.py, laeuft im Docker-Container ohne Root-Zugriff auf /sys/class/
# dmi/id o.ae.) kann collect-fingerprint.sh nicht selbst neu ausfuehren,
# braucht den bereits ermittelten Fingerprint aber fuer den /toggle-port-
# Aufruf beim relay-provisioner (identische Authentifizierung wie bei
# /provision und /verify).
data["device_fingerprint"] = fingerprint
with open(path, "w") as f:
    json.dump(data, f)
' "${RELAY_STATE_FILE}" "${SUBDOMAIN_SLUG}" "${VERIFIED}" "${RELAY_FINGERPRINT}"
chmod 600 "${RELAY_STATE_FILE}"
if [ "${VERIFIED}" = "True" ]; then record_result verified; else record_result unverified; fi

if [ "${VERIFIED}" = "True" ]; then
    log "Relay-Tunnel erfolgreich verifiziert - Ampel steht jetzt auf GRUEN."
else
    log "Relay-Tunnel noch nicht verifizierbar (${VERIFY_RESPONSE}) - Ampel bleibt ROT, naechster stuendlicher Lauf versucht es erneut."
fi
