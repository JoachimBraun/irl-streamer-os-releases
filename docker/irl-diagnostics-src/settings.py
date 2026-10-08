"""Zentrale Konfiguration des Diagnose-Dashboards (V1.89, Modularisierung Schritt 1).

Hier steht NUR, was aus Umgebungsvariablen gelesen wird - an einer Stelle,
ohne Seiteneffekte (kein Dateisystemzugriff, keine Imports von main.py).
Werte und Standardwerte sind 1:1 die bisherigen aus main.py; provision.sh
schreibt sie weiterhin in irl-diagnostics.env. main.py importiert sie hier.
"""
import os
from pathlib import Path

# ---------- Fixe Infrastruktur (nicht im Frontend einstellbar) ----------
# V1.85: neutrale Defaults passend zur Appliance (alles lokal auf dem Mini-
# PC, identisch zu dem, was provision.sh in irl-diagnostics.env schreibt)
# statt der frueheren persoenlichen Entwicklungs-Adressen.
SRTLA_STATS_URL = os.environ.get("SRTLA_STATS_URL", "http://localhost:8181/stats")

OBS_HOST = os.environ.get("OBS_HOST", "localhost")

OBS_PORT = int(os.environ.get("OBS_PORT", "4455"))

OBS_PASSWORD = os.environ.get("OBS_PASSWORD", "")

NOALBS_HOST = os.environ.get("NOALBS_HOST", "")

# Belabox ist erreichbar ausschliesslich ueber die feste WireGuard-Tunnel-
# Adresse (siehe irl-streamer-os/provision/irl-streamer-fernzugriff-
# einrichten.sh + remote-access/belabox-setup.sh, dort .2/24 fest vergeben)
# - NICHT mehr eine frei im Frontend eintragbare LAN-IP (Nutzerentscheidung
# 2026-08-25: die Belabox haengt beim Kunden ohnehin praktisch nie im
# gleichen LAN wie dieser Mini-PC, der Tunnel ist der Normalfall, keine
# Ausnahme). Wird in set_config() zwingend ueber jeden vom Frontend
# gesendeten Wert geschrieben, siehe dort.
BELABOX_HOST = os.environ.get("BELABOX_HOST", "10.10.10.2")

# Relay-Provisioner-URL fuer den Ein/Aus-Schalter pro Dienst (Nutzerwunsch
# 05.09., siehe /toggle-port-Endpunkt dort) - identischer Standardwert wie
# in irl-connectivity-report-client.sh.
RELAY_PROVISIONER_URL = os.environ.get("RELAY_PROVISIONER_URL", "https://relay.irlstreameros.de")

NOALBS_SSH_USER = os.environ.get("NOALBS_SSH_USER", "")

NOALBS_SSH_KEY_PATH = os.environ.get("NOALBS_SSH_KEY_PATH", "/app/ssh/id_ed25519_irl_diag")

NOALBS_LOG_DIR = os.environ.get(
    "NOALBS_LOG_DIR", "/opt/noalbs/noalbs-v2.19.1-x86_64-unknown-linux-musl/logs"
)

# Name der Offline-Szene aus der NOALBS-Konfiguration (switcher.switchingScenes.offline) -
# damit erkennen wir "online"/"offline" direkt am tatsaechlichen Szenennamen statt an der
# Wechsel-Kategorie ([Normal]/[Low]/[Previous]/[Offline]), da "Previous" auf jede der
# anderen Szenen zurueckwechseln kann.
NOALBS_OFFLINE_SCENE = os.environ.get("NOALBS_OFFLINE_SCENE", "BRB")

# Name der Low-Bitrate-Szene (switcher.switchingScenes.low) - separat von
# "online" markiert, da hier zwar noch gestreamt wird, aber mit reduzierter
# Bitrate, was im Dashboard sichtbar von einem normalen Online-Zustand
# unterschieden werden soll.
NOALBS_LOW_SCENE = os.environ.get("NOALBS_LOW_SCENE", "LOW")

NOALBS_CONFIG_PATH = os.environ.get(
    "NOALBS_CONFIG_PATH", "/opt/noalbs/noalbs-v2.19.1-x86_64-unknown-linux-musl/config.json"
)

