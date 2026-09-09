#!/usr/bin/env bash
# IRL Streamer OS - Belabox im lokalen Netz automatisch erkennen
#
# Wird von provision/irl-streamer-fernzugriff-einrichten.sh VOR der
# manuellen IP-Abfrage aufgerufen (Nutzerwunsch 2026-09-09: "automatisch
# erkennen welche IP die Belabox im Heimnetz bekommt", statt sie in BelaUI
# nachschauen und abtippen zu muessen).
#
# WARUM KEIN SSH-Scan (Henne-Ei-Problem, User-Feststellung 09.09.):
# SSH ist auf einer frischen/werksseitigen Belabox per Default AUS
# (Security-by-default, siehe belabox-bootstrap.sh Zeile "SSH dauerhaft
# bootfest machen - ab Werk deaktiviert") - es wird erst GENAU IN DIESEM
# Ablauf (nach der IP-Ermittlung) aktiviert. Ein SSH-Port-Scan wuerde die
# Belabox also grundsaetzlich nie finden, bevor man ihre IP schon kennt.
# EINZIGER von Anfang an offener Dienst ist BelaUIs eigener HTTP-Server
# auf Port 80 - darauf wird deshalb ausschliesslich gescannt.
#
# WARUM KEIN belabox.local/mDNS (Nutzerentscheidung 09.09.): stattdessen
# ein aktiver Scan auf Port 80 + Pruefung der tatsaechlichen HTTP-Antwort
# gegen eine belaUI-typische Signatur - funktioniert mit jedem bereits
# ausgelieferten Belabox-Image, unabhaengig davon, ob/wie es sich per
# mDNS/Avahi ankuendigt (ungeprueft, ob das offizielle Belabox-Image das
# ueberhaupt zuverlaessig tut).
#
# Signatur verifiziert gegen den echten belaUI-Quellcode (BELABOX/belaUI,
# Branch ws_nodejs, public/index.html) UND zusaetzlich LIVE gegen eine
# echte, am 09.09.2026 im LAN angeschlossene Belabox (192.168.10.210)
# bestaetigt: die Startseite liefert IMMER "<title>BELABOX</title>" im
# <head>, UNABHAENGIG vom Login-Zustand (das Login-Formular selbst ist nur
# ein <div id="login"> INNERHALB derselben Seite, kein separates Login-Gate
# auf HTTP-Ebene) - ein reiner TCP-Connect-Test allein waere zu unspezifisch
# (jeder Router/jedes andere Geraet im Heimnetz kann Port 80 offen haben).
#
# ZWEITE, UNABHAENGIGE Bestaetigung (haertet gegen False Positives ab, z.B.
# ein anderes Geraet mit zufaellig aehnlichem <title>): das exakte statische
# Asset /jquery-ui-1.12.1.css muss ebenfalls mit HTTP 200 abrufbar sein -
# live gegen die echte Belabox verifiziert (200 OK). NUR wenn BEIDE Signale
# zutreffen, gilt ein Host als Belabox-Treffer.
BELABOX_HTTP_SIGNATURE="<title>BELABOX</title>"
BELABOX_ASSET_PATH="/jquery-ui-1.12.1.css"
#
# Aufruf: belabox-discover.sh
# Ausgabe (stdout): JSON-Array gefundener Kandidaten, z.B.
#   [{"ip": "192.168.1.50"}, {"ip": "192.168.1.77"}]
# Leeres Array [] wenn nichts gefunden wurde (Aufrufer faellt dann auf die
# manuelle Eingabe zurueck - siehe irl-streamer-fernzugriff-einrichten.sh).
# Exit-Code immer 0 (auch bei leerem Ergebnis) - ein Scan-Fehlschlag ist
# kein hartes Fehlschlagen dieses Skripts, nur ein leeres Ergebnis.

set -uo pipefail

SCAN_TIMEOUT_PER_HOST=1
MAX_PARALLEL=64

log() { echo "[belabox-discover] $*" >&2; }

