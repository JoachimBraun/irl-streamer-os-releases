#!/usr/bin/env bash
# IRL Streamer OS - Fernzugriff einrichten (Phase 2, optional).
#
# Automatischer WireGuard-Schluesseltausch mit der Belabox (Nutzerwunsch
# 2026-08-31): das fruehere manuelle Copy-Paste zweier Public Keys zwischen
# zwei Terminal-/Dialogfenstern fuehrte live zu einem Tippfehler (I statt l),
# der den Tunnel komplett unbrauchbar machte, ohne dass das sofort auffiel -
# zu hohe Fehlerquelle fuer eine Aktion, die nur einmalig pro Kunde
# ausgefuehrt wird. Ersetzt sowohl dieses Skript als auch das vorherige
# remote-access/belabox-setup.sh (das manuell per SSH auf der Belabox
# gestartet werden musste) komplett durch EINEN Ablauf, der ausschliesslich
# hier auf dem Mini-PC gestartet wird.
#
# Voraussetzung: Mini-PC und Belabox haengen bei der Einrichtung im selben
# lokalen Netz (ueblicher Fall - Ersteinrichtung beim Kunden zuhause, bevor
# die Belabox mobil unterwegs ist). Verbindet sich selbst per SSH zur
# Belabox (Standard-Zugangsdaten des Belabox-Images), tauscht die
# WireGuard-Schluessel automatisch aus, schreibt beide Konfigurationen und
# verifiziert den Tunnel per Ping - der Nutzer tippt/kopiert keinen
# einzigen Schluessel mehr von Hand.
#
# Baut einen reinen Punkt-zu-Punkt-WireGuard-Tunnel zur Belabox auf - bewusst
# KEIN Headscale/Tailscale/dritter Server dazwischen (Nutzerentscheidung
# 2026-08-25): der Kunde richtet fuer den eigentlichen SRTLA-Stream ohnehin
# schon eine Portweiterleitung + DynDNS fuer diesen Mini-PC ein (Port 5000);
# dieselbe Erreichbarkeit traegt einen zweiten weitergeleiteten Port
# (WireGuard, hier 5001/udp statt des sonst ueblichen 51820) komplett mit.
#
# PORT-SCHEMA (Nutzerentscheidung 2026-09-02, zusammenhaengender leicht zu
# merkender Block statt verstreuter Standardports):
#   5000/udp - SRTLA-Relay (fest im belabox-receiver-Image einkompiliert,
#              NICHT aenderbar ohne eigenen Image-Fork - bleibt daher beim
#              Original-Port, der zufaellig genau in den Block passt)
#   5001/udp - WireGuard (dieser Tunnel)
#   5002/tcp - Caddy HTTPS (Dashboard + Guacamole + OBS-WebSocket gebuendelt)
#   5003/tcp - Caddy HTTP (nur fuer die automatische Let's-Encrypt-Erneuerung)
#
# WICHTIG: Der Stream selbst (alle Modem-Verbindungen der Belabox) darf
# NICHT durch diesen Tunnel laufen, sonst geht die Bonding-Eigenschaft
# verloren - sichergestellt durch ein bewusst ENGES AllowedIPs (nur die
# Tunnel-IP der Belabox, kein 0.0.0.0/0). Dieser Tunnel traegt dadurch
# ausschliesslich BelaUI-Steuer-Traffic, das SRTLA-Bonding bleibt komplett
# unberuehrt.
#
# Laeuft per sudo aus dem Desktop-Icon heraus, braucht daher wie beim
# Reboot-Dialog dort Zugriff auf die Desktop-Session des eingeloggten
# Nutzers fuer die zenity-Dialoge.

set -euo pipefail

TARGET_USER="streamer"
STREAMER_UID="$(id -u "${TARGET_USER}")"
LOG_PREFIX="[irl-streamer-fernzugriff]"
PROJECT_DIR="/opt/irl-streamer-os"
HELPER_DIR="${PROJECT_DIR}/remote-access"

WG_IF="wg0"
WG_DIR="/etc/wireguard"
WG_PORT="5001"
WG_ADDR="10.10.10.1/24"
BELABOX_TUNNEL_IP="10.10.10.2"