# ---------- NOALBS-Betriebsart: SSH-VM (Produktiv-Setup) vs. lokaler Docker
# ---------- (IRL-Streamer-OS-Appliance, siehe irl-streamer-os-Projekt) ----------
# "ssh_vm" (bis V1.84 Standard) ist das unveraendert bestehende Verhalten oben - NOALBS
# laeuft dort als eigener systemd-Dienst auf einer separaten VM, angesteuert
# per SSH. "local_docker" ist NEU fuer die Appliance: dort laeuft NOALBS
# gebuendelt im selben Container wie der SRTLA-Relay (Image kezzkezz/belabox,
# per supervisord), auf demselben Host wie dieses Dashboard - Ansteuerung
# per Docker-Socket (docker exec) statt SSH, Config-Datei liegt lokal
# gemountet statt per SFTP erreichbar.
# V1.85: Default "local_docker" (= Appliance, provision.sh setzt es ohnehin
# explizit) - das SSH-VM-Setup muss NOALBS_MODE=ssh_vm jetzt explizit setzen.
NOALBS_MODE = os.environ.get("NOALBS_MODE", "local_docker")

BELABOX_CONTAINER_NAME = os.environ.get("BELABOX_CONTAINER_NAME", "belabox-receiver")

NOALBS_LOCAL_CONFIG_PATH = os.environ.get("NOALBS_LOCAL_CONFIG_PATH", "/app/belabox-config.json")

NOALBS_LOCAL_LOG_PATH = os.environ.get("NOALBS_LOCAL_LOG_PATH", "/var/log/noalbs_stdout.log")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))

# ---------- Login/Session-Schutz (fuer JEDEN Zugriff, LAN wie extern) ----------
# Auf Nutzerwunsch (2026-08-21) gilt der Login jetzt auch im LAN - vorher war
# der Schutz an den von Caddy gesetzten X-External-Access-Header gekoppelt,
# was bei falscher/fehlender Weiterleitung vor Caddy (z.B. ein externer Proxy,
# der direkt auf den Host-Port 9410 statt auf Caddy zeigt) die gesamte
# Zugangsdaten-Konfiguration (SSH/belaUI/Router-Passwoerter, siehe /config)
# ungeschuetzt ausgeliefert haette - live so vorgefunden. Jetzt unabhaengig
# vom Netzwerkpfad, da rein anwendungsseitig durchgesetzt.
# Bootstrap-Admin aus der Erstinstallation (siehe .env) - wird nur benutzt,
# solange USERS_FILE noch nicht existiert (siehe _bootstrap_users unten).
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")

DASHBOARD_PASSWORD_HASH = os.environ.get("DASHBOARD_PASSWORD_HASH", "")

# Secure-Flag: Zugriff laeuft ab V1.85 ausschliesslich ueber Caddy (HTTPS,
# uvicorn bindet nur noch 127.0.0.1, siehe Dockerfile). Nur fuer lokales
# Debugging ohne TLS per DASHBOARD_COOKIE_SECURE=0 abschaltbar.
SESSION_COOKIE_SECURE = os.environ.get("DASHBOARD_COOKIE_SECURE", "1").strip() not in ("0", "false", "no")

# ---------- Host-Control (Nutzerwunsch 07.09.2026: OBS-Notfallknopf + PC-
# Neustart-Knopf im Header) ----------
# Der Dashboard-Container hat keinen Zugriff auf Host-Prozesse (OBS laeuft
# als normales Desktop-Programm in der grafischen Sitzung von 'streamer',
# NICHT im Container) und kann den echten Host nicht neu starten. Statt
# dessen schreibt dieser Endpunkt eine Trigger-Datei in einen gemeinsamen,
# beschreibbaren Ordner (HOST_CONTROL_DIR, siehe docker-compose.yml-Mount)
# - ein root-systemd-Dienst DIREKT auf dem Host (irl-streamer-host-
# control.service, siehe provision/assets/irl-streamer-host-control.py)
# beobachtet diesen Ordner und fuehrt die eigentliche Aktion aus.
HOST_CONTROL_DIR = Path(os.environ.get("HOST_CONTROL_DIR", "/host-control"))