# --- 1. Lokales /24-Subnetz ermitteln (das Netz, in dem dieser Mini-PC
# selbst gerade eine IPv4-Adresse hat - typischerweise das LAN-Interface
# bei der Ersteinrichtung beim Kunden zuhause). Nur private RFC1918-Netze
# beruecksichtigt, VPN-/Docker-/Loopback-Interfaces ausgeschlossen, damit
# nicht versehentlich ein WireGuard- oder Docker-Bridge-Netz gescannt wird.
mapfile -t LOCAL_NETS < <(
    ip -4 -o addr show scope global 2>/dev/null \
    | awk '{print $2, $4}' \
    | while read -r iface cidr; do
        case "${iface}" in
            lo|docker*|br-*|wg*|veth*|tun*) continue ;;
        esac
        echo "${cidr}"
    done
)

if [ "${#LOCAL_NETS[@]}" -eq 0 ]; then
    log "Kein lokales Netzwerk-Interface gefunden - Scan uebersprungen."
    echo "[]"
    exit 0
fi

# --- 2. Kandidaten-IPs aus jedem gefundenen /24 aufbauen (bewusst nur
# /24 unterstuetzt - deckt praktisch jedes Heimnetz ab; groessere Netze
# waeren fuer einen Vor-Ort-Scan ohnehin unverhaeltnismaessig langsam).
CANDIDATE_IPS=()
for cidr in "${LOCAL_NETS[@]}"; do
    base="${cidr%.*/*}"
    prefix="${cidr#*/}"
    if [ "${prefix}" != "24" ]; then
        log "Ueberspringe ${cidr} (nur /24-Netze werden gescannt)."
        continue
    fi
    for i in $(seq 1 254); do
        CANDIDATE_IPS+=("${base}.${i}")
    done
done

if [ "${#CANDIDATE_IPS[@]}" -eq 0 ]; then
    log "Keine scanbaren /24-Kandidaten-IPs gefunden."
    echo "[]"
    exit 0
fi

log "Scanne ${#CANDIDATE_IPS[@]} Kandidaten-IP(s) auf Port 80 (belaUI-Signatur)..."

# --- 3. Parallel per curl auf Port 80 pruefen - kein nmap-Abhaengigkeit
# noetig (curl ist bereits Systemvoraussetzung fuer den Rest des Projekts).
RESULTS_FILE="$(mktemp)"
trap 'rm -f "${RESULTS_FILE}"' EXIT

check_one() {
    local ip="$1"
    local body
    body="$(curl -fsS --max-time "${SCAN_TIMEOUT_PER_HOST}" "http://${ip}/" 2>/dev/null)" || return 0
    if [[ "${body}" != *"${BELABOX_HTTP_SIGNATURE}"* ]]; then
        return 0
    fi
    # Zweites, unabhaengiges Signal (haertet gegen False Positives ab) -
    # NUR wenn beide zutreffen, gilt der Host als Belabox-Treffer.
    local asset_status
    asset_status="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time "${SCAN_TIMEOUT_PER_HOST}" "http://${ip}${BELABOX_ASSET_PATH}" 2>/dev/null)"
    if [ "${asset_status}" = "200" ]; then
        echo "${ip}"
    fi
}
export -f check_one
export BELABOX_HTTP_SIGNATURE BELABOX_ASSET_PATH SCAN_TIMEOUT_PER_HOST

printf '%s\n' "${CANDIDATE_IPS[@]}" \
    | xargs -P "${MAX_PARALLEL}" -I{} bash -c 'check_one "$@"' _ {} \
    > "${RESULTS_FILE}" 2>/dev/null

mapfile -t FOUND_IPS < <(sort -u "${RESULTS_FILE}")

if [ "${#FOUND_IPS[@]}" -eq 0 ]; then
    log "Keine Belabox gefunden."
    echo "[]"
    exit 0
fi

log "Gefunden: ${FOUND_IPS[*]}"

# --- 4. Als JSON ausgeben (Python fuer korrektes Escaping, statt Ergebnis
# per Hand zusammenzubauen - konsistent mit dem Rest des Projekts, das
# JSON-Interop durchgehend ueber python3 -c erledigt).
python3 -c '
import json, sys
ips = sys.argv[1:]
print(json.dumps([{"ip": ip} for ip in ips]))
' "${FOUND_IPS[@]}"