# Zustandsdateien des Connectivity-Ampel-Systems (siehe
# provision/irl-connectivity-report-client.sh) - werden HIER NUR GELESEN,
# nie geschrieben. Genutzt, um die Kunden-Subdomain und (falls per Relay
# erreichbar) den individuellen Relay-Port fuer den WireGuard-Fernzugriff
# automatisch zu ermitteln, statt den Nutzer danach zu fragen/raten zu
# lassen (Nutzerfrage 03.09.: "Was gebe ich in dieses Feld genau ein?" -
# das reine Eintippen der Subdomain ohne Port hat bei CGNAT/Relay-Kunden
# nie funktioniert, weil der Relay-Server Port 5001 nicht kennt, nur den
# individuell zugeteilten Relay-Port, siehe wg_fernzugriff_public_port in
# relay-provision.json).
LICENSE_STATE_FILE="$(python3 /opt/irl-streamer-os/provision/licensing/license-locate.py license_file)"
RELAY_STATE_FILE="/opt/irl-streamer-os/state/relay-provision.json"

# Standard-SSH-Benutzername des offiziellen Belabox-Images (OrangePi
# 5+/Rock5B+, Armbian-basiert) - als Vorbelegung im Dialog, vom Nutzer
# aenderbar. Kein Standardpasswort vorbelegt (variiert je nach
# Image-Version/Kundenaenderung) - der Nutzer gibt es explizit ein.
BELABOX_DEFAULT_USER="user"

log() { echo "${LOG_PREFIX} $*"; }

zenity_as_user() {
  sudo -u "${TARGET_USER}" \
    XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
    WAYLAND_DISPLAY="wayland-0" \
    zenity "$@"
}

fail_dialog() {
  zenity_as_user --error --title="IRL Streamer OS - Fernzugriff" \
    --text="$1" --width=460
  log "FEHLER: $1"
  exit 1
}

if ! command -v sshpass >/dev/null 2>&1; then
  log "Installiere sshpass (fuer automatischen Schluesseltausch benoetigt)..."
  apt-get update -qq && apt-get install -y sshpass >/dev/null
fi

# --- 0. Eigenes WireGuard-Schluesselpaar sicherstellen ----------------------
if ! command -v wg >/dev/null 2>&1; then
  log "Installiere WireGuard..."
  apt-get update -qq && apt-get install -y wireguard >/dev/null
fi
mkdir -p "${WG_DIR}"
chmod 700 "${WG_DIR}"
if [ ! -f "${WG_DIR}/privatekey" ]; then
  umask 077
  wg genkey | tee "${WG_DIR}/privatekey" | wg pubkey > "${WG_DIR}/publickey"
fi
MY_PUBKEY="$(cat "${WG_DIR}/publickey")"

# --- 1. Belabox-IP automatisch erkennen, sonst manuell abfragen -----------
# (Nutzerwunsch 2026-09-09): SSH ist auf einer frischen Belabox per Default
# AUS (siehe belabox-bootstrap.sh) und wird erst WEITER UNTEN in diesem
# Ablauf aktiviert - ein SSH-Scan wuerde also nie etwas finden (Henne-Ei-
# Problem, User-Feststellung). belabox-discover.sh scannt daher ausschliesslich
# Port 80 (BelaUIs eigener von Anfang an offener HTTP-Server) und prueft die
# tatsaechliche HTTP-Antwort gegen eine belaUI-typische Signatur (verifiziert
# gegen den echten Quellcode BELABOX/belaUI) - kein belabox.local/mDNS
# (Nutzerentscheidung: nicht sicher, ob jedes Belabox-Image das zuverlaessig
# unterstuetzt), stattdessen ein aktiver Scan mit HTTP-Reply-Pruefung.
log "Suche Belabox automatisch im lokalen Netz (Port 80, belaUI-Signatur)..."
DISCOVERED_JSON="$(bash "${HELPER_DIR}/belabox-discover.sh" 2>/dev/null || echo '[]')"
DISCOVERED_COUNT="$(echo "${DISCOVERED_JSON}" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)"
AUTO_DISCOVERED=0

