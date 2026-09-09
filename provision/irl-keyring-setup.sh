#!/usr/bin/env bash
# IRL Streamer OS - Login-Schluesselbund fuer Autologin/Headless-Betrieb
# vorbereiten (Nutzerwunsch 2026-09-01).
#
# PROBLEM: Bei Autologin (kein Passwort wird je eingegeben) hat der
# GNOME-Keyring-PAM-Hook nie ein Passwort, mit dem er den "Login"-
# Schluesselbund automatisch entsperren/anlegen koennte. Jede Anwendung,
# die versucht ein Secret zu speichern (z.B. "grdctl rdp set-credentials"
# fuer den RDP-Server, den Guacamole anspricht, oder Chrome fuer Passwort-
# Speicherung), loest dadurch einen GUI-Passwort-Dialog aus - der bei
# einem unbeaufsichtigten Kiosk-Geraet nie beantwortet wird und das Geraet
# damit dauerhaft ohne funktionierenden Fernzugriff/RDP zuruecklaesst
# (Bugfund 2026-09-01: OHNE dieses Skript blieb "grdctl status" dauerhaft
# auf "Username/Password: (null)" stehen, obwohl set-credentials scheinbar
# durchlief).
#
# LOESUNG: Ein leerer Login-Schluesselbund wird beim ersten Start EINMALIG
# per interner D-Bus-Methode (CreateWithMasterPassword mit leerem Secret)
# angelegt - das ist der offizielle, von "gnome-keyring-daemon --unlock"
# intern genutzte Mechanismus, hier aber OHNE jeden Prompt aufgerufen.
# Live verifiziert (2026-09-01): "grdctl rdp set-credentials" hing zuvor
# nach diesem Fix nicht mehr und Zugangsdaten wurden korrekt gespeichert.
#
# Idempotent: prueft zuerst, ob schon eine 'default'-Collection existiert
# (z.B. von einem frueheren Lauf oder falls der Nutzer doch ein eigenes
# Passwort gesetzt hat) und tut in dem Fall NICHTS.
set -euo pipefail

LOG_PREFIX="[irl-keyring-setup]"
log() { echo "${LOG_PREFIX} $*"; }

KEYRINGS_DIR="${HOME}/.local/share/keyrings"

# Bereits vorhanden (Normalfall bei jedem Login NACH dem allerersten
# erfolgreichen Lauf) -> nichts zu tun, schnell beenden.
if [ -f "${KEYRINGS_DIR}/default" ] && [ -s "${KEYRINGS_DIR}/default" ]; then
    log "Login-Schluesselbund bereits eingerichtet - nichts zu tun."
    exit 0
fi

# Bugfund 2026-09-01 (2. Rollout-Test): GNOME selbst legt beim ALLERERSTEN
# Autologin-Boot manchmal schon VOR diesem Skript eine eigene, leere ABER
# VERSCHLUESSELTE login.keyring (kleingeschrieben) an - mit einem fuer uns
# unbekannten/zufaelligen Passwort, die also NIE automatisch entsperrbar
# ist. Das fuehrte dazu, dass zwei Schluesselbund-Dateien nebeneinander
# existierten (die kaputte "login.keyring" + unsere spaeter erstellte
# "Login.keyring") und der Daemon dabei in einen inkonsistenten Zustand
# geriet (secret-tool/Chrome haengten weiterhin, obwohl "default" schon
# korrekt gesetzt war). Deshalb: VOR dem Anlegen die evtl. vorhandene,
# fremd erstellte kleingeschriebene "login.keyring" ohne Nachfrage
# entfernen - sie enthaelt so oder so 0 Eintraege (frisches System), es
# gibt nichts zu verlieren.
if [ -f "${KEYRINGS_DIR}/login.keyring" ]; then
    log "Verwaiste, von GNOME selbst erzeugte login.keyring gefunden - entferne sie (enthaelt auf einem frischen System keine echten Daten)."
    rm -f "${KEYRINGS_DIR}/login.keyring"