if [ "${DISCOVERED_COUNT}" -eq 1 ]; then
  # Genau ein Treffer - automatisch uebernehmen, aber dem Nutzer trotzdem
  # kurz anzeigen WAS gefunden wurde (Nachvollziehbarkeit, kein stiller
  # Automatismus) statt ihn ganz zu uebergehen.
  AUTO_IP="$(echo "${DISCOVERED_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["ip"])')"
  if zenity_as_user --question --title="IRL Streamer OS - Fernzugriff" \
      --text="Belabox automatisch gefunden unter: ${AUTO_IP}\n\nSoll diese IP verwendet werden?" \
      --width=440; then
    BELABOX_HOST="${AUTO_IP}"
    AUTO_DISCOVERED=1
    log "Automatisch erkannte Belabox-IP uebernommen: ${BELABOX_HOST}"
  else
    BELABOX_HOST=""
  fi
elif [ "${DISCOVERED_COUNT}" -gt 1 ]; then
  # Mehrere Treffer (z.B. zwei Belaboxen im Testaufbau, oder ein anderes
  # Geraet mit zufaellig identischer HTTP-Signatur) - Auswahlliste statt
  # blind den ersten zu nehmen oder den Nutzer raten zu lassen.
  ZENITY_LIST_ARGS=()
  while IFS= read -r ip; do
    ZENITY_LIST_ARGS+=("${ip}")
  done < <(echo "${DISCOVERED_JSON}" | python3 -c 'import json,sys
for entry in json.load(sys.stdin):
    print(entry["ip"])')
  BELABOX_HOST="$(zenity_as_user --list --title="IRL Streamer OS - Fernzugriff" \
    --text="Mehrere moegliche Belaboxen im lokalen Netz gefunden - bitte die richtige auswaehlen:" \
    --column="IP-Adresse" "${ZENITY_LIST_ARGS[@]}" --width=440 --height=300)" || BELABOX_HOST=""
  [ -n "${BELABOX_HOST}" ] && AUTO_DISCOVERED=1
fi

if [ -z "${BELABOX_HOST:-}" ]; then
  # Kein automatischer Treffer (oder Nutzer hat die automatische Erkennung
  # abgelehnt/abgebrochen) - Rueckfall auf die bisherige manuelle Eingabe,
  # kein hartes Abbrechen des gesamten Ablaufs.
  BELABOX_HOST="$(zenity_as_user --entry \
    --title="IRL Streamer OS - Fernzugriff" \
    --text="Aktuelle LOKALE IP-Adresse der Belabox (im selben Netz wie dieser Mini-PC jetzt gerade, z.B. 192.168.1.50):\n\nDie Belabox-Oberflaeche (BelaUI) zeigt diese IP normalerweise an." \
    --width=480)" || { log "Abgebrochen."; exit 0; }
fi
[ -n "${BELABOX_HOST}" ] || fail_dialog "Keine Belabox-IP eingegeben."

# --- 1b. Bei automatisch erkannter IP: Nutzer anleiten, SSH auf der
# Belabox einzuschalten (Nutzerwunsch 2026-09-09) ---------------------------
# Bei MANUELLER Eingabe hat der Nutzer die BelaUI-Oberflaeche ohnehin schon
# selbst besucht (er hat die IP ja von dort abgelesen) - der Hinweis waere
# dort redundant. Bei automatischer Erkennung sieht der Nutzer die IP zum
# ersten Mal von diesem Dialog, kennt BelaUI evtl. noch gar nicht - daher
# nur in diesem Zweig der explizite Wegweiser zu Advanced/Developer -> SSH,
# mit expliziter Bestaetigung statt eines blinden Timings/Sleep (der
# nachfolgende SSH-Verbindungstest wuerde sonst ins Leere laufen, wenn der
# Nutzer noch nicht fertig ist).
if [ "${AUTO_DISCOVERED}" -eq 1 ]; then
  zenity_as_user --info --title="IRL Streamer OS - Fernzugriff" \
    --text="Deine Belabox wurde im lokalen Netz gefunden unter:\n\n${BELABOX_HOST}\n\nBitte jetzt in einem Browser zu http://${BELABOX_HOST} verbinden.\n\nDort unter 'Advanced' / 'Developer' findest du den SSH-Benutzernamen (Standard: ${BELABOX_DEFAULT_USER}) und das zugehoerige SSH-Passwort.\n\nBitte dort auf 'Start SSH Server' klicken, um SSH auf der Belabox zu aktivieren." \
    --width=500
  zenity_as_user --question --title="IRL Streamer OS - Fernzugriff" \
    --text="Hast du im Browser unter http://${BELABOX_HOST} -> Advanced/Developer auf 'Start SSH Server' geklickt?" \
    --ok-label="OK, habe ich gemacht" --cancel-label="Abbrechen" --width=460 \
    || { log "Abgebrochen (SSH-Aktivierung nicht bestaetigt)."; exit 0; }
fi

BELABOX_SSH_USER="$(zenity_as_user --entry \
  --title="IRL Streamer OS - Fernzugriff" \
  --text="SSH-Benutzername der Belabox (Standard beim Belabox-Image: ${BELABOX_DEFAULT_USER}):" \
  --entry-text="${BELABOX_DEFAULT_USER}" --width=460)" || { log "Abgebrochen."; exit 0; }

BELABOX_SSH_PASSWORD="$(zenity_as_user --entry --hide-text \
  --title="IRL Streamer OS - Fernzugriff" \
  --text="SSH-Passwort der Belabox:" \
  --width=460)" || { log "Abgebrochen."; exit 0; }
[ -n "${BELABOX_SSH_PASSWORD}" ] || fail_dialog "Kein SSH-Passwort eingegeben."

# WebGUI-Passwort (belaUI-Login, Nutzerwunsch 2026-09-05): zusaetzlich zu
# SSH-Zugangsdaten abgefragt, damit alle drei Werte (SSH-User, SSH-Passwort,
# WebGUI-Passwort) automatisch ins Diagnose-Dashboard uebernommen werden
# koennen (siehe Schritt 4c unten) - ohne dieses Skript muesste man das
# WebGUI-Passwort sonst ein zweites Mal manuell in der Dashboard-
# Konfiguration eintragen. Optional (leer lassen erlaubt, z.B. falls
# belaUI noch kein Login-Passwort gesetzt hat) - das Diagnose-Dashboard
# fragt in diesem Fall weiterhin selbst danach, wenn Stream-Steuerung
# gebraucht wird.
BELABOX_UI_PASSWORD="$(zenity_as_user --entry --hide-text \
  --title="IRL Streamer OS - Fernzugriff" \
  --text="WebGUI-Passwort der Belabox (belaUI-Login, NICHT das SSH-Passwort - optional, fuer die automatische Uebernahme ins Diagnose-Dashboard):" \
  --width=480)" || BELABOX_UI_PASSWORD=""

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=no)

log "Pruefe SSH-Erreichbarkeit der Belabox unter ${BELABOX_HOST}..."
if ! sshpass -p "${BELABOX_SSH_PASSWORD}" ssh "${SSH_OPTS[@]}" \
    "${BELABOX_SSH_USER}@${BELABOX_HOST}" "echo ok" >/dev/null 2>&1; then
  fail_dialog "Belabox unter ${BELABOX_HOST} nicht per SSH erreichbar.\n\nPruefe: sind Mini-PC und Belabox im selben Netz? Ist die IP korrekt? Ist SSH auf der Belabox aktiv (BelaUI -> SSH einschalten)?"
fi

# --- 2. Helper-Skripte auf die Belabox uebertragen und ausfuehren ----------
log "Uebertrage Einrichtungs-Helfer auf die Belabox..."
if ! sshpass -p "${BELABOX_SSH_PASSWORD}" scp "${SSH_OPTS[@]}" \
    "${HELPER_DIR}/belabox-bootstrap.sh" "${HELPER_DIR}/belabox-finalize.sh" \
    "${BELABOX_SSH_USER}@${BELABOX_HOST}:/tmp/" >/dev/null 2>&1; then
  fail_dialog "Konnte die Einrichtungs-Helfer nicht auf die Belabox uebertragen (SCP fehlgeschlagen)."
fi

log "Bereite Belabox vor und lese ihren oeffentlichen Schluessel aus..."
SUDO_ASKPASS_CMD="printf '%s\n' \"${BELABOX_SSH_PASSWORD}\""
BELABOX_PUBKEY="$(sshpass -p "${BELABOX_SSH_PASSWORD}" ssh "${SSH_OPTS[@]}" \
  "${BELABOX_SSH_USER}@${BELABOX_HOST}" \
  "${SUDO_ASKPASS_CMD} | sudo --stdin --prompt='' bash /tmp/belabox-bootstrap.sh 2>/dev/null | tail -1")"