fi

log "Kein Login-Schluesselbund gefunden - richte einen leeren (unverschluesselten) ein."

RESULT="$(timeout 15 python3 - <<'PYEOF'
import sys
import gi
gi.require_version('Secret', '1')
from gi.repository import GLib, Gio

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

# Unverschluesselte 'plain'-Session (kein DH-Handshake noetig fuer diesen
# rein lokalen, vertrauenswuerdigen Aufruf beim eigenen Login).
open_result = bus.call_sync(
    'org.freedesktop.secrets', '/org/freedesktop/secrets',
    'org.freedesktop.Secret.Service', 'OpenSession',
    GLib.Variant('(sv)', ('plain', GLib.Variant('s', ''))),
    GLib.VariantType('(vo)'), Gio.DBusCallFlags.NONE, 10000, None,
)
_, session_path = open_result.unpack()

master_secret = (session_path, b'', b'', 'text/plain')
attrs = {'org.freedesktop.Secret.Collection.Label': GLib.Variant('s', 'Login')}

try:
    result = bus.call_sync(
        'org.freedesktop.secrets', '/org/freedesktop/secrets',
        'org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface',
        'CreateWithMasterPassword',
        GLib.Variant('(a{sv}(oayays))', (attrs, master_secret)),
        GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE, 10000, None,
    )
    collection_path = result.unpack()[0]
except GLib.Error as e:
    print(f"FEHLER: {e}", file=sys.stderr)
    sys.exit(1)

bus.call_sync(
    'org.freedesktop.secrets', '/org/freedesktop/secrets',
    'org.freedesktop.Secret.Service', 'SetAlias',
    GLib.Variant('(so)', ('default', collection_path)),
    None, Gio.DBusCallFlags.NONE, 10000, None,
)
print(collection_path)
PYEOF
)" && log "Login-Schluesselbund erstellt: ${RESULT}" || {
    log "FEHLER: Einrichtung fehlgeschlagen (siehe Ausgabe oben) - RDP/Guacamole-Zugangsdaten koennten nicht gespeichert werden."
    exit 1
}

# --- Chrome-eigenen "Init fehlgeschlagen"-Zustand zuruecksetzen ------------
# Bugfund 2026-09-01 (3. Rollout-Test): Chrome merkt sich in seiner EIGENEN
# Local-State-Datei (~/.config/google-chrome/Local State, Feld
# os_crypt.portal.prev_init_success), ob die Verbindung zum Secret-Service
# beim letzten Start geklappt hat. Startete Chrome UNGLUECKLICH zeitlich
# (z.B. bevor der obige Fix vollstaendig durchgelaufen war, oder bevor
# GNOME selbst fertig gebootet hatte), speichert es "false" und zeigt
# DANACH bei JEDEM weiteren Start erneut einen eigenen Keyring-Passwort-
# Dialog - komplett UNABHAENGIG davon, ob der Login-Schluesselbund selbst
# laengst korrekt eingerichtet ist (live bestaetigt: secret-tool/grdctl
# liefen bereits einwandfrei, Chrome zeigte den Dialog trotzdem weiter).
# Nur relevant, wenn Chrome ueberhaupt schon einmal gestartet wurde und
# dieser Fehlerzustand vorliegt - sonst No-Op.
CHROME_STATE_FILE="${HOME}/.config/google-chrome/Local State"
if [ -f "${CHROME_STATE_FILE}" ]; then
    python3 - "${CHROME_STATE_FILE}" <<'PYEOF' 2>/dev/null || true
import json
import sys

path = sys.argv[1]
with open(path) as f:
    data = json.load(f)

if data.get("os_crypt", {}).get("portal", {}).get("prev_init_success") is False:
    del data["os_crypt"]
    with open(path, "w") as f:
        json.dump(data, f)
    print("[irl-keyring-setup] Chrome-Fehlerzustand (prev_init_success=false) zurueckgesetzt.")
PYEOF
fi