if [ -z "${BELABOX_PUBKEY}" ]; then
  fail_dialog "Konnte den oeffentlichen Schluessel der Belabox nicht auslesen (Vorbereitungs-Skript fehlgeschlagen)."
fi
log "Belabox-Schluessel erhalten: ${BELABOX_PUBKEY}"

# --- 3. Eigene wg0.conf schreiben (Server-Rolle: ListenPort, kein Endpoint) -
cat > "${WG_DIR}/${WG_IF}.conf" <<EOF
[Interface]
PrivateKey = $(cat "${WG_DIR}/privatekey")
Address = ${WG_ADDR}
ListenPort = ${WG_PORT}

[Peer]
PublicKey = ${BELABOX_PUBKEY}
# Bewusst NUR die Tunnel-IP der Belabox, nicht 0.0.0.0/0 - der
# SRTLA-Stream-Traffic darf diesen Tunnel nicht nehmen.
AllowedIPs = ${BELABOX_TUNNEL_IP}/32
EOF
chmod 600 "${WG_DIR}/${WG_IF}.conf"
# WICHTIG (Nutzerfehler 2026-08-31, live reproduziert): "enable --now || restart"
# greift NICHT zuverlaessig, wenn der Dienst schon vorher lief (z.B. erneute
# Ausfuehrung nach einer Fehlkonfiguration) - "enable --now" gibt dann bereits
# Exit-Code 0 zurueck (schon aktiviert + "start" auf laufenden Dienst ist ein
# No-Op, ebenfalls erfolgreich), der "||"-Fallback zum Neuladen wird also NIE
# ausgeloest. Ergebnis: die neu geschriebene wg0.conf (mit neuem Peer-Key)
# liegt zwar korrekt auf der Platte, aber der laufende WireGuard-Kernel-
# Zustand behaelt weiterhin den ALTEN Peer-Key - "wg show" zeigt dann einen
# anderen Key als in der Datei steht, der Tunnel bleibt kaputt. Fix: enable
# und restart IMMER beide ausfuehren (restart laedt bei bereits laufendem
# Dienst die Config zuverlaessig neu, bei noch nicht laufendem startet es ihn
# ganz normal).
systemctl enable "wg-quick@${WG_IF}" 2>/dev/null || true
systemctl restart "wg-quick@${WG_IF}"

# --- 4. Belabox-Seite mit dem Mini-PC-Schluessel finalisieren --------------
# LOKALE Verbindung fuer die EINRICHTUNG selbst (Belabox ist ja gerade noch
# im lokalen Netz erreichbar) - die spaeter fuer den Live-Betrieb noetige
# Relay-Subdomain tragen wir ZUSAETZLICH in die Konfiguration ein
# (PersistentKeepalive sorgt dafuer, dass der Tunnel automatisch neu
# aufgebaut wird, sobald die Belabox spaeter ueber die Relay-Subdomain
# erreichbar wird).
#
# WICHTIG (Umbau auf reines Relay-only-Modell): es gibt keine kundenseitige
# DynDNS-Eingabe mehr - JEDER Kunde bekommt automatisch einen Relay-Tunnel
# + eine generierte Subdomain <slug>.irlstreameros.de (siehe
# irl-connectivity-report-client.sh), das ist der einzige Zugriffsweg.
# Subdomain UND der individuelle WireGuard-Fernzugriff-Relay-Port werden
# daher IMMER aus relay-provision.json gelesen, nie manuell abgefragt -
# ohne bereits verifizierten Relay-Tunnel kann dieses Skript den
# spaeteren Fernzugriff noch nicht sinnvoll konfigurieren.
if [ ! -f "${RELAY_STATE_FILE}" ]; then
  fail_dialog "Es wurde noch kein Relay-Tunnel eingerichtet (${RELAY_STATE_FILE} fehlt). Bitte warte, bis der stuendliche Connectivity-Check mindestens einmal gelaufen ist, und versuche es danach erneut."
fi

RELAY_SLUG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("subdomain_slug",""))' "${RELAY_STATE_FILE}" 2>/dev/null || echo "")"
RELAY_VERIFIED="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verified", False))' "${RELAY_STATE_FILE}" 2>/dev/null || echo "False")"
RELAY_WG_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("wg_fernzugriff_public_port") or "")' "${RELAY_STATE_FILE}" 2>/dev/null || echo "")"

if [ -z "${RELAY_SLUG}" ]; then
  fail_dialog "relay-provision.json enthaelt noch kein subdomain_slug - bitte warte auf den naechsten stuendlichen Connectivity-Check und versuche es danach erneut."
fi
if [ "${RELAY_VERIFIED}" != "True" ]; then
  fail_dialog "Der Relay-Tunnel ist noch nicht verifiziert (Ampel steht auf ROT) - der Fernzugriff kann erst eingerichtet werden, sobald die Ampel auf GRUEN steht. Bitte warte auf den naechsten stuendlichen Connectivity-Check."
fi
if [ -z "${RELAY_WG_PORT}" ]; then
  fail_dialog "relay-provision.json enthaelt noch keinen wg_fernzugriff_public_port - bitte warte auf den naechsten stuendlichen Connectivity-Check und versuche es danach erneut."
fi

MINIPC_RELAY_HOST="${RELAY_SLUG}.irlstreameros.de"
EFFECTIVE_WG_PORT="${RELAY_WG_PORT}"
log "Relay-Tunnel ist verifiziert (Ampel: gruen) - nutze Subdomain ${MINIPC_RELAY_HOST} mit individuellem Relay-Port ${EFFECTIVE_WG_PORT} fuer den WireGuard-Fernzugriff."

log "Schreibe finale Konfiguration auf der Belabox..."
BELABOX_RESULT="$(sshpass -p "${BELABOX_SSH_PASSWORD}" ssh "${SSH_OPTS[@]}" \
  "${BELABOX_SSH_USER}@${BELABOX_HOST}" \
  "${SUDO_ASKPASS_CMD} | sudo --stdin --prompt='' bash /tmp/belabox-finalize.sh '${MINIPC_RELAY_HOST}' '${EFFECTIVE_WG_PORT}' '${MY_PUBKEY}' 2>/dev/null")"

if ! echo "${BELABOX_RESULT}" | grep -q "BELABOX_WG_READY"; then
  fail_dialog "Belabox-Konfiguration konnte nicht abgeschlossen werden."
fi

# --- 4b. Kein lokales Let's-Encrypt-Zertifikat mehr noetig -----------------
# Umbau auf reines Relay-only-Modell: der lokale Mini-PC braucht kein
# oeffentliches Let's-Encrypt-Zertifikat mehr fuer eine externe Domain -
# aller externer Zugriff laeuft ueber den Relay-Server, der seinerseits
# TLS/die <slug>.irlstreameros.de-Domain terminiert (siehe
# relay-provisioner/main.py write_customer_caddy_route() - UNVERAENDERT).
# Der lokale Caddy (siehe docker/caddy/Caddyfile) nutzt fuer den
# verbleibenden LAN-Zugriff weiterhin seine interne selbstsignierte CA.

# --- 4c. Belabox-Zugangsdaten automatisch ins Diagnose-Dashboard uebernehmen -
# (Nutzerwunsch 2026-09-05): SSH-Benutzer, SSH-Passwort UND WebGUI-Passwort
# stehen an dieser Stelle bereits vor - ohne diesen Schritt muesste man
# zumindest das WebGUI-Passwort ein zweites Mal manuell auf der
# Konfigurationsseite des Diagnose-Dashboards eintragen. Schreibt/aktualisiert
# gezielt NUR das "belabox"-Profil in device_config.json (jq bewahrt alle
# anderen Felder/Router-Eintraege unveraendert) - reiner Datei-Patch, kein
# API-Aufruf noetig (main.py haelt load_config() ohnehin in einem In-Memory-
# Cache, der erst nach einem Neustart des Containers die geaenderte Datei
# neu einliest, siehe load_config()-Kommentar dort).
DEVICE_CONFIG_FILE="${PROJECT_DIR}/docker/irl-diagnostics-data/device_config.json"
if command -v jq >/dev/null 2>&1; then
  log "Uebernehme Belabox-Zugangsdaten (SSH-User, SSH-Passwort, WebGUI-Passwort) ins Diagnose-Dashboard..."
  mkdir -p "$(dirname "${DEVICE_CONFIG_FILE}")"
  EXISTING_CONFIG="{}"
  [ -f "${DEVICE_CONFIG_FILE}" ] && EXISTING_CONFIG="$(cat "${DEVICE_CONFIG_FILE}")"
  echo "${EXISTING_CONFIG}" | jq \
    --arg ssh_user "${BELABOX_SSH_USER}" \
    --arg ssh_password "${BELABOX_SSH_PASSWORD}" \
    --arg ui_password "${BELABOX_UI_PASSWORD}" \
    '.belabox = ((.belabox // {}) + {ssh_user: $ssh_user, ssh_password: $ssh_password, ui_password: $ui_password})' \
    > "${DEVICE_CONFIG_FILE}.tmp" && mv "${DEVICE_CONFIG_FILE}.tmp" "${DEVICE_CONFIG_FILE}"
  if docker compose -f "${PROJECT_DIR}/docker/docker-compose.yml" restart irl-diagnostics >/dev/null 2>&1; then
    log "Diagnose-Dashboard neu gestartet - Belabox-Zugangsdaten sind jetzt dort bereits eingetragen."
  else
    log "WARNUNG: Diagnose-Dashboard konnte nicht automatisch neu gestartet werden - die Zugangsdaten greifen erst nach einem manuellen Neustart des Containers."
  fi
else
  log "WARNUNG: jq nicht installiert - Belabox-Zugangsdaten wurden NICHT automatisch ins Diagnose-Dashboard uebernommen, bitte manuell auf der Konfigurationsseite eintragen."
fi

# --- 5. Verifikation: Ping durch den frisch aufgebauten Tunnel -------------
log "Pruefe Tunnel-Verbindung..."
sleep 2
RELAY_DIAGNOSTIC_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("diagnostic_public_port") or 5010)' "${RELAY_STATE_FILE}" 2>/dev/null || echo 5010)"
RELAY_GUACAMOLE_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("guacamole_public_port") or 5011)' "${RELAY_STATE_FILE}" 2>/dev/null || echo 5011)"
# BUGFIX (gefunden bei der ersten Live-Testinstallation der Domain:Port-
# Umstellung, 06.09.): hier stand bisher der nie ersetzte woertliche
# Platzhaltertext "<BelaUI-Port>" im Dialogtext - Zenity interpretierte die
# spitzen Klammern zusaetzlich als (kaputtes) Pango-Markup und warf einen
# Parse-Fehler ins Terminal. Liefert jetzt den echten oeffentlichen
# BelaUI-Port aus relay-provision.json (identisches Muster wie Diagnose-
# Dashboard/Guacamole oben).
RELAY_BELABOX_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("belabox_webgui_public_port") or 20200)' "${RELAY_STATE_FILE}" 2>/dev/null || echo 20200)"
if ping -c 3 -W 2 "${BELABOX_TUNNEL_IP}" >/dev/null 2>&1; then
  zenity_as_user --info --title="IRL Streamer OS - Fernzugriff" \
    --text="Fernzugriff erfolgreich eingerichtet und getestet!\n\nBelaUI ist jetzt erreichbar:\n- Lokal im selben Netz: http://${BELABOX_TUNNEL_IP}\n- Von ueberall: http://${MINIPC_RELAY_HOST}:${RELAY_BELABOX_PORT}\n\nDas Diagnose-Dashboard/Guacamole ist von ueberall erreichbar unter:\nhttps://${MINIPC_RELAY_HOST}:${RELAY_DIAGNOSTIC_PORT}\nhttps://${MINIPC_RELAY_HOST}:${RELAY_GUACAMOLE_PORT}\n\nDer WireGuard-Fernzugriff laeuft ueber den individuellen Relay-Port ${EFFECTIVE_WG_PORT} - keine Portweiterleitung am Router noetig." \
    --width=480
  log "Erfolgreich eingerichtet und per Ping verifiziert."
else
  zenity_as_user --warning --title="IRL Streamer OS - Fernzugriff" \
    --text="Konfiguration wurde geschrieben, aber der Tunnel antwortet noch nicht auf Ping.\n\nMoegliche Ursachen: kurz warten und dieses Icon erneut ausfuehren, oder pruefen ob der Relay-Tunnel weiterhin verifiziert ist (Ampel sollte auf GRUEN stehen)." \
    --width=480
  log "WARNUNG: Konfiguration geschrieben, aber Ping-Test fehlgeschlagen."
fi
