import asyncio
import json
import logging
import os
import re
import secrets
import socket
import socketserver
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import contextlib

import bcrypt
import docker as docker_sdk
import httpx
import obsws_python as obs
import paramiko
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Diese Bibliotheken loggen standardmaessig auf INFO-Ebene fuer JEDEN
# einzelnen Request/Verbindungsaufbau - bei den Polling-Intervallen dieses
# Tools (SRTLA 1x/s, SSH/GL.iNet mehrfach pro Poll) fuellte das den
# Docker-Log-Puffer so schnell, dass die Historie eines echten Vorfalls
# (2026-08-21) bereits ueberschrieben war, bevor sie sich ansehen liess -
# siehe auch die groessere max-size in docker-compose.yml. Reine
# Erfolgs-/Verbindungsbanner, keine Fehlerinformation - eigene
# Fehlerbehandlung (try/except) im Code bleibt davon unberuehrt.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("paramiko").setLevel(logging.WARNING)
logging.getLogger("pyglinet").setLevel(logging.WARNING)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_NOALBS_SCENE_RE = re.compile(r"Scene switched to \[\w+\] (.+)$")

# python-glinet setzt keinen HTTP-Timeout (keine Moeglichkeit, einen zu
# konfigurieren) - bei einer nicht erreichbaren IP (kein "Connection refused",
# sondern stille Paketverluste) kann der Aufruf sonst quasi unbegrenzt haengen
# und wuerde den gemeinsamen Standard-Threadpool des Event-Loops verstopfen.
# Deshalb ein eigener, klein gehaltener Pool nur fuer Router-Snapshots -
# haengende Aufrufe bleiben so auf max. 8 gleichzeitige Threads begrenzt und
# blockieren keine anderen Hintergrund-Aufgaben (OBS/NOALBS/SRTLA-Polling).
_router_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="router-snapshot")
ROUTER_SNAPSHOT_TIMEOUT = 20  # Sekunden - grosszuegig, da Cellular-Router unter Last spuerbar traege werden


async def _router_snapshot_with_timeout(belabox_profile: "DeviceProfile", profile: "DeviceProfile") -> dict:
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_router_executor, _router_snapshot, belabox_profile, profile),
            timeout=ROUTER_SNAPSHOT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        # Der haengende Thread selbst laesst sich nicht abbrechen (Python-
        # Threads sind nicht killbar) - er laeuft im Hintergrund weiter und
        # haelt dabei den GL.iNet-Client (siehe _get_router_client) fest, auf
        # den er gerade wartet. Ohne das Verwerfen hier wuerde jeder folgende
        # Poll denselben schon haengenden/kaputten Client wiederverwenden und
        # ebenfalls fuer die vollen 20s haengen bleiben - auf unbestimmte Zeit,
        # live beobachtet (2026-08-21): pyglinet setzt keinen HTTP-Timeout,
        # ein frischer Client zur selben IP verband sich dagegen sofort. Beim
        # jetzigen Belabox-Proxy-Zugriff gilt dieselbe Vorsicht auch fuer die
        # geteilte Belabox-SSH-Verbindung selbst.
        _drop_router_client(profile)
        _drop_belabox_ssh(belabox_profile)
        return {
            "ssh_ok": False,
            "ssh_error": f"Zeitueberschreitung nach {ROUTER_SNAPSHOT_TIMEOUT}s (Geraet antwortet nicht)",
        }

# ---------- Fixe Infrastruktur (nicht im Frontend einstellbar) ----------
SRTLA_STATS_URL = os.environ.get("SRTLA_STATS_URL", "http://192.168.10.9:8181/stats")
OBS_HOST = os.environ.get("OBS_HOST", "192.168.10.10")
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
NOALBS_SSH_USER = os.environ.get("NOALBS_SSH_USER", "joba1980")
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
_NOALBS_THRESHOLD_KEYS = {"low", "rtt", "offline", "rttOffline"}

# ---------- NOALBS-Betriebsart: SSH-VM (Produktiv-Setup) vs. lokaler Docker
# ---------- (IRL-Streamer-OS-Appliance, siehe irl-streamer-os-Projekt) ----------
# "ssh_vm" (Standard) ist das unveraendert bestehende Verhalten oben - NOALBS
# laeuft dort als eigener systemd-Dienst auf einer separaten VM, angesteuert
# per SSH. "local_docker" ist NEU fuer die Appliance: dort laeuft NOALBS
# gebuendelt im selben Container wie der SRTLA-Relay (Image kezzkezz/belabox,
# per supervisord), auf demselben Host wie dieses Dashboard - Ansteuerung
# per Docker-Socket (docker exec) statt SSH, Config-Datei liegt lokal
# gemountet statt per SFTP erreichbar.
NOALBS_MODE = os.environ.get("NOALBS_MODE", "ssh_vm")
BELABOX_CONTAINER_NAME = os.environ.get("BELABOX_CONTAINER_NAME", "belabox-receiver")
NOALBS_LOCAL_CONFIG_PATH = os.environ.get("NOALBS_LOCAL_CONFIG_PATH", "/app/belabox-config.json")
NOALBS_LOCAL_LOG_PATH = os.environ.get("NOALBS_LOCAL_LOG_PATH", "/var/log/noalbs_stdout.log")

# Bewusst KEIN gecachter Singleton-Client (anders als _obs_client_ctx()) -
# live beobachtet (2026-08-24): ein lange wiederverwendeter docker-py-Client
# lieferte nach einer Weile bei exec_run() zuverlaessig leere Ausgaben
# zurueck, obwohl derselbe Aufruf mit einem frischen Client sofort korrekt
# funktionierte (vermutlich eine stale connection im darunterliegenden
# HTTP-Connection-Pool). Bei der hier ohnehin niedrigen Aufruffrequenz
# (Steuer-Endpunkte, kein Sekundentakt) ist ein frischer Client pro Aufruf
# vernachlaessigbarer Overhead - robuster als das Debuggen des Connection-
# Pools von docker-py/urllib3.
def _docker_client() -> "docker_sdk.DockerClient":
    return docker_sdk.from_env()


# Serialisiert ALLE docker-exec-Aufrufe in den belabox-receiver-Container UND
# schliesst den Docker-Client nach jedem einzelnen Aufruf explizit - live
# beobachtet (2026-08-24): vereinzelte "Prozess nicht gefunden"-Fehlschlaege
# trotz tatsaechlich laufendem Prozess traten nur nach vielen vorangegangenen
# Aufrufen im selben lange laufenden App-Prozess auf, nie in einem frischen
# Skript. `docker_sdk.from_env()` ohne explizites close() haeuft ueber die
# Lebensdauer des Prozesses offene Verbindungen an (kein Singleton mehr,
# siehe _docker_client oben) - das noch dazu, nicht Nebenlaeufigkeit allein,
# war die eigentliche Ursache (mit einem frisch neugestarteten Zielcontainer,
# also zurueckgesetztem supervisord-Restart-Zaehler, blieb der Fehler auch
# bei EINEM einzelnen isolierten Aufruf bestehen, bis close() ergaenzt wurde).
# Der Lock bleibt zusaetzlich bestehen (gleiches Muster wie
# _obs_client_lock/_obs_client_ctx), da der Hintergrund-Poller
# (poll_noalbs_local_docker, 5s-Takt) weiterhin nebenlaeufig zu on-demand-
# Aufrufen (Start/Stop/Threshold-Aendern) laeuft.
_belabox_exec_lock = threading.Lock()


def _belabox_exec(cmd):
    with _belabox_exec_lock:
        client = _docker_client()
        try:
            container = client.containers.get(BELABOX_CONTAINER_NAME)
            return container.exec_run(cmd)
        finally:
            client.close()

# Auto-Fix: OBS-Quelle automatisch neu laden, wenn die SRTLA-Verbindung nach
# einem schweren Ausfall (Paket-Drops) wieder stabil ist. Ein volles
# Neu-Oeffnen der Media Source setzt OBS' eigene (in media.c fehlerhafte)
# Timestamp-Verankerung zurueck, die bei Netzwerkquellen nach einem Aussetzer
# nicht mehr korrekt nachjustiert - das ist derselbe Effekt wie der manuelle
# Szenenwechsel, der laut Live-Test das "Kassette verlangsamt"-Symptom
# behebt. WICHTIG: das geschieht ueber SetInputSettings (siehe
# _restart_media_source), NICHT ueber TriggerMediaInputAction(RESTART) - das
# ruft obs_source_media_restart() auf, dessen Seek/Flush-Logik fuer
# Netzwerkquellen (is_local_file=false) in OBS' eigenem Code wirkungslos ist.
SRTLA_DROP_SPIKE_THRESHOLD = 10  # neue unwiderrufliche Drops in einem Poll (~1s), die als Ausfall gelten
SRTLA_RECOVERY_STABLE_POLLS = 3  # so viele Polls ohne neue Drops, bevor der Ausfall als vorbei gilt

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "device_config.json"

# SRTLA bleibt bewusst schnell (nicht auf 5s angehoben wie der Rest): das ist
# die einzige Messgroesse, die den eigentlichen Kernzweck des Tools traegt -
# das Erkennen kurzer Paket-Drop-Spitzen (SRTLA_DROP_SPIKE_THRESHOLD), die
# den "Kassette verlangsamt"-Audiofehler ausloesen, plus die Erholungserkennung
# (SRTLA_RECOVERY_STABLE_POLLS), die den automatischen Reconnect ausloest. Bei
# 5s-Takt wuerde sich sowohl die Erkennungsverzoegerung als auch die Zeit bis
# zum Auto-Reload nach einem echten Ausfall von ~1-3s auf ~5-15s verschlechtern -
# das ist der einzige Wert, bei dem sich "seltener pollen" direkt negativ auf
# die eigentliche Funktion des Tools auswirken wuerde.
POLL_INTERVAL_SRTLA = 1.0
# Alles andere braucht keine Sekundenaufloesung - 5s reicht fuer Status-
# anzeigen und Findings, die ohnehin erst nach mehreren aufeinanderfolgenden
# Polls (Debounce) gemeldet werden.
POLL_INTERVAL_OBS = 5.0
POLL_INTERVAL_OBS_SOURCES = 5.0
POLL_INTERVAL_NOALBS = 5.0
POLL_INTERVAL_ROUTER = 5.0
POLL_INTERVAL_BELABOX = 5.0

app = FastAPI(title="IRL Diagnostics")


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
SESSION_COOKIE = "irl_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 Tage
LOGIN_RATE_LIMIT = 5  # max. Fehlversuche
LOGIN_RATE_WINDOW = 15 * 60  # ...innerhalb von 15 Minuten, pro IP

USERS_FILE = DATA_DIR / "users.json"


class UserRecord(BaseModel):
    username: str
    password_hash: str
    role: str = "user"  # "admin" oder "user"
    created_at: str = ""


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"


class UserPublic(BaseModel):
    username: str
    role: str
    created_at: str = ""


_users_cache: Optional[list[UserRecord]] = None
_users_cache_lock = threading.Lock()
# Token -> {"username", "expires"} (monotonic) - der Benutzername steckt mit
# im Token, damit Admin-Endpunkte wissen, WER angemeldet ist, nicht nur DASS
# jemand angemeldet ist.
_sessions: dict[str, dict] = {}
_login_attempts: dict[str, list] = {}  # IP -> Liste von Fehlversuch-Zeitstempeln


def _bootstrap_users() -> list[UserRecord]:
    if DASHBOARD_USERNAME and DASHBOARD_PASSWORD_HASH:
        return [UserRecord(
            username=DASHBOARD_USERNAME, password_hash=DASHBOARD_PASSWORD_HASH,
            role="admin", created_at=datetime.now(timezone.utc).isoformat(),
        )]
    return []


def _write_users_file(users: list[UserRecord]) -> None:
    tmp = USERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"users": [u.model_dump() for u in users]}, indent=2))
    tmp.replace(USERS_FILE)


def load_users() -> list[UserRecord]:
    # Gleiches Muster wie load_config(): In-Memory-Cache, nur bei save_users()
    # invalidiert.
    global _users_cache
    with _users_cache_lock:
        if _users_cache is not None:
            return _users_cache
        if USERS_FILE.exists():
            data = json.loads(USERS_FILE.read_text())
            users = [UserRecord.model_validate(u) for u in data.get("users", [])]
        else:
            # Erststart: aus den DASHBOARD_*-Env-Variablen einen Admin
            # anlegen und sofort persistieren, damit er einen Neustart
            # uebersteht, selbst wenn nie ein Nutzer ueber /users angelegt
            # wird.
            users = _bootstrap_users()
            if users:
                _write_users_file(users)
        _users_cache = users
        return users


def save_users(users: list[UserRecord]) -> None:
    global _users_cache
    _write_users_file(users)
    with _users_cache_lock:
        _users_cache = users


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_RATE_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) >= LOGIN_RATE_LIMIT


def _record_failed_login(ip: str):
    _login_attempts.setdefault(ip, []).append(time.monotonic())


def _create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"username": username, "expires": time.monotonic() + SESSION_MAX_AGE}
    return token


def _session_username(token: Optional[str]) -> Optional[str]:
    if not token or token not in _sessions:
        return None
    entry = _sessions[token]
    if time.monotonic() > entry["expires"]:
        del _sessions[token]
        return None
    return entry["username"]


def _session_valid(token: Optional[str]) -> bool:
    return _session_username(token) is not None


def _current_admin(request: Request) -> UserRecord:
    # Als FastAPI-Dependency fuer alle /users-Endpunkte: wirft 401/403 statt
    # stillschweigend leere Daten zurueckzugeben, wenn kein gueltiger Admin
    # angemeldet ist.
    username = _session_username(request.cookies.get(SESSION_COOKIE))
    if not username:
        raise HTTPException(status_code=401, detail="nicht angemeldet")
    user = next((u for u in load_users() if u.username == username), None)
    if not user or user.role != "admin":
        raise HTTPException(status_code=403, detail="nur fuer Admins")
    return user


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/logout"):
        return await call_next(request)
    if _session_valid(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if "text/html" in request.headers.get("accept", ""):
        # X-Forwarded-Prefix wird von Caddy gesetzt, wenn dieses Dashboard
        # hinter einem Pfad-Praefix laeuft (siehe docker/caddy/Caddyfile,
        # Abschnitt "alle_dienste_gebuendelt" - Nutzerentscheidung
        # 2026-09-02: alle Web-Dienste hinter EINEM HTTPS-Port buendeln).
        # Fehlt der Header (z.B. direkter lokaler Zugriff auf Port 8300
        # ohne Caddy dazwischen, etwa beim Debuggen), bleibt das Praefix
        # leer und der Redirect verhaelt sich exakt wie vorher.
        prefix = request.headers.get("x-forwarded-prefix", "")
        return RedirectResponse(url=f"{prefix}/login", status_code=302)
    return JSONResponse({"error": "nicht angemeldet"}, status_code=401)


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


@app.get("/lite")
def lite_dashboard():
    """Abgespeckte Ansicht (Nutzerwunsch 07.09.2026, Redesign nach Vorbild
    einer bereits vom Nutzer selbst gebauten Referenz-Installation unter
    /quick): eigene, merkbare URL fuer eine reduzierte Kontrollflaeche mit
    NUR den wichtigsten Knoepfen (Belabox-Stream, SRTLA-Vorschau+Bild, OBS
    Stream/Aufnahme, FIX, DJI Verbinden, OBS-Notfall, PC-Neustart) - siehe
    static/lite.html. BEWUSST eine eigenstaendige, schlanke Datei (nicht
    die grosse index.html mit CSS-Ausblenden wie im ersten Anlauf 07.09.):
    der Nutzer wollte explizit 'nur genau die Knoepfe... nicht die ganze
    GUI von dem Reiter', ein reines CSS-Verstecken der index.html blieb
    trotz mehrfacher Nachbesserung visuell zu nah am vollen Dashboard.
    lite.html nutzt DIESELBEN Backend-Endpunkte wie index.html
    (/belabox/stream/*, /obs/preview/*, /obs/stream/*, /obs/record/*,
    /obs/source/fix, /infra-status, /ws, /test/start, /test/status,
    /config) - kein neuer Backend-Code noetig, nur ein neues, schlankes
    Frontend obendrauf."""
    return FileResponse("static/lite.html")


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip):
        return JSONResponse(
            {"error": "Zu viele Fehlversuche - bitte 15 Minuten warten."}, status_code=429
        )
    user = next((u for u in load_users() if secrets.compare_digest(u.username, username)), None)
    valid = user is not None and bcrypt.checkpw(password.encode(), user.password_hash.encode())
    if not valid:
        _record_failed_login(ip)
        return JSONResponse({"error": "Benutzername oder Passwort falsch."}, status_code=401)
    token = _create_session(user.username)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
        httponly=True, secure=False, samesite="lax",
    )
    return response


@app.get("/whoami")
def whoami(request: Request):
    username = _session_username(request.cookies.get(SESSION_COOKIE))
    user = next((u for u in load_users() if u.username == username), None) if username else None
    if not user:
        raise HTTPException(status_code=401, detail="nicht angemeldet")
    return {"username": user.username, "role": user.role}


@app.get("/users")
def list_users(admin: UserRecord = Depends(_current_admin)):
    return [UserPublic(username=u.username, role=u.role, created_at=u.created_at) for u in load_users()]


@app.post("/users")
def create_user(payload: UserCreate, admin: UserRecord = Depends(_current_admin)):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Benutzername darf nicht leer sein")
    if not payload.password or len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Passwort muss mindestens 8 Zeichen haben")
    if payload.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Rolle muss 'admin' oder 'user' sein")
    users = load_users()
    if any(u.username == username for u in users):
        raise HTTPException(status_code=409, detail="Benutzername existiert bereits")
    password_hash = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt()).decode()
    users = users + [UserRecord(
        username=username, password_hash=password_hash, role=payload.role,
        created_at=datetime.now(timezone.utc).isoformat(),
    )]
    save_users(users)
    return {"ok": True}


@app.delete("/users/{username}")
def delete_user(username: str, admin: UserRecord = Depends(_current_admin)):
    users = load_users()
    remaining = [u for u in users if u.username != username]
    if len(remaining) == len(users):
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden")
    if not any(u.role == "admin" for u in remaining):
        raise HTTPException(status_code=400, detail="Der letzte Admin kann nicht geloescht werden")
    save_users(remaining)
    # Bestehende Sessions dieses Nutzers sofort ungueltig machen, statt auf
    # deren natuerliches Ablaufen (bis zu 30 Tage) zu warten.
    for token, entry in list(_sessions.items()):
        if entry["username"] == username:
            del _sessions[token]
    return {"ok": True}


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.post("/system/restart")
async def restart_dashboard():
    """Startet NUR diesen Dashboard-Container neu (nicht Belabox/OBS/NOALBS/
    Router) - fuer Faelle wie haengengebliebene Live-Updates (z.B. OBS-
    Aufnahmestatus), bei denen bisher ein manueller Docker-Neustart auf
    Unraid noetig war. Kein Docker-Socket-Zugriff noetig: der Container
    faehrt per os._exit() runter, docker-compose.yml setzt fuer diesen
    Service ohnehin 'restart: unless-stopped', das startet ihn automatisch
    frisch neu. Verzoegerung, damit die HTTP-Antwort noch beim Client
    ankommt, bevor der Prozess wirklich endet."""
    async def _delayed_exit():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_delayed_exit())
    return {"ok": True}


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
HOST_CONTROL_TIMEOUT = 15  # Sekunden, wie lange auf eine *.result-Datei gewartet wird


async def _host_control_trigger(action: str, wait_for_result: bool = True) -> dict:
    """Schreibt eine Trigger-Datei und wartet (falls gewuenscht) auf das
    Ergebnis. wait_for_result=False fuer 'reboot' - der Host-Watcher
    schreibt die Ergebnisdatei zwar auch dort, aber die Maschine kann
    jederzeit noch WAEHREND des Wartens verschwinden, ein Timeout waere
    dann faelschlich eine Fehlermeldung fuer eine eigentlich erfolgreiche
    Aktion."""
    HOST_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    trigger_file = HOST_CONTROL_DIR / f"{action}.trigger"
    result_file = HOST_CONTROL_DIR / f"{action}.result"
    result_file.unlink(missing_ok=True)
    trigger_file.write_text("")
    if not wait_for_result:
        return {"ok": True}
    deadline = time.monotonic() + HOST_CONTROL_TIMEOUT
    while time.monotonic() < deadline:
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
            except Exception:
                data = {"ok": False, "error": "Ergebnisdatei war nicht lesbar"}
            result_file.unlink(missing_ok=True)
            return data
        await asyncio.sleep(0.3)
    return {"ok": False, "error": "Host-Control-Dienst hat nicht rechtzeitig geantwortet (laeuft irl-streamer-host-control.service?)"}


@app.post("/host/obs/start")
async def host_obs_start():
    result = await _host_control_trigger("obs_start")
    if not result.get("ok"):
        return JSONResponse(result, status_code=502)
    return result


@app.post("/host/obs/stop")
async def host_obs_stop():
    result = await _host_control_trigger("obs_stop")
    if not result.get("ok"):
        return JSONResponse(result, status_code=502)
    return result


@app.post("/host/system/reboot")
async def host_system_reboot():
    # wait_for_result=False (siehe _host_control_trigger-Docstring) - die
    # Maschine startet gleich neu, ein Timeout-Warten auf eine
    # Ergebnisdatei ist hier sinnlos und wuerde nur unnoetig lange auf der
    # Client-Seite haengen.
    await _host_control_trigger("reboot", wait_for_result=False)
    return {"ok": True}


def _obs_process_running() -> bool:
    """Fuer die Gruen/Rot-Anzeige des OBS-Notfallknopfes: liest den vom
    Host-Watcher periodisch geschriebenen Status (siehe obs_status.json in
    provision/assets/irl-streamer-host-control.py) statt selbst 'pgrep' im
    Container aufzurufen - der Container laeuft NICHT mit 'pid: host', ein
    'pgrep -x obs' im Container saehe also nur Container-eigene Prozesse,
    niemals den echten OBS-Prozess auf dem Host. Bewusst NICHT ueber die
    bestehende OBS-Websocket-Verbindung (_obs_snapshot_safe) gehen: die
    zeigt nur, ob obs-websocket ERREICHBAR ist (kann bei falschem
    Passwort/Plugin-Fehler negativ sein, obwohl OBS laeuft) - hier soll
    strikt nur der Prozess selbst gemeint sein, unabhaengig von dessen
    interner Konfiguration."""
    status_file = HOST_CONTROL_DIR / "obs_status.json"
    try:
        data = json.loads(status_file.read_text())
    except Exception:
        return False
    # Aeltere Status-Meldung als 10s gilt als veraltet (Watcher haengt/laeuft
    # nicht) - dann lieber "nicht sicher" (False) anzeigen als einen
    # potenziell laengst falschen Stand.
    checked_at = data.get("checked_at")
    if checked_at is None or (time.time() - checked_at) > 10:
        return False
    return bool(data.get("running"))


# ---------- Dauerhafter Hintergrund-Status fuer feste Infrastruktur ----------
# NOALBS-VM und SRTLA-Relay aendern nie Host/Zugangsdaten, daher kein
# Test-Knopf noetig - stattdessen laeuft das unabhaengig von Start/Stop-Test
# permanent im Hintergrund und wird per Polling im Frontend angezeigt.

infra_status: dict = {
    "license": {"ok": None, "state": None, "checked_at": None, "error": None, "days_remaining": None},
    "noalbs": {"ok": None, "checked_at": None, "error": None},
    "srtla": {"ok": None, "checked_at": None, "error": None},
    "obs": {"ok": None, "checked_at": None, "error": None},
    "obs_process": {"running": None, "checked_at": None},
    # router1, router2, ... werden dynamisch von infra_watcher() befuellt,
    # je nachdem wie viele Router aktuell konfiguriert sind.
}

POLL_INTERVAL_INFRA = 5.0

# Wird von poll_router() (laeuft als Teil der - inzwischen dauerhaft aktiven -
# Test-Session) befuellt. infra_watcher() liest Router-Status von hier statt
# ihn selbst nochmal separat abzufragen: zwei unabhaengige Poller haetten pro
# Router doppelt so oft eingeloggt wie noetig (siehe _get_router_client).
_last_router_snapshot: dict = {}
# Router-Labels, fuer die aktuell ein poll_router()-Task laeuft. Da die
# Test-Session dauerhaft laeuft (kein Start/Stop mehr) und ihre Poller-Liste
# beim Start fixiert wird, wuerde ein spaeter (ohne Neustart) hinzugefuegter
# Router sonst nie gepollt - infra_watcher() erkennt das ueber dieses Set und
# uebernimmt fuer solche "verwaisten" Router als Fallback selbst das Polling.
_actively_polled_routers: set = set()


# kezzkezz/belabox konfiguriert supervisord OHNE [unix_http_server]-Sektion
# (live verifiziert, 2026-08-24: "unix:///tmp/supervisor.sock no such file",
# der Socket wird nie erstellt) - supervisorctl kann sich also grundsaetzlich
# NIE verbinden, unabhaengig vom Pfad. Steuerung laeuft deshalb direkt ueber
# POSIX-Signale an den Prozess (gefunden per reinem /proc-Scan, da das Image
# nicht mal `ps`/`pgrep` mitbringt - nur eine minimale sh mit `kill`-Builtin):
# SIGSTOP/SIGCONT zum Pausieren/Fortsetzen (kein "richtiger" Exit, daher
# greift supervisords autorestart=unexpected nicht ein), SIGTERM zum
# Neustarten (das ZAEHLT als unexpected -> supervisord startet automatisch
# neu und liest dabei die frisch geschriebene config.json neu ein - das ist
# hier erwuenscht, siehe _noalbs_write_threshold_local_docker unten).
def _local_docker_find_pid(process_name: str) -> Optional[int]:
    script = (
        'for p in /proc/[0-9]*; do '
        f'n=$(cat "$p/comm" 2>/dev/null); '
        f'[ "$n" = "{process_name}" ] && echo "${{p#/proc/}}" && break; '
        'done'
    )
    # Retry als zusaetzliche Absicherung gegen kurzzeitige Luecken (z.B.
    # unmittelbar nach einem Neustart, bevor der neue Prozess vollstaendig
    # sichtbar ist) - die eigentliche Ursache wiederholter Fehlschlaege war
    # aber das fehlende close() oben, nicht Timing (siehe dortiger Kommentar).
    for attempt in range(2):
        code, output = _belabox_exec(["sh", "-c", script])
        pid_str = output.decode(errors="replace").strip()
        if pid_str.isdigit():
            return int(pid_str)
        if attempt == 0:
            time.sleep(0.3)
    return None


def _check_noalbs_local_docker() -> tuple[bool, Optional[str]]:
    try:
        pid = _local_docker_find_pid("noalbs")
        if pid is None:
            return False, "NOALBS-Prozess im belabox-receiver-Container nicht gefunden"
        code, output = _belabox_exec(["sh", "-c", f'awk \'{{print $3}}\' /proc/{pid}/stat'])
        state = output.decode(errors="replace").strip()
        if state == "T":
            return False, "NOALBS-Prozess ist pausiert (manuell deaktiviert)"
        return True, None
    except Exception as exc:
        return False, str(exc)


def _check_noalbs() -> tuple[bool, Optional[str]]:
    if NOALBS_MODE == "local_docker":
        return _check_noalbs_local_docker()
    try:
        ssh = _ssh_connect(NOALBS_HOST, NOALBS_SSH_USER, key_path=NOALBS_SSH_KEY_PATH)
        try:
            _, stdout, _ = ssh.exec_command("systemctl is-active noalbs")
            state = stdout.read().decode().strip()
            if state != "active":
                return False, f"NOALBS-Service ist '{state}', nicht 'active'"
            return True, None
        finally:
            ssh.close()
    except Exception as exc:
        return False, str(exc)


_NOALBS_LOCAL_SIGNALS = {"start": "CONT", "stop": "STOP", "restart": "TERM"}


def _noalbs_service_command_local_docker(action: str) -> tuple[bool, Optional[str]]:
    sig = _NOALBS_LOCAL_SIGNALS.get(action)
    if sig is None:
        return False, f"Unbekannte Aktion fuer lokalen Docker-Modus: {action}"
    try:
        pid = _local_docker_find_pid("noalbs")
        if pid is None:
            # supervisord gibt nach zu vielen Neustarts in kurzer Zeit
            # dauerhaft auf (startretries, live beobachtet 2026-08-24) - kein
            # supervisorctl verfuegbar, um das gezielt zurueckzusetzen (siehe
            # Kommentar oben). Fuer "restart" ist ein kompletter
            # Container-Neustart (live als zuverlaessig verifiziert) daher
            # eine akzeptable Eskalation - kurze Unterbrechung auch des
            # SRTLA-Relays, aber nur bei einer gezielten Config-Aenderung,
            # nicht im Normalbetrieb. Fuer reines Pausieren/Fortsetzen
            # (stop/start) waere das unangemessen heftig - dort bleibt es
            # beim einfachen Fehler.
            if action != "restart":
                return False, "NOALBS-Prozess im belabox-receiver-Container nicht gefunden"
            client = _docker_client()
            try:
                container = client.containers.get(BELABOX_CONTAINER_NAME)
                container.restart(timeout=10)
            finally:
                client.close()
            return True, None
        code, output = _belabox_exec(["sh", "-c", f"kill -{sig} {pid}"])
        if code != 0:
            return False, output.decode(errors="replace").strip() or f"kill -{sig} {pid} fehlgeschlagen"
        return True, None
    except Exception as exc:
        return False, str(exc)


def _noalbs_service_command(action: str) -> tuple[bool, Optional[str]]:
    if NOALBS_MODE == "local_docker":
        return _noalbs_service_command_local_docker(action)
    # Zum manuellen An/Ausschalten waehrend Szenenbearbeitung in OBS (sonst
    # switcht NOALBS sofort wieder zurueck). Braucht auf der NOALBS-VM eine
    # gezielte passwortlose sudo-Freigabe fuer genau diese zwei Befehle
    # (NOPASSWD in /etc/sudoers.d/, User joba1980) - der SSH-Key allein reicht
    # nicht, da der Dienst als root laeuft.
    try:
        ssh = _ssh_connect(NOALBS_HOST, NOALBS_SSH_USER, key_path=NOALBS_SSH_KEY_PATH)
    except Exception as exc:
        return False, f"SSH-Verbindung fehlgeschlagen: {exc}"
    try:
        _, stdout, stderr = ssh.exec_command(f"sudo -n systemctl {action} noalbs")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            err = stderr.read().decode(errors="replace").strip()
            return False, err or f"systemctl {action} fehlgeschlagen (Exit-Code {code})"
        return True, None
    finally:
        ssh.close()


def _noalbs_get_thresholds_local_docker() -> dict:
    with open(NOALBS_LOCAL_CONFIG_PATH, "r") as f:
        cfg = json.loads(f.read())
    return cfg.get("switcher", {}).get("triggers", {})


def _noalbs_get_thresholds() -> dict:
    if NOALBS_MODE == "local_docker":
        return _noalbs_get_thresholds_local_docker()
    ssh = _ssh_connect(NOALBS_HOST, NOALBS_SSH_USER, key_path=NOALBS_SSH_KEY_PATH)
    try:
        sftp = ssh.open_sftp()
        with sftp.open(NOALBS_CONFIG_PATH, "r") as f:
            cfg = json.loads(f.read().decode())
        return cfg.get("switcher", {}).get("triggers", {})
    finally:
        ssh.close()


def _noalbs_write_threshold_local_docker(key: str, value: Optional[int]) -> dict:
    # Datei liegt direkt gemountet (siehe docker-compose.yml) - kein
    # Verzeichnis-Rechte-Problem wie bei der VM, einfaches Ueberschreiben
    # reicht. Schreibt NUR die Datei (kein eigener Neustart hier!) - der
    # aufrufende Endpunkt (noalbs_set_threshold) kuemmert sich danach
    # modusabhaengig ums Wirksammachen. Live-Fund (2026-08-24): urspruenglich
    # loeste diese Funktion selbst schon per SIGTERM einen Neustart aus, UND
    # der Endpunkt hat direkt danach nochmal per stop+start "nachgeholfen" -
    # zwei Neustart-Versuche in Millisekunden-Abstand trafen den Prozess
    # zuverlaessig genau in der kurzen Luecke zwischen Sterben des alten und
    # vollstaendigem Erscheinen des neuen Prozesses in /proc.
    with open(NOALBS_LOCAL_CONFIG_PATH, "r") as f:
        cfg = json.loads(f.read())
    cfg.setdefault("switcher", {}).setdefault("triggers", {})[key] = value
    with open(NOALBS_LOCAL_CONFIG_PATH, "w") as f:
        f.write(json.dumps(cfg, indent=2))
    return cfg["switcher"]["triggers"]


def _noalbs_config_from_json(cfg: dict) -> "NoalbsConfig":
    sw = cfg.get("switcher", {})
    scenes = sw.get("switchingScenes", {})
    chat = cfg.get("chat", {})
    opt = cfg.get("optionalOptions", {})
    return NoalbsConfig(
        bitrate_switcher_enabled=sw.get("bitrateSwitcherEnabled", True),
        only_switch_when_streaming=sw.get("onlySwitchWhenStreaming", False),
        instantly_switch_on_recover=sw.get("instantlySwitchOnRecover", False),
        auto_switch_notification=sw.get("autoSwitchNotification", False),
        reconnect_delay=sw.get("reconnectDelay", 3000),
        scene_switch_delay=sw.get("sceneSwitchDelay", 3000),
        offline_timeout=sw.get("offlineTimeout", 15000),
        scene_normal=scenes.get("normal", "LIVE"),
        scene_low=scenes.get("low", "LOW"),
        scene_offline=scenes.get("offline", "BRB"),
        chat_username=chat.get("username", ""),
        chat_prefix=chat.get("prefix", "!"),
        chat_enable_public_commands=chat.get("enablePublicCommands", False),
        chat_enable_auto_stop_on_host_or_raid=chat.get("enableAutoStopStreamOnHostOrRaid", False),
        chat_admins=chat.get("admins", []),
        record_while_streaming=opt.get("recordWhileStreaming", False),
    )


def _noalbs_get_settings_local_docker() -> "NoalbsConfig":
    with open(NOALBS_LOCAL_CONFIG_PATH, "r") as f:
        cfg = json.loads(f.read())
    return _noalbs_config_from_json(cfg)


def _noalbs_get_settings() -> "NoalbsConfig":
    if NOALBS_MODE == "local_docker":
        return _noalbs_get_settings_local_docker()
    ssh = _ssh_connect(NOALBS_HOST, NOALBS_SSH_USER, key_path=NOALBS_SSH_KEY_PATH)
    try:
        sftp = ssh.open_sftp()
        with sftp.open(NOALBS_CONFIG_PATH, "r") as f:
            cfg = json.loads(f.read().decode())
        return _noalbs_config_from_json(cfg)
    finally:
        ssh.close()


def _apply_noalbs_settings(cfg: dict, settings: "NoalbsConfig") -> dict:
    sw = cfg.setdefault("switcher", {})
    sw["bitrateSwitcherEnabled"] = settings.bitrate_switcher_enabled
    sw["onlySwitchWhenStreaming"] = settings.only_switch_when_streaming
    sw["instantlySwitchOnRecover"] = settings.instantly_switch_on_recover
    sw["autoSwitchNotification"] = settings.auto_switch_notification
    sw["reconnectDelay"] = settings.reconnect_delay
    sw["sceneSwitchDelay"] = settings.scene_switch_delay
    sw["offlineTimeout"] = settings.offline_timeout
    scenes = sw.setdefault("switchingScenes", {})
    scenes["normal"] = settings.scene_normal
    scenes["low"] = settings.scene_low
    scenes["offline"] = settings.scene_offline
    chat = cfg.setdefault("chat", {})
    chat["username"] = settings.chat_username
    chat["prefix"] = settings.chat_prefix
    chat["enablePublicCommands"] = settings.chat_enable_public_commands
    chat["enableAutoStopStreamOnHostOrRaid"] = settings.chat_enable_auto_stop_on_host_or_raid
    chat["admins"] = settings.chat_admins
    opt = cfg.setdefault("optionalOptions", {})
    opt["recordWhileStreaming"] = settings.record_while_streaming
    return cfg


def _noalbs_write_settings_local_docker(settings: "NoalbsConfig") -> None:
    # Kein eigener Neustart hier (siehe Kommentar bei
    # _noalbs_write_threshold_local_docker) - der aufrufende Endpunkt loest
    # GENAU EINEN Neustart aus, nachdem geschrieben wurde.
    with open(NOALBS_LOCAL_CONFIG_PATH, "r") as f:
        cfg = json.loads(f.read())
    _apply_noalbs_settings(cfg, settings)
    with open(NOALBS_LOCAL_CONFIG_PATH, "w") as f:
        f.write(json.dumps(cfg, indent=2))


def _noalbs_write_settings(settings: "NoalbsConfig") -> None:
    if NOALBS_MODE == "local_docker":
        _noalbs_write_settings_local_docker(settings)
        return
    ssh = _ssh_connect(NOALBS_HOST, NOALBS_SSH_USER, key_path=NOALBS_SSH_KEY_PATH)
    try:
        sftp = ssh.open_sftp()
        with sftp.open(NOALBS_CONFIG_PATH, "r") as f:
            cfg = json.loads(f.read().decode())
        _apply_noalbs_settings(cfg, settings)
        with sftp.open(NOALBS_CONFIG_PATH, "w") as f:
            f.write(json.dumps(cfg, indent=2))
    finally:
        ssh.close()


def _noalbs_write_threshold(key: str, value: Optional[int]) -> dict:
    if NOALBS_MODE == "local_docker":
        return _noalbs_write_threshold_local_docker(key, value)
    # config.json gehoert jetzt joba1980 (nach `sudo chown`), das umgebende
    # Verzeichnis aber weiterhin dem verwaisten uid 1001 mit rwxr-xr-x - eine
    # Temp-Datei im selben Ordner anlegen (klassisches atomares Schreibmuster)
    # scheiterte deshalb an fehlendem Schreibzugriff aufs VERZEICHNIS
    # ("Permission denied"), obwohl die Datei selbst beschreibbar ist. Direktes
    # Ueberschreiben der bestehenden Datei braucht dagegen nur Schreibrechte
    # auf die Datei - das reicht mit dem bereits gesetzten chown.
    ssh = _ssh_connect(NOALBS_HOST, NOALBS_SSH_USER, key_path=NOALBS_SSH_KEY_PATH)
    try:
        sftp = ssh.open_sftp()
        with sftp.open(NOALBS_CONFIG_PATH, "r") as f:
            cfg = json.loads(f.read().decode())
        cfg.setdefault("switcher", {}).setdefault("triggers", {})[key] = value
        with sftp.open(NOALBS_CONFIG_PATH, "w") as f:
            f.write(json.dumps(cfg, indent=2))
        return cfg["switcher"]["triggers"]
    finally:
        ssh.close()


async def _check_srtla() -> tuple[bool, Optional[str]]:
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(SRTLA_STATS_URL)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                return False, f"unerwartete Antwort: {data}"
            return True, None
    except Exception as exc:
        return False, str(exc)


def _obs_snapshot_safe() -> tuple[bool, Optional[str]]:
    try:
        _obs_snapshot()
        return True, None
    except Exception as exc:
        return False, str(exc)


def _check_license() -> dict:
    """Liest den lokalen Lizenz-/Testphasenstand ueber das bereits
    bestehende, offline arbeitende license-check.py (siehe
    provision/licensing/) - keine eigene Signaturpruefung hier, sondern
    Wiederverwendung derselben Quelle der Wahrheit wie der taegliche
    Timer (license-daily-check.sh) und license-guard.sh. Pfade sind auf
    dem lizenzierten Host fest (siehe docker-compose.yml Bind-Mounts),
    NICHT konfigurierbar - dieses Dashboard laeuft immer auf demselben
    Rechner wie das Lizenzsystem selbst (network_mode: host).

    Rueckgabe fuers Frontend (analog zu den anderen infra_status-
    Eintraegen, aber mit eigenen Zusatzfeldern days_remaining/kind):
      {"ok": True,  "state": "valid",   "kind": "trial"|"license",
       "days_remaining": N, "expires_at": "...", "error": None}
      {"ok": False, "state": "expired"|"invalid"|"missing", "error": "..."}
    """
    resolver_script = Path("/opt/irl-streamer-os/provision/licensing/license-locate.py")
    check_script = Path("/opt/irl-streamer-os/provision/licensing/license-check.py")

    if not resolver_script.exists() or not check_script.exists():
        # Laeuft dieses Dashboard z.B. im Testbed (mehrere Instanzen ohne
        # echten Host-Mount) statt auf einer echten Appliance - dann gibt
        # es diese Pfade nicht. Kein Fehlerzustand, einfach nichts anzeigen.
        return {"ok": None, "state": "unavailable", "error": None, "days_remaining": None}

    try:
        resolve_result = subprocess.run(
            ["python3", str(resolver_script), "license_file"],
            capture_output=True, text=True, timeout=5,
        )
        license_file = Path(resolve_result.stdout.strip())
    except Exception as exc:
        return {"ok": None, "state": "unavailable", "error": str(exc), "days_remaining": None}

    if not license_file.exists():
        return {"ok": None, "state": "missing", "error": "Noch keine Lizenz-/Testphasendatei vorhanden", "days_remaining": None}

    try:
        result = subprocess.run(
            ["python3", str(check_script), str(license_file)],
            capture_output=True, text=True, timeout=5,
        )
        status = json.loads(result.stdout)
    except Exception as exc:
        return {"ok": None, "state": "unavailable", "error": str(exc), "days_remaining": None}

    state = status.get("state")
    if state == "valid":
        return {
            "ok": True, "state": "valid", "kind": status.get("kind"),
            "days_remaining": status.get("days_remaining"),
            "expires_at": status.get("expires_at"), "error": None,
        }
    if state == "expired":
        return {"ok": False, "state": "expired", "kind": status.get("kind"), "days_remaining": 0, "error": "abgelaufen"}
    # "invalid" (kaputte/manipulierte Datei) - wie expired behandeln, siehe
    # license-check.py-Kommentar (fail closed).
    return {"ok": False, "state": "invalid", "days_remaining": None, "error": status.get("reason", "ungueltig")}


async def infra_watcher():
    while True:
        try:
            license_status = await asyncio.to_thread(_check_license)
            infra_status["license"] = {
                **license_status,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            noalbs_ok, noalbs_err = await asyncio.to_thread(_check_noalbs)
            infra_status["noalbs"] = {
                "ok": noalbs_ok, "error": noalbs_err,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            srtla_ok, srtla_err = await _check_srtla()
            infra_status["srtla"] = {
                "ok": srtla_ok, "error": srtla_err,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            # Kein eigener OBS-Check mehr hier - die (dauerhaft laufende)
            # Test-Session pollt OBS bereits selbst (poll_obs); wir lesen nur
            # deren letzten Stand, statt eine dritte unabhaengige
            # OBS-Verbindung/-Abfrage zusaetzlich zu poll_obs und
            # poll_obs_scene_items aufzumachen (analog zum bestehenden
            # Router-Muster ueber _last_router_snapshot). Fallback auf einen
            # eigenen Check nur, falls (noch) keine Session laeuft.
            obs_event = current_session.last_by_source.get("obs") if current_session else None
            if obs_event is not None:
                obs_data = obs_event.get("data", {})
                obs_ok = bool(obs_data.get("ok"))
                obs_err = obs_data.get("error") if not obs_ok else None
            else:
                obs_ok, obs_err = await asyncio.to_thread(_obs_snapshot_safe)
            infra_status["obs"] = {
                "ok": obs_ok, "error": obs_err,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            # OBS-Notfallknopf-Status (07.09.2026): reine Prozess-Existenz,
            # unabhaengig von obs-websocket-Erreichbarkeit (siehe
            # _obs_process_running()-Docstring oben). Liest den vom Host-
            # Watcher geschriebenen Stand - liefert running=False (nicht
            # None), solange der Watcher noch nicht gelaufen ist oder
            # veraltet ist, das Frontend zeigt das als "rot" (sicherer
            # Default: OBS-Notfallknopf zeigt im Zweifel "aus" statt
            # faelschlich "an").
            infra_status["obs_process"] = {
                "running": _obs_process_running(),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

            # Router sind eigentlich "konfigurierbare Geraete" (siehe unten), aber
            # sobald in der Konfiguration eine IP hinterlegt und gespeichert ist,
            # soll dafuer trotzdem eine dauerhafte Status-Pille erscheinen, genau
            # wie bei der fixen Infrastruktur - deshalb hier mitgeprueft, mit der
            # jeweils aktuell gespeicherten Konfiguration.
            cfg = load_config()
            # Veraltete Router-Eintraege entfernen (z.B. wenn Router 3 im Frontend
            # geloescht wurde), damit /infra-status nicht auf ewig Karteileichen
            # mit alten Daten zurueckgibt.
            current_labels = {f"router{i+1}" for i in range(len(cfg.routers))}
            for stale in [k for k in list(infra_status.keys()) if k.startswith("router") and k not in current_labels]:
                del infra_status[stale]

            for i, profile in enumerate(cfg.routers):
                label = f"router{i+1}"
                if not profile.host:
                    # Noch leerer Router-Slot (z.B. "+ Router hinzufuegen"
                    # geklickt, aber noch keine IP eingetragen) - dafuer soll
                    # keine Pille erscheinen, deshalb den Key ganz weglassen
                    # statt ok=None zu setzen (das wuerde mit "konfiguriert,
                    # aber noch kein Poll-Ergebnis" verwechselt, siehe unten).
                    infra_status.pop(label, None)
                    continue
                # Kein eigener Snapshot mehr hier - die (dauerhaft laufende)
                # Test-Session pollt den Router bereits selbst; wir zeigen nur
                # deren letzten Stand an. Verhindert doppelte GL.iNet-Logins
                # (siehe _get_router_client-Kommentar).
                if label not in _actively_polled_routers:
                    # Ausnahme: dieser Router wird von keinem laufenden
                    # poll_router()-Task abgedeckt (z.B. erst NACH dem Start
                    # der dauerhaften Session ueber ⚙ hinzugefuegt) - ohne
                    # diesen Fallback wuerde er nie gepollt, weil es keinen
                    # Start/Stop-Knopf mehr gibt, der die Poller neu aufsetzt.
                    snapshot = await _router_snapshot_with_timeout(cfg.belabox, profile)
                    _last_router_snapshot[label] = {
                        "ok": snapshot.get("ssh_ok"), "error": snapshot.get("ssh_error"),
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "data": snapshot,
                    }
                cached = _last_router_snapshot.get(label)
                # Direkt nach dem Start, bevor der erste Poll durch ist, bleibt
                # ok bewusst None - der Router-Key selbst bleibt aber gesetzt,
                # damit die Pille nicht faelschlich als "nicht konfiguriert"
                # verschwindet.
                infra_status[label] = cached if cached else {"ok": None, "error": None, "checked_at": None}
        except Exception:
            # Ein einzelner fehlgeschlagener Durchlauf (z.B. eine kurzzeitig
            # unlesbare Config-Datei) soll diesen Hintergrund-Task nicht
            # dauerhaft beenden - vorhandene infra_status-Eintraege bleiben
            # unangetastet, beim naechsten Durchlauf wird es erneut versucht.
            pass

        await asyncio.sleep(POLL_INTERVAL_INFRA)


@app.on_event("startup")
async def start_infra_watcher():
    asyncio.create_task(infra_watcher())
    # Session startet sofort mit dem Container, nicht erst wenn ein Browser
    # das Dashboard oeffnet - siehe Begruendung bei _start_session().
    _start_session()


@app.get("/infra-status")
def get_infra_status():
    return infra_status


# ---------- Connectivity-Ampel (Umbau auf reines Relay-only-Modell) ----------
#
# ZWEI Zustaende: rot (kein verifizierter Relay-Tunnel) und gruen
# (verifizierter Relay-Tunnel). Liest AUSSCHLIESSLICH
# state/relay-provision.json (geschrieben von
# provision/irl-connectivity-report-client.sh) - kein lokaler
# Erreichbarkeits-Check (oeffentliche IP/UPnP/Portforward) mehr, kein
# DynDNS-Hostname-Handling mehr, kein technisches "gruen ohne Relay"
# (direkte oeffentliche Erreichbarkeit) mehr. JEDER Kunde bekommt
# automatisch einen WireGuard-Relay-Tunnel + eine generierte Subdomain
# <slug>.irlstreameros.de - das ist der einzige Zugriffsweg.
#
# Sobald verified=true, werden ALLE noetigen Zugriffs-URLs/-Ports mit
# ausgegeben (Guacamole, Diagnose-Dashboard, BelaUI, OBS-Websocket,
# SRTLA) - jeweils <slug>.irlstreameros.de PLUS eigener oeffentlicher
# Port (KEIN Pfad-Praefix in der extern kommunizierten URL, siehe
# relay-provisioner main.py diagnostic_public_port_for()/
# guacamole_public_port_for() - analoges Muster wie die bereits
# bestehenden srtla_public_port/wg_fernzugriff_public_port/
# obs_websocket_public_port/belabox_webgui_public_port). Intern auf dem
# Mini-PC bleibt Caddy weiterhin pfadbasiert gebuendelt (siehe
# docker/caddy/Caddyfile, alle_dienste_gebuendelt-Block, Port 5002 NUR
# fuer LAN-Zugriff) - das betrifft nur die INTERNE Verkabelung, nicht die
# oeffentlich sichtbare URL-Struktur.
RELAY_PROVISION_FILE = Path("/opt/irl-streamer-os/state/relay-provision.json")

# BUGFIX (07.09.2026, live gefunden): /opt/irl-streamer-os ist im Dashboard-
# Container bewusst read-only gemountet (siehe docker-compose.yml, Haertung
# der Lizenzdatei 05.09.) - toggle_relay_port() konnte relay-provision.json
# deshalb NIE tatsaechlich beschreiben ("Read-only file system"), der
# Schreibversuch dort ist als best-effort mit "except: pass" verpackt und
# scheiterte seither lautlos bei JEDEM Toggle-Klick. Server-seitig (relay-
# provisioner-DB + iptables) wurde der Zustand korrekt geaendert, nur die
# lokale Anzeige blieb für immer auf dem letzten von einem echten
# /provision-Lauf geschriebenen Stand stehen ("Schalter tut sichtbar
# nichts"). Fix: Schalterstand separat in den TATSAECHLICH beschreibbaren
# /app/data-Mount schreiben und beim Lesen ueber die read-only Werte legen.
TOGGLE_OVERRIDE_FILE = Path("/app/data/relay-toggle-overrides.json")


def _load_toggle_overrides() -> dict:
    if not TOGGLE_OVERRIDE_FILE.exists():
        return {}
    try:
        return json.loads(TOGGLE_OVERRIDE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _check_connectivity() -> dict:
    if not RELAY_PROVISION_FILE.exists():
        # Auf einer echten Appliance liegt die Datei nach dem ersten
        # Timer-Lauf immer vor - im Testbed (mehrere Instanzen ohne
        # echten Host-Mount) gibt es sie schlicht nicht, kein
        # Fehlerzustand: Ampel steht dann einfach auf rot.
        return {"status": "red", "last_check_at": None}

    try:
        relay = json.loads(RELAY_PROVISION_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "red", "last_check_at": None, "error": str(exc)}

    # relay-provision.json selbst enthaelt kein "checked_at"-Feld (siehe
    # irl-connectivity-report-client.sh) - die Datei-Aenderungszeit ist
    # hier ein exaktes Aequivalent, da die Datei bei JEDEM Timer-Lauf neu
    # geschrieben wird (auch wenn sich verified nicht aendert).
    try:
        last_check_at = datetime.fromtimestamp(
            RELAY_PROVISION_FILE.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except Exception:
        last_check_at = None

    result = {
        "status": "green" if relay.get("verified") else "red",
        "last_check_at": last_check_at,
        "subdomain": relay.get("subdomain_slug") or None,
    }

    if result["status"] != "green":
        return result

    # Alle Zugriffs-Ports NUR im gruenen Zustand mit ausgeben - jeweils
    # eigener oeffentlicher Port pro Dienst auf derselben Subdomain, siehe
    # Modul-Kommentar oben. Default-Werte (5002 fuer Guacamole/Diagnose,
    # 5000/5001/4455/5003 fuer die anderen Dienste) sind reine Fallbacks
    # fuer Bestandsdaten ohne diese Felder (aeltere relay-provision.json,
    # bevor der jeweilige Port eingefuehrt wurde) - sollten auf einer
    # frischen Installation nie noetig sein.
    result["srtla_public_port"] = relay.get("srtla_public_port")
    result["wg_fernzugriff_public_port"] = relay.get("wg_fernzugriff_public_port")
    result["obs_websocket_public_port"] = relay.get("obs_websocket_public_port")
    result["belabox_webgui_public_port"] = relay.get("belabox_webgui_public_port")
    result["diagnostic_public_port"] = relay.get("diagnostic_public_port")
    result["guacamole_public_port"] = relay.get("guacamole_public_port")
    # Ein/Aus-Schalter-Status pro Dienst (Nutzerwunsch 05.09.): Default
    # True fuer Bestandsdaten ohne diese Felder (aeltere
    # relay-provision.json-Dateien, die vor diesem Feature geschrieben
    # wurden - "an" entspricht dem bisherigen, einzigen Verhalten).
    # Lokale Overrides (siehe TOGGLE_OVERRIDE_FILE oben) haben Vorrang vor
    # dem read-only relay-provision.json-Wert, da genau dorthin der letzte
    # tatsaechliche Klick-Zustand geschrieben wird.
    overrides = _load_toggle_overrides()
    result["srtla_enabled"] = overrides.get("srtla_enabled", relay.get("srtla_enabled", True))
    result["wg_fernzugriff_enabled"] = overrides.get("wg_fernzugriff_enabled", relay.get("wg_fernzugriff_enabled", True))
    result["obs_websocket_enabled"] = overrides.get("obs_websocket_enabled", relay.get("obs_websocket_enabled", True))
    result["belabox_webgui_enabled"] = overrides.get("belabox_webgui_enabled", relay.get("belabox_webgui_enabled", True))

    return result


class TogglePortRequest(BaseModel):
    service: str
    enabled: bool


@app.post("/toggle-relay-port")
def toggle_relay_port(req: TogglePortRequest):
    """Schaltet einen der 4 optionalen Relay-Ports (SRTLA, WireGuard-
    Fernzugriff, OBS-Websocket, Belabox-WebGUI) an oder aus (Nutzerwunsch
    05.09.: "da potenziell ja jeder offene Port ein Sicherheitsrisiko ist"
    - der Kunde soll ueber diese Konfigurationsseite selbst entscheiden
    koennen, welche Dienste von aussen per Relay erreichbar sein sollen).
    Reicht die Anfrage 1:1 an den relay-provisioner weiter (/toggle-port
    dort macht die eigentliche iptables-Arbeit) - dieses Dashboard kennt
    selbst keine Firewall-Details, nur die Relay-Zugangsdaten aus
    relay-provision.json (device_fingerprint wird seit 05.09. dort
    mitgespeichert, siehe irl-connectivity-report-client.sh).

    WICHTIG: greift ausschliesslich am Netcup-Relay-Server - es gibt
    seit dem Umbau auf das reine Relay-only-Modell keinen anderen
    Zugriffsweg mehr, ueber den diese Dienste erreichbar waeren."""
    valid_services = {"srtla", "wg_fernzugriff", "obs_websocket", "belabox_webgui"}
    if req.service not in valid_services:
        return JSONResponse({"error": f"Unbekannter Dienst: {req.service}"}, status_code=400)

    if not RELAY_PROVISION_FILE.exists():
        return JSONResponse({"error": "Kein Relay-Tunnel eingerichtet"}, status_code=409)

    try:
        relay = json.loads(RELAY_PROVISION_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return JSONResponse({"error": f"relay-provision.json konnte nicht gelesen werden: {exc}"}, status_code=500)

    fingerprint = relay.get("device_fingerprint")
    if not fingerprint:
        # Aeltere Installation, deren relay-provision.json noch VOR dem
        # Nachtrag (05.09.) geschrieben wurde - der naechste stuendliche
        # Timer-Lauf ergaenzt das Feld automatisch, siehe
        # irl-connectivity-report-client.sh.
        return JSONResponse(
            {"error": "device_fingerprint fehlt noch in relay-provision.json - bitte einmal den naechsten stuendlichen Connectivity-Check abwarten oder das Dashboard neu laden, nachdem der Timer erneut gelaufen ist."},
            status_code=409,
        )

    try:
        resp = httpx.post(
            f"{RELAY_PROVISIONER_URL}/toggle-port",
            json={"device_fingerprint": fingerprint, "service": req.service, "enabled": req.enabled},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPStatusError as exc:
        return JSONResponse({"error": f"relay-provisioner lehnte ab: {exc.response.text}"}, status_code=exc.response.status_code)
    except Exception as exc:
        return JSONResponse({"error": f"relay-provisioner nicht erreichbar: {exc}"}, status_code=502)

    # Lokalen Anzeige-Cache aktualisieren, damit die Ampel-Karte den neuen
    # Status SOFORT zeigt, ohne auf den naechsten stuendlichen Timer-Lauf
    # warten zu muessen. BUGFIX (07.09.2026): NICHT mehr in
    # RELAY_PROVISION_FILE schreiben - dieser Mount ist im Dashboard-
    # Container read-only (siehe TOGGLE_OVERRIDE_FILE-Kommentar oben),
    # jeder Schreibversuch dorthin scheiterte bisher lautlos und der
    # Schalter zeigte nie den echten neuen Zustand an. Stattdessen in die
    # separate, tatsaechlich beschreibbare Override-Datei schreiben.
    try:
        overrides = _load_toggle_overrides()
        overrides[f"{req.service}_enabled"] = req.enabled
        TOGGLE_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOGGLE_OVERRIDE_FILE.write_text(json.dumps(overrides), encoding="utf-8")
    except Exception:
        pass  # Anzeige-Cache-Update ist best-effort, der Server-Zustand ist bereits korrekt gesetzt

    return result


def _auto_srtla_target_overrides() -> dict:
    """Ermittelt automatisch das SRTLA-Ziel (Adresse/Port/StreamID) je nach
    aktuellem Ampel-Status. belaUI kann die Zieladresse nur BEIM
    Streamstart selbst setzen (kein reines Vorbereiten ohne zu starten,
    siehe updateConfig()/startStream() in belaUI.js) - deshalb wird hier
    bewusst NUR beim tatsaechlichen Klick auf "Stream starten" ermittelt
    und mitgeschickt, nie waehrend eines bereits laufenden Streams.

    Bei Rot: KEINE Overrides moeglich (kein Relay-Tunnel vorhanden).
    Bei Gruen (verifizierter Relay-Tunnel): automatisch die Relay-
    Subdomain + individueller SRTLA-Relay-Port aus relay-provision.json
    (siehe docs/2026-09-03_SRTLA-Relay-Loesung.md) - identische
    StreamID wie bisher (die aendert sich nicht, nur das Transportziel).
    """
    status = _check_connectivity()
    if status.get("status") != "green":
        return {}

    slug = status.get("subdomain")
    srtla_port = status.get("srtla_public_port")
    if not slug or not srtla_port:
        # Verifiziert, aber (noch) kein Relay-Port bekannt (z.B. sehr
        # altes Geraet vor Einfuehrung dieses Features) - lieber gar
        # keine Overrides als eine kaputte halbe Adresse zu senden.
        return {}

    return {
        "srtla_addr": f"{slug}.irlstreameros.de",
        "srtla_port": srtla_port,
    }


@app.get("/connectivity")
def get_connectivity():
    return _check_connectivity()


# ---------- Konfigurierbare Geraete (per Frontend, jedes Mal neu waehlbar) ----------


# Router-Herstellerauswahl im Frontend ("Router hinzufuegen") - bestimmt,
# welche Vendor-API _router_snapshot() versucht. "other" bekommt bewusst NUR
# die vendor-unabhaengige Netzwerkqualitaet (WLAN/LAN, siehe
# _belabox_network_quality), keine Login-/Telemetrieversuche.
ROUTER_VENDORS = {"glinet", "netgear", "tplink", "other"}


class DeviceProfile(BaseModel):
    label: str = ""
    # Bei Routern: NUR NOCH die lokale IP/den Hostnamen des Routers im Netz
    # der Belabox (z.B. 192.168.1.1) - NICHT mehr vom Dashboard aus direkt
    # erreichbar/angesprochen (Nutzerentscheidung 2026-08-31: alle
    # Router-Abfragen laufen ueber die bereits bestehende Belabox-SSH-
    # Verbindung durch den WireGuard-Tunnel, siehe _belabox_tcp_proxy).
    # Bei "belabox" selbst: weiterhin die eigene Tunnel-/SSH-Adresse.
    host: str = ""
    # Bei Routern: Nutzer/Passwort der jeweiligen Router-WEBOBERFLAECHE
    # (GL.iNet-RPC-Login, Netgear-Session, TP-Link-Login) - NICHT SSH-Zugang
    # zum Router selbst (Router werden nie per SSH angesprochen). Bei
    # "belabox": weiterhin echte SSH-Zugangsdaten fuer die Belabox.
    ssh_user: str = ""
    ssh_password: str = ""
    # Router-Hersteller ("glinet"/"netgear"/"tplink"/"other") - steuert in
    # _router_snapshot(), welcher Vendor-Pfad versucht wird. Default "glinet"
    # fuer Abwaertskompatibilitaet mit bestehenden Konfigurationen (Router 1
    # war bisher implizit immer GL.iNet).
    vendor: str = "glinet"
    # Optional: feste Belabox-Netzwerkschnittstelle (z.B. "wlan1") fuer die
    # Netzwerkqualitaets-Abfrage (siehe _belabox_network_quality) - normalerweise
    # automatisch anhand des /24-Subnetzes von "host" erkannt, hier nur als
    # manueller Override, falls die Erkennung fehlschlaegt (z.B. mehrere
    # Interfaces im selben Subnetz).
    belabox_iface: str = ""
    # Nur fuer GL.iNet-Router relevant: JSON-RPC-Methode + Parameter fuer
    # Signal-/Modem-Stats. Muss einmalig live am eingeschalteten Router ueber
    # /router/{which}/discover ermittelt werden, siehe README-Hinweis im Code.
    api_method: str = ""
    api_params: str = "[]"
    # Nur fuer den Belabox-Encoder relevant: Login-Passwort der belaUI-Weboberflaeche
    # (NICHT das SSH-Passwort) - noetig, um Stream Start/Stop per WebSocket
    # fernzusteuern, siehe _belabox_ws_command().
    ui_password: str = ""


class DjiDeviceProfile(BaseModel):
    # Nur Einstellungen, keine Zugangsdaten fuers Backend: die eigentliche
    # Kamerasteuerung laeuft komplett im Browser per Web Bluetooth (siehe
    # static/dji-ble-test.html), das Backend speichert diese Werte nur, damit
    # sie nicht bei jedem Stream erneut eingetippt werden muessen.
    model: str = "osmoAction4"
    wifi_ssid: str = ""
    wifi_password: str = ""
    rtmp_url: str = ""
    resolution: str = "1080p"
    fps: int = 30
    bitrate: int = 6000
    stabilization: str = "RockSteadyPlus"
    pin_code: str = "love"


class NoalbsConfig(BaseModel):
    # Vom Nutzer ausgewaehlte Teilmenge der NOALBS-config.json (Rest siehe
    # config.json.template) - Schwellenwerte (triggers) haben bereits eine
    # eigene UI (/noalbs/thresholds), Zugangsdaten/OBS-Verbindung/streamServers
    # bleiben bewusst technisch/automatisch verwaltet. Defaults spiegeln
    # config.json.template. ACHTUNG: "offline_timeout" hier ist
    # switcher.offlineTimeout - es gibt zusaetzlich ein GLEICHNAMIGES, separates
    # optionalOptions.offlineTimeout (Default null, ungenutzt/unklarer Zweck),
    # das hier bewusst NICHT angefasst wird.
    bitrate_switcher_enabled: bool = True
    only_switch_when_streaming: bool = False
    instantly_switch_on_recover: bool = False
    auto_switch_notification: bool = False
    reconnect_delay: int = 3000
    scene_switch_delay: int = 3000
    offline_timeout: int = 15000
    scene_normal: str = "LIVE"
    scene_low: str = "LOW"
    scene_offline: str = "BRB"
    chat_username: str = ""
    chat_prefix: str = "!"
    chat_enable_public_commands: bool = False
    chat_enable_auto_stop_on_host_or_raid: bool = False
    chat_admins: list[str] = []
    record_while_streaming: bool = False


class DeviceConfig(BaseModel):
    belabox: DeviceProfile = DeviceProfile()
    # Router 1 ist immer vorhanden (mindestens ein Eintrag), weitere sind
    # frei hinzufuegbar/entfernbar - flexibel fuer 1 bis N Router im Setup.
    routers: list[DeviceProfile] = [DeviceProfile()]
    dji: DjiDeviceProfile = DjiDeviceProfile()
    # Name der OBS-Quelle (Media Source), die der "Fix"-Knopf und der
    # automatische Watchdog bei SRTLA-Ausfaellen neu laden (siehe
    # _auto_reload_source()/_restart_media_source()). Default seit
    # 2026-08-31 "Belabox Stream" - so heisst die Quelle in der
    # mitgelieferten provision/obs-scenes.json (LIVE/LOW-Szenen), passt
    # also automatisch out-of-the-box zusammen. War frueher fest ueber die
    # Umgebungsvariable AUTO_RELOAD_OBS_SOURCE auf "Stream Vito" verdrahtet
    # (anderer, aelterer Rollout mit abweichendem Quellennamen) - Default
    # bleibt weiterhin per Env-Var ueberschreibbar, falls ein Rollout einen
    # eigenen Namen braucht.
    obs_media_source: str = os.environ.get("AUTO_RELOAD_OBS_SOURCE", "Belabox Stream")


_config_cache: Optional[DeviceConfig] = None
_config_cache_lock = threading.Lock()


def load_config() -> DeviceConfig:
    # In-Memory-Cache statt bei jedem Aufruf (u.a. infra_watcher() alle 5s,
    # dauerhaft) die Datei neu zu lesen und zu validieren - die Config aendert
    # sich praktisch nie, nur wenn save_config() sie explizit invalidiert.
    # WICHTIG: das zurueckgegebene Objekt wird von allen Aufrufern nur
    # GELESEN, nie mutiert - sonst wuerde eine Mutation unbeabsichtigt in den
    # Cache durchschlagen.
    global _config_cache
    with _config_cache_lock:
        if _config_cache is not None:
            return _config_cache
        if CONFIG_FILE.exists():
            cfg = DeviceConfig.model_validate_json(CONFIG_FILE.read_text())
        else:
            cfg = DeviceConfig()
        if not cfg.routers:
            cfg.routers = [DeviceProfile()]
        # Nur auf der Appliance (local_docker) ist die Belabox-Host fest die
        # WireGuard-Tunnel-Adresse - im Produktiv-Setup (ssh_vm, separate
        # NOALBS-VM) bleibt sie weiterhin frei im Frontend eintragbar, siehe
        # BELABOX_HOST oben und Nutzerentscheidung 2026-08-25.
        if NOALBS_MODE == "local_docker":
            cfg.belabox.host = BELABOX_HOST
        _config_cache = cfg
        return cfg


def save_config(cfg: DeviceConfig) -> None:
    # Atomar schreiben (Temp-Datei + os.replace): ein gleichzeitiger
    # load_config()-Aufruf aus infra_watcher() konnte sonst mitten in einem
    # Schreibvorgang eine abgeschnittene/kaputte JSON-Datei lesen - dadurch
    # fiel load_config() (indirekt) auf zu wenige Router zurueck und
    # infra_watcher() hat den vermeintlich "entfernten" Router kurzzeitig aus
    # infra_status geloescht -> er verschwand aus der Uebersicht.
    global _config_cache
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(cfg.model_dump_json(indent=2))
    tmp.replace(CONFIG_FILE)
    with _config_cache_lock:
        _config_cache = cfg


@app.get("/config")
def get_config():
    cfg = load_config()
    # belabox_host_locked ist kein persistiertes Feld, nur ein Hinweis fuers
    # Frontend, ob das Host/IP-Feld dort editierbar sein soll (Appliance)
    # oder nicht (Produktiv-Setup, siehe load_config/set_config oben).
    return {**cfg.model_dump(), "belabox_host_locked": NOALBS_MODE == "local_docker"}


@app.post("/config")
async def set_config(cfg: DeviceConfig):
    # Nur auf der Appliance (local_docker) ist die Belabox-Host fest die
    # WireGuard-Tunnel-Adresse, nicht vom Frontend eintragbar - siehe
    # BELABOX_HOST oben und Nutzerentscheidung 2026-08-25. Im Produktiv-
    # Setup (ssh_vm) bleibt gespeichert, was das Frontend schickt.
    if NOALBS_MODE == "local_docker":
        cfg.belabox.host = BELABOX_HOST
    save_config(cfg)
    # Die laufenden Poll-Tasks (poll_belabox_live, poll_router, ...) wurden
    # beim Container-/Session-Start einmalig mit den DAMALIGEN Profildaten
    # erzeugt (siehe _start_session()) und lesen spaeter geaenderte Werte nie
    # erneut ein - live gefunden (2026-08-25): frisch gespeicherte Belabox-/
    # Router-Zugangsdaten blieben wirkungslos (Belabox-Karte blieb leer),
    # bis jemand den Container von Hand neu gestartet hat. Denselben bereits
    # vorhandenen, sicheren Selbst-Neustart wie "/system/restart" ausloesen,
    # damit neue Zugangsdaten sofort ohne manuellen Docker-Eingriff greifen.
    async def _delayed_exit():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_delayed_exit())
    return {"ok": True}


# ---------- Session-Verwaltung ----------


FINDING_COOLDOWN = 300  # Sekunden, bevor dieselbe Finding-Art erneut gemeldet wird
RECENT_WINDOW = 30  # Sekunden Rueckblick fuer die Marker-Analyse
DEBOUNCE_CONSECUTIVE = 3  # so viele aufeinanderfolgende gleiche Polls, bevor ein Zustandswechsel gemeldet wird
ROUTER_UNREACHABLE_HOLD = 30  # Sekunden ununterbrochen kein Ping-Erfolg, bevor "nicht erreichbar" gemeldet wird

# Uebersetzt rohe Exception-/Netzwerkfehlertexte in verstaendliche deutsche
# Saetze, damit im Frontend keine kryptischen "[Errno 111]"-Meldungen ohne
# Kontext auftauchen.
_ERROR_TRANSLATIONS = [
    ("Connection refused", "Das Ziel nimmt aktuell keine Verbindungen an (Dienst laeuft vermutlich nicht)."),
    ("No route to host", "Kein Netzwerkpfad zum Geraet (falsches Subnetz, VPN nicht verbunden, oder Geraet aus)."),
    ("Network is unreachable", "Netzwerk nicht erreichbar (falsche IP/Subnetz oder Routing-Problem)."),
    ("timed out", "Zeitueberschreitung - Geraet antwortet nicht (falsche IP, Geraet aus, oder Firewall blockiert)."),
    ("Authentication failed", "SSH-Anmeldung fehlgeschlagen - Nutzername/Passwort in der Konfiguration pruefen."),
    ("invalid access token", "Zugangs-Token ist ungueltig/abgelaufen."),
    ("Name or service not known", "Hostname/IP konnte nicht aufgeloest werden - Adresse in der Konfiguration pruefen."),
]


def _friendly_error(raw: Optional[str]) -> str:
    if not raw:
        return "unbekannter Fehler"
    for needle, translation in _ERROR_TRANSLATIONS:
        if needle.lower() in raw.lower():
            return f"{translation} (Rohmeldung: {raw})"
    return raw


class Session:
    def __init__(self, cfg: DeviceConfig):
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.cfg = cfg
        self.queue: asyncio.Queue = asyncio.Queue()
        self.clients: set[WebSocket] = set()
        self.tasks: list[asyncio.Task] = []
        self.log_path = SESSIONS_DIR / f"{self.id}.jsonl"
        self.log_file = open(self.log_path, "a", buffering=1, encoding="utf-8")
        self.stopped = asyncio.Event()
        # Analyzer-Zustand: rollierender Puffer der letzten RECENT_WINDOW
        # Sekunden je Quelle (fuer die Marker-Analyse) + Zustandsuebergaenge
        # (verbunden<->getrennt etc.), damit Findings nur bei einer Aenderung
        # gemeldet werden statt bei jedem einzelnen Poll erneut.
        self.recent: dict[str, list] = {}
        self._state: dict = {
            "noalbs_obs_connected": None,
            "obs_connected": None,
            "obs_prev_skipped": None,
            "last_finding_ts": {},
            "srtla_outage": {},
        }
        # Damit ein neu verbindender Browser sofort den aktuellen Stand sieht
        # statt erst auf das naechste Ereignis warten zu muessen - relevant
        # seit die Session dauerhaft mit dem Container laeuft (nicht mehr erst
        # beim Oeffnen des Dashboards startet): ein Browser, der Minuten nach
        # Sessionstart verbindet, saehe sonst leere Karten (v.a. bei NOALBS,
        # das nur bei tatsaechlichen Ereignissen wie Szenenwechseln loggt statt
        # laufend wie SRTLA/OBS/Router), bis zufaellig das naechste Ereignis kommt.
        self.last_by_source: dict[str, dict] = {}
        self.active_findings: dict[str, dict] = {}

    def emit(self, source: str, data: dict):
        event = {"ts": datetime.now(timezone.utc).isoformat(), "source": source, "data": data}
        self.queue.put_nowait(event)
        self._remember(source, data)
        if source == "finding":
            resolves = data.get("resolves")
            if resolves:
                self.active_findings.pop(resolves, None)
            elif data.get("key"):
                self.active_findings[data["key"]] = event
        elif source != "marker":
            self.last_by_source[source] = event
        if source == "marker":
            self._analyze_marker(data)
        elif source != "finding":
            self._analyze_realtime(source, data)

    def _remember(self, source: str, data: dict):
        buf = self.recent.setdefault(source, [])
        buf.append((time.monotonic(), data))
        cutoff = time.monotonic() - RECENT_WINDOW
        while buf and buf[0][0] < cutoff:
            buf.pop(0)

    def _should_fire(self, key: str) -> bool:
        last = self._state["last_finding_ts"].get(key, 0)
        if time.monotonic() - last < FINDING_COOLDOWN:
            return False
        self._state["last_finding_ts"][key] = time.monotonic()
        return True

    def finding(self, key: str, severity: str, title: str, detail: str, recommendation: str,
                resolves: Optional[str] = None, bypass_cooldown: bool = False):
        # "resolves" markiert diese Meldung als Aufloesung eines vorher
        # gemeldeten Problems (gleicher key dort). Solche Aufloesungen muessen
        # zuverlaessig durchkommen, damit das Findings-Panel im Frontend die
        # zugehoerige alte Meldung wirklich entfernt - deshalb ohne Cooldown.
        # bypass_cooldown gilt fuer die "Problem"-Seite eines debounce- oder
        # zustandsbasierten Paares: die eigentliche Drosselung passiert dort
        # schon durch das Debounce/den Zustandswechsel selbst - der zusaetzliche
        # 300s-Cooldown wuerde ein zweites, echtes Auftreten desselben Problems
        # (z.B. Router faellt zweimal in 5 Minuten aus) faelschlich verschlucken.
        if not resolves and not bypass_cooldown and not self._should_fire(key):
            return
        self.emit("finding", {
            "key": key, "severity": severity, "title": title,
            "detail": detail, "recommendation": recommendation, "resolves": resolves,
        })

    def _debounce(self, counter_key: str, is_bad: bool) -> Optional[bool]:
        """Meldet einen Zustandswechsel erst, nachdem er sich DEBOUNCE_CONSECUTIVE
        aufeinanderfolgende Polls lang bestaetigt hat - eine einzelne kurze
        Signalschwankung oder ein einzelner verpasster Poll loest dadurch noch
        kein Finding aus. Rueckgabe: True = Problem beginnt jetzt, False =
        Problem endet jetzt (nur falls vorher tatsaechlich gemeldet), None =
        keine meldenswerte Aenderung."""
        counters = self._state.setdefault("debounce_counters", {})
        states = self._state.setdefault("debounce_states", {})
        reported = states.get(counter_key, False)
        if is_bad == reported:
            counters[counter_key] = 0
            return None
        count = counters.get(counter_key, 0) + 1
        counters[counter_key] = count
        if count >= DEBOUNCE_CONSECUTIVE:
            states[counter_key] = is_bad
            counters[counter_key] = 0
            return is_bad
        return None

    def _time_debounce(self, key: str, is_bad: bool, hold_seconds: float) -> Optional[bool]:
        """Wie _debounce, aber zeitbasiert statt pollzahlbasiert: ein Problem
        wird erst gemeldet, nachdem es hold_seconds lang ununterbrochen
        bestanden hat (z.B. 30s durchgehend nicht per Ping erreichbar).
        Rueckgabe wie _debounce: True = Problem beginnt jetzt, False = Problem
        endet jetzt, None = keine meldenswerte Aenderung."""
        since = self._state.setdefault("bad_since", {})
        states = self._state.setdefault("debounce_states", {})
        reported = states.get(key, False)
        if not is_bad:
            since.pop(key, None)
            if reported:
                states[key] = False
                return False
            return None
        start = since.setdefault(key, time.monotonic())
        if not reported and time.monotonic() - start >= hold_seconds:
            states[key] = True
            return True
        return None

    def _analyze_realtime(self, source: str, data: dict):
        if source == "noalbs":
            line = data.get("line", "")
            if "Waiting for OBS connection" in line:
                if self._state["noalbs_obs_connected"] is not False:
                    self._state["noalbs_obs_connected"] = False
                    self.finding(
                        "noalbs_obs_disconnected", "warning",
                        "NOALBS hat die Verbindung zu OBS verloren",
                        "Solange diese Verbindung fehlt, kann NOALBS nicht automatisch die Szene wechseln, "
                        "wenn sich Bitrate/RTT verschlechtern.",
                        "Pruefen, ob OBS laeuft und der WebSocket-Server aktiv ist "
                        "(Werkzeuge -> WebSocket-Server-Einstellungen in OBS).",
                        bypass_cooldown=True,
                    )
            elif "obs_v5: Connected" in line or "Switcher running" in line:
                if self._state["noalbs_obs_connected"] is not True:
                    self._state["noalbs_obs_connected"] = True
                    self.finding(
                        "noalbs_obs_connected", "info",
                        "NOALBS ist (wieder) mit OBS verbunden",
                        "Die automatische Szenen-Umschaltung ist aktiv.",
                        "Keine Aktion noetig.", resolves="noalbs_obs_disconnected",
                    )
            elif "Twitch authentication failed" in line:
                self.finding(
                    "noalbs_twitch_auth", "info",
                    "NOALBS: Twitch-Chat-Authentifizierung schlaegt fehl",
                    "Der Twitch-Chatbot (!fix, !switch, ...) funktioniert dadurch vermutlich nicht - "
                    "unabhaengig vom Ton-Problem.",
                    "OAuth-Token in der NOALBS-Konfiguration erneuern (TWITCH_BOT_OAUTH ist vermutlich abgelaufen).",
                )

        elif source == "obs":
            ok = data.get("ok")
            if ok is False:
                if self._state["obs_connected"] is True:
                    self.finding(
                        "obs_disconnected", "critical",
                        "OBS-Verbindung waehrend des Tests verloren",
                        _friendly_error(data.get('error')),
                        "Pruefen, ob OBS noch laeuft und der WebSocket-Server erreichbar ist.",
                        bypass_cooldown=True,
                    )
                self._state["obs_connected"] = False
            elif ok is True:
                if self._state["obs_connected"] is False:
                    self.finding(
                        "obs_reconnected", "info",
                        "OBS-Verbindung wiederhergestellt", "", "Keine Aktion noetig.",
                        resolves="obs_disconnected",
                    )
                self._state["obs_connected"] = True
                skipped = data.get("output_skipped_frames")
                prev_skipped = self._state["obs_prev_skipped"]
                if isinstance(skipped, (int, float)) and isinstance(prev_skipped, (int, float)):
                    delta = skipped - prev_skipped
                    if delta > 5:
                        self.finding(
                            "obs_skipped_frames", "warning",
                            "OBS verwirft ploetzlich Frames",
                            f"{delta} zusaetzliche uebersprungene Frames seit der letzten Messung "
                            f"(insgesamt {skipped}).",
                            "Deutet auf einen kurzzeitigen Engpass zwischen Encoder und OBS hin - "
                            "mit den SRTLA-Werten zum gleichen Zeitpunkt abgleichen.",
                        )
                self._state["obs_prev_skipped"] = skipped
                congestion = data.get("output_congestion")
                if congestion:
                    self.finding(
                        "obs_congestion", "warning",
                        "OBS meldet Ausgabe-Kongestion",
                        f"output_congestion = {congestion}",
                        "Typisches Zeichen fuer einen Bandbreiteneinbruch auf der eingehenden Verbindung - "
                        "das koennte die Ton-Verzerrung verursachen.",
                    )

        elif re.fullmatch(r"router\d+", source):
            # Erreichbarkeit wird bewusst per TCP-"Ping" (reine Netzwerkebene,
            # siehe _tcp_ping) statt per SSH/API-Erfolg beurteilt - SSH kann
            # aus Gruenden fehlschlagen, die nichts mit "Router ist weg" zu tun
            # haben (z.B. Login haengt kurz). Erst nach ROUTER_UNREACHABLE_HOLD
            # Sekunden UNUNTERBROCHENER Nichterreichbarkeit wird gemeldet.
            ping_ok = data.get("ping_ok")
            if ping_ok is not None:
                transition = self._time_debounce(f"{source}_ping", not ping_ok, ROUTER_UNREACHABLE_HOLD)
                if transition is True:
                    self.finding(
                        f"{source}_unreachable", "warning",
                        f"{source}: seit {ROUTER_UNREACHABLE_HOLD}s nicht mehr erreichbar (Ping)",
                        _friendly_error(data.get("ssh_error")),
                        "Moeglicher Hinweis auf Signalverlust oder einen Neustart dieses gebondeten Links.",
                        bypass_cooldown=True,
                    )
                elif transition is False:
                    self.finding(
                        f"{source}_reachable", "info", f"{source}: wieder erreichbar", "", "Keine Aktion noetig.",
                        resolves=f"{source}_unreachable",
                    )

            # api_ok wird nur gesetzt, wenn fuer diesen Router ueberhaupt eine
            # Vendor-API versucht wird (glinet/netgear/tplink) - bei
            # vendor=="other" bleibt es None, dort gibt es bewusst keine
            # API-Erreichbarkeits-Meldung (siehe ROUTER_VENDORS-Kommentar).
            api_ok = data.get("api_ok")
            if api_ok is not None:
                transition = self._debounce(f"{source}_api", not api_ok)
                if transition is True:
                    self.finding(
                        f"{source}_api_unreachable", "warning",
                        f"{source}: Router-API nicht erreichbar (Signal-/Systemwerte fehlen)",
                        _friendly_error(data.get("api_error")),
                        "Die Belabox erreicht den Router zwar (TCP-Ping), aber dessen Web-API antwortet "
                        "nicht wie erwartet - Nutzername/Passwort der Router-Weboberflaeche pruefen, "
                        "sowie ob der eingestellte Hersteller (GL.iNet/Netgear/TP-Link) tatsaechlich stimmt.",
                        bypass_cooldown=True,
                    )
                elif transition is False:
                    self.finding(
                        f"{source}_api_reachable", "info", f"{source}: Router-API wieder erreichbar", "",
                        "Keine Aktion noetig.", resolves=f"{source}_api_unreachable",
                    )

            cpu_temp = data.get("cpu_temp")
            if isinstance(cpu_temp, (int, float)):
                transition = self._debounce(f"{source}_hot", cpu_temp >= 70)
                if transition is True:
                    self.finding(
                        f"{source}_hot", "warning",
                        f"{source}: hohe CPU-Temperatur ({cpu_temp}°C)",
                        "Im Rucksack/geschlossenen Fahrzeugfach kann Waermestau zu Drosselung oder "
                        "Instabilitaet des Routers fuehren.",
                        "Kuehlung/Belueftung pruefen, falls das waehrend des Streams weiter ansteigt.",
                        bypass_cooldown=True,
                    )
                elif transition is False:
                    self.finding(
                        f"{source}_cool", "info", f"{source}: Temperatur wieder normal ({cpu_temp}°C)", "",
                        "Keine Aktion noetig.", resolves=f"{source}_hot",
                    )

            # Schwaches Signal wird bewusst NICHT mehr als Finding gemeldet -
            # das kommt auf Cellular-Links "hin und wieder" ganz normal vor und
            # sollte kein Alarm sein. Die RSRP/RSRQ/SINR-Werte bleiben weiterhin
            # sichtbar in der Router-Karte (renderRouterSummary im Frontend),
            # nur eben nicht mehr als eigenstaendige Fehlermeldung.

        elif source == "belabox":
            ok = data.get("ok")
            if ok is not None:
                transition = self._debounce("belabox_ssh", not ok)
                if transition is True:
                    self.finding(
                        "belabox_unreachable", "warning",
                        "Belabox-Encoder nicht per SSH erreichbar",
                        _friendly_error(data.get("error")),
                        "Pruefen: laeuft der Belabox-Mini-PC, ist die IP in der Konfiguration korrekt, "
                        "ist SSH auf dem Geraet aktiv?",
                        bypass_cooldown=True,
                    )
                elif transition is False:
                    self.finding(
                        "belabox_reachable", "info", "Belabox-Encoder wieder erreichbar", "",
                        "Keine Aktion noetig.", resolves="belabox_unreachable",
                    )

            bonded = data.get("bonded_link_count")
            ifaces = data.get("interfaces") or []
            if ok and isinstance(bonded, int) and len(ifaces) > 0:
                transition = self._debounce("belabox_single_link", bonded < len(ifaces))
                if transition is True:
                    self.finding(
                        "belabox_single_link", "critical" if bonded <= 1 else "warning",
                        f"Nur {bonded} von {len(ifaces)} Netzwerk-Interfaces im SRTLA-Bonding-Pool",
                        f"/tmp/srtla_ips auf dem Encoder enthaelt nur {bonded} IP(s), obwohl "
                        f"{len(ifaces)} Interfaces aktiv sind ({', '.join(i['name'] for i in ifaces)}). "
                        "Ohne mehrere gebondete Links gibt es keine Redundanz - faellt der eine genutzte "
                        "Link kurz aus (z.B. schwaches Signal), reisst die ganze Verbindung ab statt nur "
                        "die Bandbreite zu reduzieren. Genau das erklaert den Totalausfall im ersten Livetest.",
                        "In belaUI unter 'Network' pruefen, ob weitere Interfaces (z.B. der zweite Router) "
                        "fuers Bonding aktiviert werden koennen/sollten.",
                        bypass_cooldown=True,
                    )
                elif transition is False:
                    self.finding(
                        "belabox_all_links_bonded", "info",
                        f"Alle {len(ifaces)} Netzwerk-Interfaces jetzt im SRTLA-Bonding-Pool", "",
                        "Keine Aktion noetig.", resolves="belabox_single_link",
                    )

        elif source == "srtla":
            stats = data.get("stats") or {}
            publishers = stats.get("publishers") if isinstance(stats, dict) else None
            if data.get("ok") and self._state.get("obs_connected") and publishers is not None:
                transition = self._debounce("srtla_publisher", not publishers)
                if transition is True:
                    self.finding(
                        "srtla_no_publisher", "warning",
                        "SRTLA-Relay zeigt keinen aktiven Publisher",
                        "OBS ist verbunden, aber der SRTLA-Server sieht aktuell keine eingehende Verbindung vom Encoder.",
                        "Belabox-Encoder pruefen: laeuft die Uebertragung, ist SRTLA aktiv verbunden?",
                        bypass_cooldown=True,
                    )
                elif transition is False:
                    self.finding(
                        "srtla_publisher_back", "info", "SRTLA-Relay sieht wieder einen aktiven Publisher", "",
                        "Keine Aktion noetig.", resolves="srtla_no_publisher",
                    )

            if isinstance(publishers, dict):
                for path, p in publishers.items():
                    drop = p.get("pktRcvDrop")
                    if not isinstance(drop, (int, float)):
                        continue
                    outages = self._state["srtla_outage"]
                    st = outages.setdefault(path, {"prev_drop": None, "active": False, "stable_polls": 0})
                    prev = st["prev_drop"]
                    if prev is not None:
                        delta = drop - prev
                        if delta > SRTLA_DROP_SPIKE_THRESHOLD:
                            if not st["active"]:
                                st["active"] = True
                                # bewusst ohne Cooldown emittiert (self.emit statt self.finding) -
                                # jeder einzelne Ausfall soll sichtbar sein, nicht nur der erste
                                # innerhalb von FINDING_COOLDOWN.
                                self.emit("finding", {
                                    "key": f"srtla_outage_{path}", "severity": "critical",
                                    "title": f"Schwerer Netzwerk-Ausfall auf {path}",
                                    "detail": f"{delta} Pakete in ~1s unwiderruflich verloren "
                                              f"(RTT {p.get('rtt')}ms, Puffer {p.get('msRcvBuf')}ms) - "
                                              "typischer Ausloeser fuer den 'Kassette verlangsamt'-Audioeffekt.",
                                    "recommendation": f"Warte auf Erholung, dann automatischer Neustart "
                                                       f"von '{self.cfg.obs_media_source}' in OBS.",
                                })
                            st["stable_polls"] = 0
                        elif st["active"]:
                            st["stable_polls"] += 1
                            if st["stable_polls"] >= SRTLA_RECOVERY_STABLE_POLLS:
                                st["active"] = False
                                st["stable_polls"] = 0
                                self._auto_reload_source(path)
                    st["prev_drop"] = drop

        elif source == "obs_sources":
            # Die fruehere Warnung "Media-Source mit hohem Puffer" wurde
            # entfernt (Nutzerwunsch, 2026-08-20): der Reconnector
            # (_auto_reload_source/_restart_media_source) faengt das
            # zugrundeliegende Audio-Drift-Problem jetzt automatisch ab -
            # die Meldung war dadurch nur noch irrefuehrender Dauer-Hinweis
            # ohne Handlungsbedarf, kein echter Fehlerzustand mehr.
            for src_info in data.get("sources", []):
                url = src_info.get("url", "")
                if src_info.get("kind") == "vlc_source" and "srt://" in url:
                    self.finding(
                        f"obs_vlc_source_info_{src_info.get('name')}", "info",
                        f"'{src_info.get('name')}' ist bereits eine VLC-Videoquelle",
                        "Diese ist fuer dauerhafte Live-SRT-Feeds meist stabiler als eine Media Source.",
                        "Keine Aktion noetig.",
                    )

    def _auto_reload_source(self, path: str):
        name = self.cfg.obs_media_source
        if not name:
            return

        async def _do():
            ok, err = await asyncio.to_thread(_restart_media_source, name)
            # In beiden Faellen (Erfolg oder Fehlschlag) ist der eigentliche
            # Netzwerk-Ausfall vorbei - die kritische Ausfall-Meldung im Panel
            # soll daher so oder so verschwinden, unabhaengig davon, ob der
            # automatische Neustart selbst geklappt hat.
            if ok:
                self.emit("finding", {
                    "key": f"srtla_auto_reload_{path}", "severity": "info",
                    "title": f"'{name}' automatisch neu geladen",
                    "detail": "Der Ausfall ist vorbei, die Verbindung ist seit "
                              f"{SRTLA_RECOVERY_STABLE_POLLS}s wieder stabil. Die OBS-Quelle wurde "
                              "automatisch neu gestartet, um den FFmpeg-Demuxer-Puffer zurueckzusetzen.",
                    "recommendation": "Pruefen, ob der Ton danach wieder normal klingt.",
                    "resolves": f"srtla_outage_{path}",
                })
            else:
                self.emit("finding", {
                    "key": f"srtla_auto_reload_failed_{path}", "severity": "warning",
                    "title": f"Automatischer Neustart von '{name}' fehlgeschlagen",
                    "detail": _friendly_error(err),
                    "recommendation": "Quelle manuell in OBS neu laden (z.B. per Szenenwechsel).",
                    "resolves": f"srtla_outage_{path}",
                })

        asyncio.create_task(_do())

    def _analyze_marker(self, marker_data: dict):
        window = [(src, d) for src, buf in self.recent.items() for _, d in buf]
        parts = []

        obs_events = [d for s, d in window if s == "obs" and d.get("ok")]
        if obs_events:
            congestions = [e.get("output_congestion") for e in obs_events if e.get("output_congestion")]
            if congestions:
                parts.append(f"OBS zeigte in den letzten {RECENT_WINDOW}s Kongestion (zuletzt {congestions[-1]}).")
            skipped_vals = [
                e.get("output_skipped_frames") for e in obs_events
                if isinstance(e.get("output_skipped_frames"), (int, float))
            ]
            if len(skipped_vals) >= 2 and skipped_vals[-1] - skipped_vals[0] > 5:
                parts.append(f"OBS hat {skipped_vals[-1] - skipped_vals[0]} Frames in diesem Zeitraum uebersprungen.")

        srtla_events = [d for s, d in window if s == "srtla" and d.get("ok")]
        if srtla_events:
            parts.append(f"Letzter SRTLA-Stats-Snapshot vor der Markierung: {json.dumps(srtla_events[-1].get('stats'), ensure_ascii=False)}")

        noalbs_lines = [d.get("line", "") for s, d in window if s == "noalbs" and d.get("line")]
        if noalbs_lines:
            parts.append("Letzte NOALBS-Zeilen: " + " | ".join(noalbs_lines[-3:]))

        router_labels = sorted({s for s, _ in window if re.fullmatch(r"router\d+", s)})
        for label in router_labels:
            unreachable = [d for s, d in window if s == label and d.get("ssh_ok") is False]
            if unreachable:
                parts.append(f"{label} war in diesem Zeitraum zeitweise nicht per SSH erreichbar.")

        if not parts:
            parts.append(
                f"Keine auffaelligen Werte in den letzten {RECENT_WINDOW}s in den ueberwachten Quellen gefunden - "
                "moeglicherweise reagieren die Metriken zu langsam fuer dieses Symptom, oder die Ursache liegt "
                "ausserhalb der beobachteten Daten (z.B. ein reiner Audio-Buffer-Drift innerhalb von OBS selbst)."
            )

        self.emit("finding", {
            "key": f"marker_analysis_{time.monotonic()}",
            "severity": "info",
            "title": "Analyse zum markierten Zeitpunkt",
            "detail": " ".join(parts),
            "recommendation": "Diese Momentaufnahme mit dem Rohdaten-Log um denselben Zeitstempel gegenpruefen.",
        })

    async def broadcaster(self):
        while True:
            event = await self.queue.get()
            line = json.dumps(event, ensure_ascii=False)
            self.log_file.write(line + "\n")
            dead = []
            for ws in self.clients:
                try:
                    await ws.send_text(line)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

    async def stop(self):
        self.stopped.set()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.log_file.close()


current_session: Optional[Session] = None


# ---------- Poller: SRTLA/SLS-Stats ----------


async def poll_srtla(session: Session):
    async with httpx.AsyncClient(timeout=3) as client:
        while not session.stopped.is_set():
            try:
                resp = await client.get(SRTLA_STATS_URL)
                resp.raise_for_status()
                session.emit("srtla", {"ok": True, "stats": resp.json()})
            except Exception as exc:
                session.emit("srtla", {"ok": False, "error": str(exc)})
            await asyncio.sleep(POLL_INTERVAL_SRTLA)


# ---------- Poller: OBS ----------

# Eine dauerhaft wiederverwendete OBS-Verbindung statt bei jedem einzelnen
# Aufruf neu zu verbinden/trennen - vorher bauten 3 unabhaengige Poller
# (Status alle 1s, Szenen-Quellen alle 2s, Infra-Check alle 5s) jeweils ihre
# eigene WebSocket-Verbindung zu OBS auf. obsws_python ist nicht fuer
# nebenlaeufige Aufrufe von mehreren Threads gleichzeitig ausgelegt, daher
# serialisiert ein Lock alle Zugriffe (lokales Netzwerk, kostet nur wenige ms
# Wartezeit unter Last). Bei einem Fehler wird die Verbindung verworfen, der
# naechste Aufruf baut automatisch eine neue auf (z.B. nach einem OBS-Neustart).
_obs_client: Optional["obs.ReqClient"] = None
_obs_client_lock = threading.Lock()


def _drop_obs_client_locked():
    global _obs_client
    if _obs_client is not None:
        try:
            _obs_client.disconnect()
        except Exception:
            pass
        _obs_client = None


@contextlib.contextmanager
def _obs_client_ctx():
    global _obs_client
    with _obs_client_lock:
        if _obs_client is None:
            _obs_client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
        try:
            yield _obs_client
        except Exception:
            _drop_obs_client_locked()
            raise


def _obs_snapshot() -> dict:
    with _obs_client_ctx() as client:
        stream = client.get_stream_status()
        scene = client.get_current_program_scene()
        record = client.get_record_status()
        stats = client.get_stats()
        return {
            "ok": True,
            "output_active": stream.output_active,
            "output_reconnecting": getattr(stream, "output_reconnecting", None),
            "output_congestion": getattr(stream, "output_congestion", None),
            "output_bytes": getattr(stream, "output_bytes", None),
            "output_skipped_frames": getattr(stream, "output_skipped_frames", None),
            "output_total_frames": getattr(stream, "output_total_frames", None),
            "output_timecode": getattr(stream, "output_timecode", None),
            "current_scene": getattr(scene, "current_program_scene_name", None),
            "record_active": getattr(record, "output_active", None),
            "record_timecode": getattr(record, "output_timecode", None),
            "record_bytes": getattr(record, "output_bytes", None),
            "active_fps": getattr(stats, "active_fps", None),
            "cpu_usage": getattr(stats, "cpu_usage", None),
            "memory_usage": getattr(stats, "memory_usage", None),
            "available_disk_space": getattr(stats, "available_disk_space", None),
        }


def _restart_media_source(name: str) -> tuple[bool, Optional[str]]:
    # TriggerMediaInputAction(RESTART) ruft in OBS obs_source_media_restart()
    # auf - dessen Seek/Flush-Logik ist in media.c hinter einer is_local_file-
    # Pruefung versteckt und tut fuer Netzwerkquellen (unser Fall) schlicht
    # nichts. Der tatsaechlich wirksame Weg (durch Lesen von OBS' eigenem
    # Quellcode bestaetigt, siehe obs-ffmpeg-source.c ffmpeg_source_update()):
    # ein SetInputSettings-Aufruf - auch mit unveraenderten Werten - loest bei
    # Nicht-lokalen Quellen bedingungslos ein komplettes Neu-Oeffnen aus.
    #
    # Retry+Verify (2026-08-25): direkt nach einem OBS-Neustart kann
    # SetInputSettings per RPC erfolgreich quittiert werden, waehrend die
    # Quelle intern noch nicht fertig in die gerade ladende Szenensammlung
    # eingehaengt ist - der Trigger bleibt dann wirkungslos, obwohl der aufruf
    # ok zurueckmeldet (live beobachtet: 2 von 3 Fix-Klicks direkt nach einem
    # OBS-Crash/Neustart blieben so ohne sichtbaren Effekt). Deshalb wird nach
    # dem Trigger verifiziert, dass die Quelle tatsaechlich in Wiedergabe geht,
    # bevor der Aufruf als erfolgreich gilt; bleibt das aus, wird der Trigger
    # automatisch erneut ausgeloest (gleiches Retry+Verify-Muster wie beim
    # RDP-Setup in provision.sh).
    last_err = "Unbekannter Fehler"
    for attempt in range(3):
        try:
            with _obs_client_ctx() as client:
                current = client.get_input_settings(name)
                client.set_input_settings(name, current.input_settings, True)
                state = None
                for _ in range(15):
                    time.sleep(0.3)
                    state = client.get_media_input_status(name).media_state
                    if state == "OBS_MEDIA_STATE_PLAYING":
                        return True, None
                last_err = f"Quelle nach Neuladen nicht in Wiedergabe (Status: {state})"
        except Exception as exc:
            last_err = str(exc)
    return False, last_err


def _obs_sources_snapshot() -> list[dict]:
    """Prueft alle Media-/VLC-Quellen in OBS auf ihre (SRT-)URL, z.B. um eine
    hohe SRT-Latency auf einer Media Source zu erkennen. Feldnamen von
    GetInputList/GetInputSettings sind noch nicht gegen ein echtes OBS mit
    konfigurierter Belabox-Quelle verifiziert - beim naechsten Livetest
    gegenchecken, falls hier nichts oder Falsches ankommt."""
    with _obs_client_ctx() as client:
        result = []
        inputs = client.get_input_list()
        for item in getattr(inputs, "inputs", None) or []:
            kind = item.get("inputKind") if isinstance(item, dict) else None
            name = item.get("inputName") if isinstance(item, dict) else None
            if kind not in ("ffmpeg_source", "vlc_source") or not name:
                continue
            try:
                settings = client.get_input_settings(name)
                raw = getattr(settings, "input_settings", None) or {}
                url = raw.get("input") or raw.get("local_file") or ""
            except Exception:
                url = ""
            result.append({"name": name, "kind": kind, "url": url})
        return result


async def poll_obs(session: Session):
    # Quellen-Check (welche OBS-Quelle nimmt den Belabox-Feed entgegen, mit
    # welcher SRT-Latency) nur einmal pro Session, das aendert sich waehrend
    # eines laufenden Tests normalerweise nicht.
    try:
        sources = await asyncio.to_thread(_obs_sources_snapshot)
        session.emit("obs_sources", {"sources": sources})
    except Exception as exc:
        session.emit("obs_sources", {"sources": [], "error": str(exc)})

    while not session.stopped.is_set():
        try:
            data = await asyncio.to_thread(_obs_snapshot)
            session.emit("obs", data)
        except Exception as exc:
            session.emit("obs", {"ok": False, "error": str(exc)})
        await asyncio.sleep(POLL_INTERVAL_OBS)


def _obs_scene_items_snapshot() -> dict:
    """Alle Quellen der aktuell aktiven Programm-Szene mit Sichtbarkeits- und
    Mute-Status - fuer die Quellen-Liste im Dashboard (Auge/Lautsprecher-Icons).
    Nur echte Inputs (sourceType == OBS_SOURCE_TYPE_INPUT) werden auf Audio
    geprueft - verschachtelte Szenen als Item (Code 602) und Inputs ohne Audio
    (Code 604) werfen sonst nur unnoetige Fehler bei jedem Poll."""
    with _obs_client_ctx() as client:
        scene = client.get_current_program_scene()
        scene_name = scene.current_program_scene_name
        items = client.get_scene_item_list(scene_name)
        result = []
        for it in items.scene_items:
            has_audio = False
            muted = None
            volume = None
            if it.get("sourceType") == "OBS_SOURCE_TYPE_INPUT":
                # obsws_python loggt einen vollen Traceback fuer den erwarteten
                # Fehlschlag bei Quellen ohne Audio, BEVOR wir ihn hier unten
                # abfangen koennen - bei jedem Poll (5s) fuer jede
                # Nicht-Audio-Quelle, das war der groesste Einzelposten im
                # Docker-Log (siehe max-size-Erhoehung in docker-compose.yml).
                # Nur waehrend dieses einen erwartungsgemaessen Aufrufs
                # stummgeschaltet (Lock in _obs_client_ctx serialisiert
                # ohnehin alle OBS-Zugriffe) - echte Fehler bei anderen
                # OBS-Aufrufen bleiben normal sichtbar.
                _obsws_logger = logging.getLogger("obsws_python.reqs.ReqClient")
                _prev_level = _obsws_logger.level
                _obsws_logger.setLevel(logging.CRITICAL)
                try:
                    mute = client.get_input_mute(it.get("sourceName"))
                    has_audio = True
                    muted = mute.input_muted
                    volume = round(client.get_input_volume(it.get("sourceName")).input_volume_mul, 3)
                except Exception:
                    pass
                finally:
                    _obsws_logger.setLevel(_prev_level)
            result.append({
                "item_id": it.get("sceneItemId"),
                "name": it.get("sourceName"),
                "kind": it.get("inputKind") or it.get("sourceType"),
                "visible": it.get("sceneItemEnabled"),
                "has_audio": has_audio,
                "muted": muted,
                "volume": volume,
                "index": it.get("sceneItemIndex"),
            })
        # Index 0 = unterste Ebene im OBS-Sources-Panel (obs-websocket-Protokoll) -
        # absteigend sortiert entspricht das exakt der Reihenfolge, wie OBS die
        # Quellen von oben nach unten anzeigt.
        result.sort(key=lambda x: x["index"] if x["index"] is not None else -1, reverse=True)
        return {"ok": True, "scene": scene_name, "items": result}


async def poll_obs_scene_items(session: Session):
    while not session.stopped.is_set():
        try:
            data = await asyncio.to_thread(_obs_scene_items_snapshot)
            session.emit("obs_scene_items", data)
        except Exception as exc:
            session.emit("obs_scene_items", {"ok": False, "error": str(exc)})
        await asyncio.sleep(POLL_INTERVAL_OBS_SOURCES)


# ---------- Poller: OBS-Vorschaubild (klein, per Schalter an/aus) ----------
# Standardmaessig AUS (Opt-in, nicht in der Config gespeichert - startet nach
# einem Neustart des Containers wieder deaktiviert). Live gemessen: ein
# 320x180-Screenshot der aktuellen Programmszene braucht ~40ms und ergibt
# ~8KB JPEG - laeuft rein im Heimnetz zwischen Unraid und dem OBS-PC, NICHT
# ueber die Mobilfunk-/WLAN-Strecke der Belabox, also auch bei aktivem Stream
# unkritisch fuer die eigentliche Uebertragung.
POLL_INTERVAL_OBS_PREVIEW = 5.0
OBS_PREVIEW_WIDTH = 320
OBS_PREVIEW_HEIGHT = 180
OBS_PREVIEW_QUALITY = 70

_obs_preview_enabled = False


def _obs_preview_snapshot() -> dict:
    with _obs_client_ctx() as client:
        scene = client.get_current_program_scene()
        scene_name = scene.current_program_scene_name
        shot = client.get_source_screenshot(
            scene_name, "jpeg", OBS_PREVIEW_WIDTH, OBS_PREVIEW_HEIGHT, OBS_PREVIEW_QUALITY
        )
        return {"ok": True, "scene": scene_name, "image": shot.image_data}


async def poll_obs_preview(session: Session):
    while not session.stopped.is_set():
        if _obs_preview_enabled:
            try:
                data = await asyncio.to_thread(_obs_preview_snapshot)
                session.emit("obs_preview", data)
            except Exception as exc:
                session.emit("obs_preview", {"ok": False, "error": str(exc)})
        await asyncio.sleep(POLL_INTERVAL_OBS_PREVIEW)


# ---------- Poller: NOALBS-VM (SSH, tailt die aktuellste Log-Datei) ----------


def _ssh_connect(host: str, user: str, password: str = "", key_path: str = "") -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # 10s statt vorher 6s - auf zellulaeren Verbindungen (Router/Belabox
    # unterwegs) sind kurze RTT-Spitzen in den Sekundenbereich normal, ein zu
    # knapper Timeout meldete dadurch faelschlich "nicht erreichbar".
    if key_path:
        c.connect(host, username=user, key_filename=key_path, timeout=10)
    else:
        c.connect(host, username=user, password=password, timeout=10)
    return c


def _parse_noalbs_scene_line(line: str) -> Optional[dict]:
    m = _NOALBS_SCENE_RE.search(line)
    if not m:
        return None
    scene_name = m.group(1).strip()
    ts_token = line.split(None, 1)[0] if line else None
    if scene_name == NOALBS_OFFLINE_SCENE:
        state = "offline"
    elif scene_name == NOALBS_LOW_SCENE:
        state = "low"
    else:
        state = "online"
    return {
        "scene": scene_name,
        "state": state,
        "switched_at": ts_token,
    }


def _noalbs_last_scene(ssh: paramiko.SSHClient) -> Optional[dict]:
    # Beim (Neu-)Verbinden die letzte "Scene switched"-Zeile ueber die gesamte
    # Log-Historie suchen (nicht nur die letzten 4KB aus _noalbs_tail), damit
    # die Karte sofort den aktuellen Szenenstatus zeigt statt erst auf den
    # naechsten tatsaechlichen Wechsel warten zu muessen.
    _, stdout, _ = ssh.exec_command(
        f"grep -h 'Scene switched to' {NOALBS_LOG_DIR}/*.log 2>/dev/null | tail -1"
    )
    line = _ANSI_RE.sub("", stdout.read().decode(errors="replace")).strip()
    return _parse_noalbs_scene_line(line)


def _noalbs_tail(ssh: paramiko.SSHClient, offsets: dict) -> list[str]:
    # Aktuellste Log-Datei ermitteln (NOALBS legt bei jedem Neustart eine neue an)
    _, stdout, _ = ssh.exec_command(
        f"ls -t {NOALBS_LOG_DIR}/*.log 2>/dev/null | head -1"
    )
    latest = stdout.read().decode().strip()
    if not latest:
        return []
    _, stdout, _ = ssh.exec_command(f"stat -c %s {latest}")
    try:
        size = int(stdout.read().decode().strip())
    except ValueError:
        return []
    last_file = offsets.get("file")
    last_offset = offsets.get("offset", 0)
    if last_file != latest or size < last_offset:
        # Neue Datei (Neustart) oder rotiert -> von vorne, aber nur die letzten 4KB
        # um nicht die komplette Historie beim Session-Start zu fluten.
        start = max(0, size - 4096)
    else:
        start = last_offset
    if start >= size:
        offsets["file"], offsets["offset"] = latest, size
        return []
    _, stdout, _ = ssh.exec_command(f"tail -c +{start + 1} {latest}")
    chunk = stdout.read().decode(errors="replace")
    offsets["file"], offsets["offset"] = latest, size
    return [_ANSI_RE.sub("", line) for line in chunk.splitlines() if line.strip()]


def _noalbs_local_docker_last_scene() -> Optional[dict]:
    code, output = _belabox_exec(
        ["sh", "-c", f"grep -h 'Scene switched to' {NOALBS_LOCAL_LOG_PATH} 2>/dev/null | tail -1"]
    )
    line = _ANSI_RE.sub("", output.decode(errors="replace")).strip()
    return _parse_noalbs_scene_line(line)


def _noalbs_local_docker_tail(offsets: dict) -> list[str]:
    code, output = _belabox_exec(["stat", "-c", "%s", NOALBS_LOCAL_LOG_PATH])
    if code != 0:
        return []
    try:
        size = int(output.decode().strip())
    except ValueError:
        return []
    last_offset = offsets.get("offset", 0)
    start = max(0, size - 4096) if size < last_offset else last_offset
    if start >= size:
        offsets["offset"] = size
        return []
    code, output = _belabox_exec(["tail", "-c", f"+{start + 1}", NOALBS_LOCAL_LOG_PATH])
    chunk = output.decode(errors="replace")
    offsets["offset"] = size
    return [_ANSI_RE.sub("", line) for line in chunk.splitlines() if line.strip()]


async def poll_noalbs_local_docker(session: Session):
    # Aequivalent zu poll_noalbs unten, nur per docker-exec-Tail statt SSH -
    # dieselben Parsing-/Zustandsfunktionen (_parse_noalbs_scene_line) werden
    # wiederverwendet, damit sich das Frontend (Szenen-Karte) nicht um den
    # Unterschied kuemmern muss.
    offsets: dict = {}
    scene_state = {"scene": None, "state": None, "switched_at": None}
    try:
        seeded = await asyncio.to_thread(_noalbs_local_docker_last_scene)
        if seeded:
            scene_state = seeded
            session.emit("noalbs", {"ok": True, "line": "", **scene_state})
    except Exception:
        pass
    while not session.stopped.is_set():
        try:
            lines = await asyncio.to_thread(_noalbs_local_docker_tail, offsets)
            for line in lines:
                parsed = _parse_noalbs_scene_line(line)
                if parsed:
                    scene_state = parsed
                session.emit("noalbs", {"ok": True, "line": line, **scene_state})
        except Exception as exc:
            session.emit("noalbs", {"ok": False, "error": str(exc)})
        await asyncio.sleep(POLL_INTERVAL_NOALBS)


async def poll_noalbs(session: Session):
    if NOALBS_MODE == "local_docker":
        await poll_noalbs_local_docker(session)
        return
    try:
        ssh = await asyncio.to_thread(
            _ssh_connect, NOALBS_HOST, NOALBS_SSH_USER, key_path=NOALBS_SSH_KEY_PATH
        )
    except Exception as exc:
        session.emit("noalbs", {"ok": False, "error": f"SSH-Verbindung fehlgeschlagen: {exc}"})
        return
    offsets: dict = {}
    scene_state = {"scene": None, "state": None, "switched_at": None}
    try:
        seeded = await asyncio.to_thread(_noalbs_last_scene, ssh)
        if seeded:
            scene_state = seeded
            session.emit("noalbs", {"ok": True, "line": "", **scene_state})
    except Exception:
        pass
    try:
        while not session.stopped.is_set():
            try:
                lines = await asyncio.to_thread(_noalbs_tail, ssh, offsets)
                for line in lines:
                    parsed = _parse_noalbs_scene_line(line)
                    if parsed:
                        scene_state = parsed
                    session.emit("noalbs", {"ok": True, "line": line, **scene_state})
            except Exception as exc:
                session.emit("noalbs", {"ok": False, "error": str(exc)})
            await asyncio.sleep(POLL_INTERVAL_NOALBS)
    finally:
        ssh.close()


# ---------- Poller: Belabox-Encoder (SSH, generischer Health-Check) ----------

_belabox_net_prev: dict = {}
_belabox_net_lock = threading.Lock()


def _parse_net_dev_tx(raw: str) -> dict:
    """/proc/net/dev: Spalten nach dem Doppelpunkt sind erst 8x Receive, dann
    8x Transmit (jeweils bytes packets errs drop fifo ...). Transmit-Bytes ist
    also Index 8 der Spaltenliste."""
    tx = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        cols = rest.split()
        if name != "lo" and len(cols) >= 9:
            try:
                tx[name] = int(cols[8])
            except ValueError:
                pass
    return tx


def _mbps_since_last_sample(host: str, tx_bytes: dict) -> dict:
    """Wandelt kumulierte TX-Byte-Zaehler in eine Momentan-Bitrate (Mbit/s) um,
    indem die Differenz zum letzten Poll durch die vergangene Zeit geteilt
    wird - klassische Delta-Berechnung, wie man sie auch von `iftop`/`vnstat`
    kennt. Erster Poll nach Start liefert noch keinen Wert (kein Vorgaenger)."""
    now = time.monotonic()
    with _belabox_net_lock:
        prev = _belabox_net_prev.get(host)
        _belabox_net_prev[host] = (tx_bytes, now)
    if not prev:
        return {}
    prev_bytes, prev_time = prev
    elapsed = now - prev_time
    if elapsed <= 0:
        return {}
    result = {}
    for name, bytes_now in tx_bytes.items():
        bytes_before = prev_bytes.get(name)
        if bytes_before is None or bytes_now < bytes_before:
            continue  # Interface neu aufgetaucht oder Zaehler zurueckgesetzt (z.B. Reconnect)
        result[name] = round((bytes_now - bytes_before) * 8 / elapsed / 1_000_000, 2)
    return result


def _belabox_snapshot(ssh: paramiko.SSHClient, host: str) -> dict:
    """Live am eigenen RK3588-Belabox-Encoder verifiziert (2026-08-19, eigener
    SRTLA-Relay statt offizieller Belabox-Cloud): der Dienst heisst 'belaUI'
    (Grossschreibung!), nicht 'belaui' wie urspruenglich angenommen - das war
    der Grund fuer 'kein belaui-Service gefunden'. Statt des generischen
    Health-Checks lesen wir jetzt die tatsaechlich relevanten Prozesse direkt:
    srtla_send (das eigentliche Bonding) und belacoder (die Encoder-Pipeline),
    plus welche Netzwerk-Interfaces gerade im Bonding-Pool (/tmp/srtla_ips)
    stecken - das ist die Grundlage fuer die Redundanz gegen einen einzelnen
    ausfallenden Link (siehe Router2-Vorfall im ersten Livetest).
    Nimmt eine bereits bestehende SSH-Verbindung entgegen (siehe poll_belabox)
    statt selbst zu verbinden/trennen - die Verbindung wird ueber die gesamte
    Session hinweg wiederverwendet (analog zum NOALBS-Log-Poller), da der
    Encoder oft ueber dieselbe Mobilfunk-/WLAN-Strecke wie der Stream laeuft
    und ein Neuverbinden alle paar Sekunden dort unnoetig Bandbreite kostet."""
    # "| grep -v ^$$ " filtert den Selbstmatch raus: pgrep -f matcht per
    # Substring auch die eigene "bash -c '...srtla_send...'"-Aufrufzeile
    # dieses SSH-Kommandos, weil die den Suchbegriff selbst enthaelt. $$
    # ist die PID dieser Shell - genau die Zeile, die faelschlich matcht.
    cmd = (
        "(systemctl is-active belaUI 2>&1 || true); echo ---; "
        "pgrep -fa srtla_send | grep -v \"^$$ \"; echo ---; "
        "pgrep -fa belacoder | grep -v \"^$$ \"; echo ---; "
        "cat /tmp/srtla_ips 2>/dev/null; echo ---; "
        "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; echo ---; "
        "cat /proc/loadavg; echo ---; "
        "ip -4 -br addr show 2>/dev/null; echo ---; "
        "cat /proc/net/dev"
    )
    _, stdout, _ = ssh.exec_command(cmd)
    output = stdout.read().decode(errors="replace")
    parts = [p.strip() for p in output.split("---")]
    parts += [""] * (8 - len(parts))
    belaui_status, srtla_proc, belacoder_proc, bonded_raw, temp_raw, loadavg_raw, ifaces_raw, netdev_raw = parts[:8]

    srtla_target = None
    m = re.search(r"srtla_send\s+\d+\s+(\S+)\s+(\d+)", srtla_proc)
    if m:
        srtla_target = f"{m.group(1)}:{m.group(2)}"

    bonded_ips = [ip.strip() for ip in bonded_raw.splitlines() if ip.strip()]
    interfaces = []
    for line in ifaces_raw.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[0] != "lo" and cols[1] == "UP":
            ip = cols[2].split("/")[0]
            interfaces.append({"name": cols[0], "ip": ip, "bonded": ip in bonded_ips})

    cpu_temp = None
    try:
        cpu_temp = round(int(temp_raw.splitlines()[0]) / 1000, 1)
    except (ValueError, IndexError):
        pass

    tx_bytes = _parse_net_dev_tx(netdev_raw)
    mbps_by_iface = _mbps_since_last_sample(host, tx_bytes)
    total_mbps = 0.0
    for iface in interfaces:
        mbps = mbps_by_iface.get(iface["name"])
        iface["mbps"] = mbps
        if iface["bonded"] and mbps is not None:
            total_mbps += mbps
    total_mbps = round(total_mbps, 2) if mbps_by_iface else None

    return {
        "ok": True,
        "belaui_active": belaui_status.strip() == "active",
        "srtla_send_running": bool(srtla_proc.strip()),
        "srtla_target": srtla_target,
        "belacoder_running": bool(belacoder_proc.strip()),
        "cpu_temp": cpu_temp,
        "load_average": loadavg_raw.split()[:3] if loadavg_raw else None,
        "interfaces": interfaces,
        "bonded_link_count": len(bonded_ips),
        "total_mbps": total_mbps,
    }


async def _belabox_ws_auth(ws, ui_password: str, wait_for: tuple = ()) -> dict:
    """Gemeinsamer Login-Teil von belaUIs WebSocket-Protokoll (siehe
    _belabox_ws_command fuer die vollstaendige Herleitung aus belaUI.js):
    {"auth": {"password": ...}} -> {"auth": {"success": true/false}}, gefolgt
    von sendInitialStatus() (config/netif/status/...). wait_for benennt
    zusaetzliche Nachrichtentypen (z.B. "config", "netif"), auf die noch
    gewartet wird, bevor zurueckgegeben wird - gesammelt in einem dict."""
    if not ui_password:
        raise RuntimeError(
            "Kein belaUI-Passwort hinterlegt - in der Geraete-Konfiguration unter "
            "Belabox-Encoder eintragen (Login-Passwort der belaUI-Weboberflaeche, "
            "nicht das SSH-Passwort)."
        )
    await ws.send(json.dumps({"auth": {"password": ui_password}}))

    authed = False
    collected: dict = {}
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (authed and all(k in collected for k in wait_for)):
        raw = await asyncio.wait_for(ws.recv(), timeout=8)
        msg = json.loads(raw)
        if "auth" in msg:
            if not msg["auth"].get("success"):
                raise RuntimeError("belaUI-Login fehlgeschlagen (falsches Passwort?)")
            authed = True
        for key in wait_for:
            if key in msg:
                collected[key] = msg[key]
    if not authed:
        raise RuntimeError("Keine Login-Antwort von belaUI erhalten (Zeitueberschreitung)")
    return collected


async def _belabox_ws_command(
    host: str, ui_password: str, action: str, overrides: Optional[dict] = None
) -> dict:
    """Startet/stoppt den Stream ueber belaUIs eigenes WebSocket-Protokoll -
    es gibt dafuer keine dokumentierte HTTP-API, das Protokoll wurde direkt
    aus /opt/belaUI/belaUI.js (Node-Quellcode auf dem Encoder) und
    public/script.js (verbindet zu ws://<host>/, kein eigener Pfad) rekonstruiert:
    {"auth": {"password": ...}} -> {"auth": {"success": true/false}} + Status/Config,
    dann {"start": <aktuelle Konfiguration>} oder {"stop": 0}. Fuer "start" wird
    grundsaetzlich die vom Server selbst gerade gemeldete Konfiguration
    zurueckgeschickt (wie es die echte Weboberflaeche tut) - updateConfig() in
    belaUI.js ueberschreibt sonst einzelne Felder blind mit undefined, wenn
    Parameter fehlen. `overrides` (z.B. {"max_br": 6000}) wird vor dem
    Zurueckschicken drueber gemischt - fuer Einstellungen wie die Ziel-Bitrate,
    die der Nutzer im Dashboard schon VOR dem Start angepasst haben koennte,
    ohne dass NOALBS/belaUI selbst das bereits kennt.
    """
    uri = f"ws://{host}/"
    async with websockets.connect(uri, open_timeout=5) as ws:
        collected = await _belabox_ws_auth(ws, ui_password, wait_for=("config",))
        current_config = collected.get("config")

        if action == "start":
            if current_config is None:
                raise RuntimeError("Keine aktuelle Konfiguration von belaUI erhalten")
            if overrides:
                current_config = {**current_config, **overrides}
            await ws.send(json.dumps({"start": current_config}))
        else:
            await ws.send(json.dumps({"stop": 0}))

        want_streaming = action == "start"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if "notification" in msg:
                for n in msg["notification"].get("show", []):
                    if n.get("type") == "error":
                        raise RuntimeError(n.get("msg") or "belaUI meldet einen Fehler")
            if "status" in msg and msg["status"].get("is_streaming") == want_streaming:
                return {"ok": True, "is_streaming": msg["status"]["is_streaming"]}
        raise RuntimeError("Zeitueberschreitung: keine Bestaetigung von belaUI erhalten")


BELABOX_LIVE_KEEPALIVE_INTERVAL = 20.0  # Sekunden ohne Nachricht, bevor ein Lebenszeichen gesendet wird
BELABOX_LIVE_RECONNECT_DELAY = 5.0  # Wartezeit vor einem erneuten Verbindungsversuch nach einem Fehler
BELABOX_LIVE_STALE_THRESHOLD = 25.0  # Sekunden ohne jedes Update, ab denen der Task als haengend gilt
BELABOX_LIVE_WATCHDOG_INTERVAL = 10.0  # Pruefintervall des Wachhunds

# Zeitstempel (time.monotonic()) des letzten belabox_live-Updates (Erfolg
# ODER protokollierter Fehler) - siehe _belabox_live_watchdog(). Live am
# 2026-08-23 beobachtet: der Verbindungs-Task kann sich lautlos aufhaengen
# (kein einziges Ereignis mehr, auch kein Fehler), OHNE dass die eingebaute
# try/except-Reconnect-Schleife in poll_belabox_live() das je mitbekommt -
# die greift nur bei einer tatsaechlich geworfenen Exception, ein echt
# haengender await wirft aber nie eine. Root Cause nicht abschliessend
# geklaert (vermutlich ein stiller Verbindungsabriss, den weder websockets'
# eingebautes Ping/Pong noch das 20s-Keepalive-Timeout auffangen) - deshalb
# hier ein reiner Aktivitaets-Wachhund von aussen statt eines Versuchs, die
# genaue Ursache im Detail zu beheben.
_belabox_live_last_update: float = 0.0

# Geteilter Zustand statt einer lokalen Variable in _belabox_live_listen():
# handleNetif() in belaUI.js bestaetigt eine Bonding-Aenderung NUR direkt an
# den Absender (conn.send), anders als 'bitrate'/'config'/'status'
# (broadcastMsg/broadcastMsgExcept) - der dauerhafte Zuhoerer hier wuerde eine
# per /belabox/netif ausgeloeste Aenderung sonst nie mitbekommen. Der Endpoint
# aktualisiert dieses Dict daher direkt mit (siehe belabox_set_netif), damit
# beide Wege konsistent denselben, aktuellen Stand fortschreiben.
_belabox_live_state: dict = {"interfaces": {}, "max_br": None, "is_streaming": None, "pipeline": None}


async def _belabox_live_listen(session: Session, profile: DeviceProfile):
    """Haelt EINE WebSocket-Verbindung zu belaUI dauerhaft offen, statt wie
    zuvor alle 5s komplett neu zu verbinden+zu authentifizieren - das lief
    ueber dieselbe Mobilfunk-/WLAN-Strecke wie der Stream selbst und hat dort
    unnoetig Bandbreite verbraucht. belaUI BROADCASTET die meisten Aenderungen
    (Bitrate, Start/Stopp) aktiv an alle verbundenen Clients, auch an rein
    lesende wie diese hier (siehe broadcastMsg()/broadcastMsgExcept() in
    belaUI.js) - wir muessen also nicht aktiv nachfragen, sondern nur
    zuhoeren und bei Aenderungen den Stand aktualisieren (Ausnahme: Bonding-
    Haken, siehe _belabox_live_state-Kommentar). Ein periodisches Keepalive
    haelt die Verbindung offen und erkennt einen toten Socket, ohne staendig
    neu zu authentifizieren."""
    global _belabox_live_last_update
    uri = f"ws://{profile.host}/"
    async with websockets.connect(uri, open_timeout=5, ping_interval=10, ping_timeout=10) as ws:
        collected = await _belabox_ws_auth(ws, profile.ui_password, wait_for=("netif", "config", "status"))
        _belabox_live_state["interfaces"] = collected.get("netif") or {}
        _belabox_live_state["max_br"] = (collected.get("config") or {}).get("max_br")
        _belabox_live_state["pipeline"] = (collected.get("config") or {}).get("pipeline")
        _belabox_live_state["is_streaming"] = (collected.get("status") or {}).get("is_streaming")
        session.emit("belabox_live", {"ok": True, **_belabox_live_state})
        _belabox_live_last_update = time.monotonic()

        while not session.stopped.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=BELABOX_LIVE_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                # Nichts Neues seit dem Keepalive-Intervall - kurzes
                # Lebenszeichen senden (vom Protokoll explizit als No-Op
                # vorgesehen, siehe handleMessage() case 'keepalive').
                # WICHTIG: hier NICHT _belabox_live_last_update anfassen - ein
                # send() auf eine bereits kaputte/haengende Verbindung kann
                # erfolgreich zurueckkehren (TCP puffert lokal), obwohl nie
                # wieder etwas ankommt. Genau das hat den Watchdog beim ersten
                # Versuch blind gemacht: er hielt die Verbindung faelschlich
                # dauerhaft fuer lebendig, nur weil WIR SELBST alle 20s brav
                # weitergesendet haben. Nur tatsaechlich empfangene Daten
                # (unten) zaehlen als Lebenszeichen.
                await ws.send(json.dumps({"keepalive": 1}))
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            changed = False
            if isinstance(msg.get("netif"), dict):
                _belabox_live_state["interfaces"] = msg["netif"]
                changed = True
            if isinstance(msg.get("config"), dict) and "max_br" in msg["config"]:
                _belabox_live_state["max_br"] = msg["config"]["max_br"]
                changed = True
            if isinstance(msg.get("config"), dict) and "pipeline" in msg["config"]:
                _belabox_live_state["pipeline"] = msg["config"]["pipeline"]
                changed = True
            if isinstance(msg.get("status"), dict) and "is_streaming" in msg["status"]:
                _belabox_live_state["is_streaming"] = msg["status"]["is_streaming"]
                changed = True
            if isinstance(msg.get("bitrate"), dict) and "max_br" in msg["bitrate"]:
                _belabox_live_state["max_br"] = msg["bitrate"]["max_br"]
                changed = True
            _belabox_live_last_update = time.monotonic()
            if changed:
                session.emit("belabox_live", {"ok": True, **_belabox_live_state})


async def _belabox_set_netif(host: str, ui_password: str, name: str, ip: str, enabled: bool) -> dict:
    """Setzt/entfernt eine einzelne Schnittstelle aus dem Bonding - das
    Pendant zu den Haken in belaUIs eigener Weboberflaeche (handleNetif() in
    belaUI.js). Die IP muss exakt zur aktuell von belaUI gemeldeten IP dieser
    Schnittstelle passen, sonst ignoriert der Server die Anfrage stillschweigend.
    WICHTIG (anders als bei 'bitrate'/'config'/'status'): handleNetif()
    schickt die Bestaetigung per conn.send() NUR an den Absender selbst
    zurueck, nicht per broadcastMsg() an alle Clients - der separate
    dauerhafte Zuhoerer (poll_belabox_live/_belabox_live_listen) wuerde eine
    Aenderung hier NIE mitbekommen. Deshalb sammeln wir hier zusaetzlich
    config/status ein und geben den kompletten Stand zurueck - der Aufrufer
    (siehe /belabox/netif) schiebt ihn dem Zuhoerer aktiv unter."""
    uri = f"ws://{host}/"
    async with websockets.connect(uri, open_timeout=5) as ws:
        collected = await _belabox_ws_auth(ws, ui_password, wait_for=("config", "status"))
        await ws.send(json.dumps({"netif": {"name": name, "ip": ip, "enabled": enabled}}))

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            msg = json.loads(raw)
            if "notification" in msg:
                for n in msg["notification"].get("show", []):
                    if n.get("type") == "error":
                        raise RuntimeError(n.get("msg") or "belaUI meldet einen Fehler")
            if "netif" in msg:
                current = msg["netif"].get(name)
                if current is not None and current.get("enabled") == enabled:
                    return {
                        "ok": True,
                        "interfaces": msg["netif"],
                        "max_br": (collected.get("config") or {}).get("max_br"),
                        "is_streaming": (collected.get("status") or {}).get("is_streaming"),
                    }
        raise RuntimeError("Zeitueberschreitung: keine Bestaetigung von belaUI erhalten")


async def _belabox_set_bitrate(host: str, ui_password: str, max_br: int) -> dict:
    """Setzt die maximale SRTLA-Uebertragungsbitrate. NUR live wirksam
    waehrend eines laufenden Streams: handleMessage() in belaUI.js versteckt
    case 'bitrate' hinter `if (isStreaming)` - ohne aktiven Stream verwirft
    der Server die Nachricht stillschweigend, ohne jede Fehlermeldung. Laeuft
    kein Stream, greift der neue Wert daher erst beim naechsten Start (siehe
    _belabox_call: max_br wird dort als Override in die Start-Konfiguration
    gemischt). Ausserdem schickt der Server eine Erfolgsbestaetigung nur an
    ALLE ANDEREN Clients (broadcastMsgExcept()), nie an den Absender selbst -
    es gibt also protokollseitig keine direkte Erfolgsmeldung fuer die eigene
    Aenderung; wir warten nur kurz auf eine etwaige Fehlermeldung."""
    uri = f"ws://{host}/"
    async with websockets.connect(uri, open_timeout=5) as ws:
        collected = await _belabox_ws_auth(ws, ui_password, wait_for=("status",))
        if not (collected.get("status") or {}).get("is_streaming"):
            return {
                "ok": True, "applied_live": False,
                "note": "Kein aktiver Stream - Wert wird erst beim naechsten Start uebernommen.",
            }
        await ws.send(json.dumps({"bitrate": {"max_br": max_br}}))
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                msg = json.loads(raw)
                if "notification" in msg:
                    for n in msg["notification"].get("show", []):
                        if n.get("type") == "error":
                            raise RuntimeError(n.get("msg") or "belaUI meldet einen Fehler")
        except asyncio.TimeoutError:
            pass
        return {"ok": True, "applied_live": True}


async def poll_belabox_live(session: Session, profile: DeviceProfile):
    global _belabox_live_last_update
    if not profile.host:
        return
    while not session.stopped.is_set():
        try:
            await _belabox_live_listen(session, profile)
        except Exception as exc:
            session.emit("belabox_live", {"ok": False, "error": str(exc)})
            _belabox_live_last_update = time.monotonic()
        if session.stopped.is_set():
            break
        # Verbindung ist abgebrochen/fehlgeschlagen - kurze Pause vor dem
        # naechsten Versuch, statt sofort erneut zu haemmern.
        await asyncio.sleep(BELABOX_LIVE_RECONNECT_DELAY)


async def _belabox_live_watchdog(session: Session, profile: DeviceProfile, initial_task: "asyncio.Task"):
    """Aeusserer Wachhund fuer poll_belabox_live() - siehe Kommentar bei
    _belabox_live_last_update fuer den Hintergrund (live am 2026-08-23
    beobachtet: der Verbindungs-Task kann sich lautlos aufhaengen, ohne dass
    poll_belabox_live()s eigene try/except-Reconnect-Schleife das je
    mitbekommt). Prueft periodisch, ob ueberhaupt noch Aktivitaet
    (Erfolg ODER protokollierter Fehler) stattfindet - falls nicht, wird der
    haengende Task hart gecancelt und durch einen frischen ersetzt, statt auf
    eine Exception zu warten, die nie kommt."""
    global _belabox_live_last_update
    task = initial_task
    while not session.stopped.is_set():
        await asyncio.sleep(BELABOX_LIVE_WATCHDOG_INTERVAL)
        if session.stopped.is_set():
            return
        stale_for = time.monotonic() - _belabox_live_last_update
        if stale_for <= BELABOX_LIVE_STALE_THRESHOLD:
            continue
        logging.warning(f"belabox_live watchdog: stale for {stale_for:.1f}s, forcing reconnect")
        task.cancel()
        with contextlib.suppress(BaseException):
            # Bewusst mit eigenem Timeout statt eines unbegrenzten await: ein
            # cancel() liefert nur ein CancelledError an die naechste
            # Suspendierungsstelle im Task - haengt der Task an einer Stelle,
            # die das nicht zeitnah annimmt, wuerde ein blankes "await task"
            # den Watchdog selbst auf unbestimmte Zeit mitreissen. Nach dem
            # Timeout wird der alte Task einfach als Waise zurueckgelassen
            # (verliert seine letzte Referenz -> Garbage Collection), statt
            # den Watchdog zu blockieren.
            await asyncio.wait_for(task, timeout=5)
        logging.warning("belabox_live watchdog: old task cancelled, starting fresh one")
        task = asyncio.create_task(poll_belabox_live(session, profile))
        session.tasks.append(task)
        _belabox_live_last_update = time.monotonic()


async def poll_belabox(session: Session, profile: DeviceProfile):
    # SSH-Verbindung wird ueber die gesamte Session wiederverwendet (wie beim
    # NOALBS-Log-Poller) statt bei jedem Poll neu zu verbinden - der Encoder
    # laeuft oft ueber dieselbe Mobilfunk-/WLAN-Strecke wie der Stream selbst.
    # Anders als beim bestehenden NOALBS-Poller wird hier bei einem Fehler
    # aber tatsaechlich neu verbunden, statt auf einer toten Verbindung
    # haengen zu bleiben.
    if not profile.host:
        return
    ssh: Optional[paramiko.SSHClient] = None
    try:
        while not session.stopped.is_set():
            try:
                if ssh is None:
                    ssh = await asyncio.to_thread(
                        _ssh_connect, profile.host, profile.ssh_user, profile.ssh_password
                    )
                data = await asyncio.to_thread(_belabox_snapshot, ssh, profile.host)
                session.emit("belabox", data)
            except Exception as exc:
                session.emit("belabox", {"ok": False, "error": str(exc)})
                if ssh is not None:
                    try:
                        ssh.close()
                    except Exception:
                        pass
                    ssh = None
            await asyncio.sleep(POLL_INTERVAL_BELABOX)
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass


# ---------- Poller: GL.iNet-Router ----------


_router_clients: dict = {}
_router_clients_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Belabox-Proxy-Fundament (Nutzerentscheidung 2026-08-31): Router sind vom
# Dashboard aus NICHT mehr direkt erreichbar - nur die Belabox ist ueber den
# bestehenden WireGuard-Tunnel erreichbar (siehe BELABOX_HOST), sie selbst
# haengt aber (WLAN/USB-Tethering) im lokalen Netz des Routers/Hotspots.
# Alle Router-Abfragen laufen deshalb ueber die bestehende SSH-Verbindung zur
# Belabox: entweder als einfacher Shell-Befehl dort (curl, iw, ethtool) oder,
# wo eine fertige Python-Bibliothek mit eigenem Login-Handshake gebraucht
# wird (pyglinet, tplinkrouterc6u), als lokaler SSH-Portforward durch den
# bestehenden Kanal (technisch wie "ssh -L", per paramiko direct-tcpip
# Channel nachgebaut) - die Bibliothek denkt, sie spricht 127.0.0.1,
# tatsaechlich leitet die SSH-Verbindung das zur echten Router-IP im
# Belabox-LAN weiter.
# ---------------------------------------------------------------------------

_belabox_ssh_clients: dict = {}
_belabox_ssh_lock = threading.Lock()


def _get_belabox_ssh(profile: "DeviceProfile") -> paramiko.SSHClient:
    """Eine SSH-Verbindung zur Belabox, geteilt zwischen _belabox_snapshot
    (poll_belabox) UND allen Router-Abfragen - vermeidet mehrere parallele
    SSH-Logins zum selben Encoder (analog zum _get_router_client-Caching frueher
    fuer GL.iNet-Router direkt). transport.is_active() prueft vor jeder
    Wiederverwendung, ob die Verbindung noch lebt (z.B. nach Signalabriss)."""
    cache_key = f"{profile.host}|{profile.ssh_user}"
    with _belabox_ssh_lock:
        client = _belabox_ssh_clients.get(cache_key)
        if client is not None:
            transport = client.get_transport()
            if transport is not None and transport.is_active():
                return client
            try:
                client.close()
            except Exception:
                pass
            _belabox_ssh_clients.pop(cache_key, None)
        client = _ssh_connect(profile.host, profile.ssh_user, password=profile.ssh_password)
        _belabox_ssh_clients[cache_key] = client
        return client


def _drop_belabox_ssh(profile: "DeviceProfile"):
    cache_key = f"{profile.host}|{profile.ssh_user}"
    with _belabox_ssh_lock:
        client = _belabox_ssh_clients.pop(cache_key, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


class _ForwardHandler(socketserver.BaseRequestHandler):
    """Ein eingehender lokaler TCP-Connect wird 1:1 auf einen neuen
    direct-tcpip-Channel durch die bestehende Belabox-SSH-Verbindung
    gespiegelt - Standardmuster fuer "ssh -L" in reinem Python/paramiko
    (siehe paramiko-Demo forward.py), hier mit expliziten Attributen statt
    Server-Subclassing, damit mehrere Instanzen mit unterschiedlichem Router-
    Ziel nebeneinander laufen koennen.

    WICHTIG (Nutzerfehler 2026-08-31, live reproduziert): die urspruengliche
    Implementierung nutzte select.select([self.request, channel], ...) mit
    einem paramiko-Channel-Objekt darin. Das ist laut paramiko-Dokumentation
    ausdruecklich NICHT zuverlaessig - Channel.fileno() liefert nur einen
    Pseudo-Deskriptor zum Pollen, keinen echten, bidirektional
    select()-faehigen Socket (siehe paramiko-Issues #537/#695). Live
    reproduziert: TLS-Handshakes durch den Forward (z.B. GL.iNet-Router-API
    auf Port 443) schlugen zuverlaessig mit "ConnectionResetError: [Errno
    104] Connection reset by peer" waehrend ssl.do_handshake() fehl, obwohl
    derselbe TLS-Handshake direkt auf der Belabox (ohne den Forward)
    einwandfrei funktionierte - der select()-basierte Relay verlor/verzoegerte
    Daten in einer Richtung. Fix: zwei Threads mit blockierenden
    recv()/sendall()-Aufrufen statt select() - klassisches, robustes
    Pump-Muster, keine Abhaengigkeit von Channel.fileno()."""

    def handle(self):
        try:
            channel = self.server.ssh_transport.open_channel(
                "direct-tcpip",
                (self.server.remote_host, self.server.remote_port),
                self.request.getpeername(),
                timeout=10,
            )
        except Exception:
            return
        if channel is None:
            return

        def pump_socket_to_channel():
            try:
                while True:
                    data = self.request.recv(4096)
                    if len(data) == 0:
                        break
                    channel.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    channel.shutdown_write()
                except Exception:
                    pass

        def pump_channel_to_socket():
            try:
                while True:
                    data = channel.recv(4096)
                    if len(data) == 0:
                        break
                    self.request.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    self.request.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=pump_socket_to_channel, daemon=True)
        t2 = threading.Thread(target=pump_channel_to_socket, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        channel.close()
        self.request.close()


class _ForwardServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _open_local_forward(ssh: paramiko.SSHClient, remote_host: str, remote_port: int) -> "_ForwardServer":
    """Startet einen lokalen TCP-Listener auf einem freien Port (127.0.0.1),
    der jede Verbindung durch die gegebene SSH-Verbindung zu
    (remote_host, remote_port) im Netz DER BELABOX weiterleitet - das ist der
    eigentliche "Zugriff auf den Router ueber die Belabox statt direkt".
    Aufrufer MUSS server.shutdown() + server.server_close() aufrufen, wenn
    fertig (siehe _with_router_forward), sonst bleibt der Hintergrund-Thread
    haengen."""
    server = _ForwardServer(("127.0.0.1", 0), _ForwardHandler)
    server.ssh_transport = ssh.get_transport()
    server.remote_host = remote_host
    server.remote_port = remote_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class _with_router_forward:
    """Context-Manager: oeffnet fuer die Dauer des with-Blocks einen lokalen
    Portforward zur Router-IP (ueber die Belabox-SSH-Verbindung) und liefert
    die lokale (host, port)-Adresse, gegen die eine Bibliothek wie pyglinet
    oder tplinkrouterc6u stattdessen verbinden soll."""

    def __init__(self, belabox_profile: "DeviceProfile", remote_host: str, remote_port: int):
        self.belabox_profile = belabox_profile
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.server = None

    def __enter__(self):
        ssh = _get_belabox_ssh(self.belabox_profile)
        self.server = _open_local_forward(ssh, self.remote_host, self.remote_port)
        return ("127.0.0.1", self.server.server_address[1])

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def _belabox_ssh_exec(belabox_profile: "DeviceProfile", cmd: str, timeout: float = 12.0) -> tuple:
    """Fuehrt einen Shell-Befehl auf der Belabox aus (ueber die geteilte
    SSH-Verbindung) und liefert (exit_status, stdout, stderr) als Strings.
    Basis fuer curl-Abfragen an lokale Router-APIs (Netgear) und die
    vendor-unabhaengige Netzwerkqualitaets-Abfrage (iw/ethtool)."""
    ssh = _get_belabox_ssh(belabox_profile)
    try:
        _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return exit_status, out, err
    except Exception:
        # Verbindung koennte tot sein (z.B. Signalabriss seit letztem Poll) -
        # naechster Aufruf baut ueber _get_belabox_ssh automatisch neu auf.
        _drop_belabox_ssh(belabox_profile)
        raise


# ---------------------------------------------------------------------------
# Vendor-unabhaengige Netzwerkqualitaet: WLAN-Signalstaerke/-Bitrate ODER
# LAN-Linkgeschwindigkeit ZWISCHEN Belabox und Router, egal welcher
# Hersteller - reine 802.11/Ethernet-Radio-/Link-Werte, kein Router-API-Call,
# kostet 0 zusaetzliche Mobilfunk-Bandbreite (rein lokale Kernel-Abfrage auf
# der Belabox selbst). Basis fuer den "anderer Hersteller"-Pfad UND als
# Ergaenzung bei GL.iNet/Netgear/TP-Link (Nutzerwunsch 2026-08-31).
# ---------------------------------------------------------------------------


def _belabox_iface_for_router(belabox_profile: "DeviceProfile", router_profile: "DeviceProfile") -> Optional[str]:
    """Ermittelt, welches Netzwerk-Interface der Belabox im selben /24-Subnetz
    wie der konfigurierte Router haengt (per "ip -4 -br addr show" auf der
    Belabox) - Grundlage, um GENAU dieses Interface fuer iw/ethtool
    anzusprechen statt zu raten. profile.belabox_iface erlaubt einen
    manuellen Override, falls mehrere Interfaces im selben Subnetz haengen."""
    if router_profile.belabox_iface:
        return router_profile.belabox_iface
    if not router_profile.host:
        return None
    router_prefix = ".".join(router_profile.host.split(".")[:3])
    try:
        _, out, _ = _belabox_ssh_exec(belabox_profile, "ip -4 -br addr show 2>/dev/null")
    except Exception:
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[0] != "lo":
            ip = cols[2].split("/")[0]
            if ".".join(ip.split(".")[:3]) == router_prefix:
                return cols[0]
    return None


def _belabox_network_quality(belabox_profile: "DeviceProfile", router_profile: "DeviceProfile") -> dict:
    """Fragt auf der Belabox die Verbindungsqualitaet zu genau dem
    Interface ab, das zum konfigurierten Router gehoert - "iw dev <if> link"
    fuer WLAN (Signalstaerke in dBm, Bitrate, Frequenz), "ethtool <if>" als
    LAN-Fallback (Linkgeschwindigkeit, Duplex). Beides sind reine
    Kernel-/Treiber-Abfragen auf der Belabox selbst, kein Netzwerkverkehr."""
    result: dict = {"iface": None, "link_type": None}
    iface = _belabox_iface_for_router(belabox_profile, router_profile)
    result["iface"] = iface
    if not iface:
        result["error"] = "Konnte kein Belabox-Interface im Router-Subnetz finden"
        return result
    try:
        _, out, _ = _belabox_ssh_exec(
            belabox_profile,
            f"iw dev {iface} link 2>/dev/null; echo ---ETHTOOL---; ethtool {iface} 2>/dev/null",
        )
    except Exception as exc:
        result["error"] = str(exc)
        return result
    iw_part, _, ethtool_part = out.partition("---ETHTOOL---")
    if "Connected to" in iw_part:
        result["link_type"] = "wlan"
        m = re.search(r"signal:\s*(-?\d+)\s*dBm", iw_part)
        if m:
            result["rssi_dbm"] = int(m.group(1))
        m = re.search(r"rx bitrate:\s*([\d.]+)\s*MBit/s", iw_part)
        if m:
            result["rx_bitrate_mbps"] = float(m.group(1))
        m = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", iw_part)
        if m:
            result["tx_bitrate_mbps"] = float(m.group(1))
        m = re.search(r"freq:\s*(\d+)", iw_part)
        if m:
            result["freq_mhz"] = int(m.group(1))
        m = re.search(r"SSID:\s*(.+)", iw_part)
        if m:
            result["ssid"] = m.group(1).strip()
    elif "Speed:" in ethtool_part:
        result["link_type"] = "lan"
        m = re.search(r"Speed:\s*(\S+)", ethtool_part)
        if m:
            result["link_speed"] = m.group(1)
        m = re.search(r"Duplex:\s*(\S+)", ethtool_part)
        if m:
            result["duplex"] = m.group(1)
        m = re.search(r"Link detected:\s*(\S+)", ethtool_part)
        if m:
            result["link_detected"] = m.group(1) == "yes"
    else:
        result["error"] = f"Weder WLAN- noch LAN-Linkdaten fuer {iface} gefunden"
    return result


# ---------------------------------------------------------------------------
# Vendor-Tier 2: herstellerspezifische Signal-/Systemwerte, jeweils UEBER
# die Belabox erreicht (Portforward fuer GL.iNet/TP-Link-Bibliotheken,
# direkter curl-Aufruf auf der Belabox fuer Netgear).
# ---------------------------------------------------------------------------


def _router_snapshot_glinet(belabox_profile: "DeviceProfile", profile: "DeviceProfile") -> dict:
    """Wie zuvor (pyglinet JSON-RPC), nur dass die Bibliothek jetzt gegen
    einen lokalen Portforward zur Belabox spricht statt direkt gegen die
    (vom Dashboard aus gar nicht mehr erreichbare) Router-IP."""
    from pyglinet import GlInet

    result: dict = {}
    with _with_router_forward(belabox_profile, profile.host, 443) as (local_host, local_port):
        client = GlInet(
            url=f"https://{local_host}:{local_port}/rpc",
            username=profile.ssh_user or "root",
            password=profile.ssh_password,
            verify_ssl_certificate=False,
            keep_alive=False,
        )
        try:
            client.login()
            result["api_ok"] = True
            try:
                info = client.request("call", ["modem", "get_info", {}])
                modems = (info or {}).get("result", {}).get("modems", [])
                if modems:
                    bus = modems[0]["bus"]
                    cells = client.request("call", ["modem", "get_cells_info", {"bus": bus}])
                    result["modem_name"] = modems[0].get("name")
                    result["cells"] = (cells or {}).get("result", {}).get("cells", [])
            except Exception as exc:
                result["modem_error"] = str(exc)
            try:
                status = client.request("call", ["system", "get_status", {}])
                sys_info = (status or {}).get("result", {}).get("system", {})
                result["cpu_temp"] = (sys_info.get("cpu") or {}).get("temperature")
                result["uptime_seconds"] = sys_info.get("uptime")
                result["memory_free"] = sys_info.get("memory_free")
                result["memory_total"] = sys_info.get("memory_total")
                result["load_average"] = sys_info.get("load_average")
                client_info = (status or {}).get("result", {}).get("client", [])
                if client_info:
                    result["clients_wireless"] = client_info[0].get("wireless_total")
                    result["clients_cable"] = client_info[0].get("cable_total")
            except Exception as exc:
                result["system_error"] = str(exc)
            if profile.api_method:
                try:
                    params = json.loads(profile.api_params or "[]")
                    result["custom_api_ok"] = True
                    result["custom_api_result"] = client.request(profile.api_method, params)
                except Exception as exc:
                    result["custom_api_ok"] = False
                    result["custom_api_error"] = str(exc)
        except Exception as exc:
            result["api_ok"] = False
            result["api_error"] = str(exc)
    return result


def _router_snapshot_netgear(belabox_profile: "DeviceProfile", profile: "DeviceProfile") -> dict:
    """Netgear-Nighthawk-M-Serie: community-dokumentiertes internes JSON
    unter /api/model.json (kein Login fuer die Basisdaten noetig, siehe
    z.B. github.com/motamman/zennora-wlan, github.com/jacobhere/netgear-m5).
    Der eigentliche HTTP-Request laeuft als curl AUF der Belabox (Ziel ist
    die lokale Router-IP im Belabox-Netz) - das JSON kommt komplett per SSH
    zurueck, wird HIER (im Backend, nicht auf der Belabox) gefiltert. Der
    rohe Dump kann mehrere zehn KB gross sein; da der Poll bewusst mit
    eigenem, entkoppeltem Intervall laeuft (POLL_INTERVAL_ROUTER_VENDOR),
    ist die zusaetzliche Last auf dem gebondeten Mobilfunk-Uplink trotzdem
    vernachlaessigbar (siehe Nutzergespraech 2026-08-31 zur Bandbreite)."""
    result: dict = {}
    url = f"http://{profile.host}/api/model.json?internalapi=1&x=42"
    try:
        exit_status, out, err = _belabox_ssh_exec(
            belabox_profile, f"curl -s --max-time 8 '{url}'"
        )
    except Exception as exc:
        result["api_ok"] = False
        result["api_error"] = str(exc)
        return result
    if exit_status != 0 or not out.strip():
        result["api_ok"] = False
        result["api_error"] = err.strip() or "Leere Antwort von model.json (curl auf der Belabox)"
        return result
    try:
        data = json.loads(out)
    except ValueError as exc:
        result["api_ok"] = False
        result["api_error"] = f"Ungueltiges JSON von model.json: {exc}"
        return result
    result["api_ok"] = True
    wwan = data.get("wwan") or {}
    wwanadv = data.get("wwanadv") or {}
    general = data.get("general") or {}
    power = data.get("power") or {}
    result["signal_strength_dbm"] = wwan.get("signalStrength") or wwanadv.get("rxLevel")
    result["rsrp"] = wwanadv.get("rsrp")
    result["rsrq"] = wwanadv.get("rsrq")
    result["sinr"] = wwanadv.get("sinr")
    result["network_type"] = wwan.get("currentNWserviceType") or wwanadv.get("curBand")
    result["connection_state"] = wwan.get("connection")
    result["battery_level"] = ((power.get("battery") or {}).get("level"))
    result["device_temp"] = general.get("devTemperature") or data.get("devTemperature")
    result["uptime_seconds"] = general.get("upTime")
    return result


def _router_snapshot_tplink(belabox_profile: "DeviceProfile", profile: "DeviceProfile") -> dict:
    """TP-Link (inkl. Mercusys) mobile Router per tplinkrouterc6u (PyPI,
    unterstuetzt explizit LTE-Router mit get_lte_status: RSRP/RSRQ/SNR).
    Wie bei GL.iNet ueber einen Portforward zur Belabox angesprochen, da die
    Bibliothek einen eigenen Crypto-Login-Handshake macht, den ein simpler
    curl-Aufruf nicht nachbilden kann."""
    result: dict = {}
    try:
        from tplinkrouterc6u import TplinkRouter
    except ImportError:
        result["api_ok"] = False
        result["api_error"] = "tplinkrouterc6u nicht installiert (siehe requirements.txt)"
        return result
    with _with_router_forward(belabox_profile, profile.host, 80) as (local_host, local_port):
        try:
            router = TplinkRouter(f"http://{local_host}:{local_port}", profile.ssh_password or "")
            router.authorize()
            result["api_ok"] = True
            try:
                lte = router.get_lte_status()
                result["rsrp"] = lte.rsrp
                result["rsrq"] = lte.rsrq
                result["sinr"] = lte.snr
                result["signal_level"] = lte.sig_level
                result["network_type"] = lte.network_type_info
                result["isp_name"] = lte.isp_name
                result["sim_status"] = lte.sim_status_info
            except Exception as exc:
                result["modem_error"] = str(exc)
            try:
                status = router.get_status()
                result["clients_wireless"] = getattr(status, "wired_total", None)
                result["clients_cable"] = getattr(status, "wired_total", None)
                result["cpu_usage"] = getattr(status, "cpu_usage", None)
                result["mem_usage"] = getattr(status, "mem_usage", None)
            except Exception as exc:
                result["system_error"] = str(exc)
            try:
                router.logout()
            except Exception:
                pass
        except Exception as exc:
            result["api_ok"] = False
            result["api_error"] = str(exc)
    return result


def _get_router_client(profile: "DeviceProfile"):
    from pyglinet import GlInet

    cache_key = f"{profile.host}|{profile.ssh_user or 'root'}"
    with _router_clients_lock:
        client = _router_clients.get(cache_key)
        if client is None:
            client = GlInet(
                url=f"https://{profile.host}/rpc",
                username=profile.ssh_user or "root",
                password=profile.ssh_password,
                verify_ssl_certificate=False,
                keep_alive=False,
            )
            _router_clients[cache_key] = client
        return client


def _drop_router_client(profile: "DeviceProfile"):
    cache_key = f"{profile.host}|{profile.ssh_user or 'root'}"
    with _router_clients_lock:
        _router_clients.pop(cache_key, None)


def _tcp_ping(host: str, port: int = 22, timeout: float = 2.0) -> bool:
    """Ping-Ersatz auf TCP-Ebene: prueft reine Netzwerk-Erreichbarkeit,
    unabhaengig von SSH-Login oder Router-API (die koennen aus anderen
    Gruenden fehlschlagen, ohne dass der Router wirklich "weg" ist)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _router_snapshot(belabox_profile: "DeviceProfile", profile: "DeviceProfile") -> dict:
    """Vendor-Weiche fuer die Router-Abfrage: seit 2026-08-31 laeuft JEDER
    Zugriff ausschliesslich ueber die Belabox (siehe Modulkommentar oben bei
    _get_belabox_ssh) - kein Direktzugriff vom Dashboard mehr, auch nicht
    fuer die Erreichbarkeitspruefung (frueher _tcp_ping direkt gegen den
    Router; die Belabox selbst ist ja bereits ueber den WireGuard-Tunnel
    erreichbar und dient hier als Sonde ins Router-Netz).
    "ssh_ok"/"ssh_error" im Ergebnis bleiben aus Kompatibilitaetsgruenden mit
    dem restlichen Code (Findings-Engine, Frontend-Karten) bestehen, meinen
    aber jetzt "ueber die Belabox erreichbar", nicht mehr direktes SSH zum
    Router selbst."""
    result: dict = {"vendor": profile.vendor}
    if not belabox_profile.host:
        result["ssh_ok"] = False
        result["ssh_error"] = "Belabox nicht konfiguriert - Router-Zugriff laeuft ausschliesslich ueber sie"
        return result

    # Erreichbarkeit: TCP-Connect zum Router AUF der Belabox versucht (nicht
    # vom Dashboard aus - der Router ist von hier aus gar nicht routbar).
    try:
        _, out, _ = _belabox_ssh_exec(
            belabox_profile,
            f"timeout 3 bash -c 'echo > /dev/tcp/{profile.host}/80' 2>/dev/null && echo REACHABLE || echo UNREACHABLE",
        )
        ping_ok = "REACHABLE" in out
    except Exception as exc:
        result["ssh_ok"] = False
        result["ssh_error"] = f"Belabox nicht erreichbar (Voraussetzung fuer Router-Zugriff): {exc}"
        result["ping_ok"] = False
        return result
    result["ping_ok"] = ping_ok
    if not ping_ok:
        result["ssh_ok"] = False
        result["ssh_error"] = "Router laut Belabox nicht erreichbar (kein TCP-Connect auf Port 80)."
        result["api_ok"] = False
        result["api_error"] = "Erreichbarkeitspruefung ueber die Belabox fehlgeschlagen - Vendor-API uebersprungen."
        return result
    result["ssh_ok"] = True  # "ueber die Belabox erreichbar" (siehe Docstring)

    # Vendor-spezifische Telemetrie (Signal/System) - "other" bewusst
    # ausgenommen, siehe ROUTER_VENDORS-Kommentar bei DeviceProfile.
    try:
        if profile.vendor == "glinet":
            result.update(_router_snapshot_glinet(belabox_profile, profile))
        elif profile.vendor == "netgear":
            result.update(_router_snapshot_netgear(belabox_profile, profile))
        elif profile.vendor == "tplink":
            result.update(_router_snapshot_tplink(belabox_profile, profile))
        # vendor == "other": keine Vendor-API, nur die Netzwerkqualitaet unten.
    except Exception as exc:
        result["api_ok"] = False
        result["api_error"] = str(exc)

    # Netzwerkqualitaet (WLAN-RSSI/Bitrate oder LAN-Speed) - fuer ALLE
    # Hersteller inkl. "other" (Nutzerwunsch 2026-08-31), da sie komplett
    # vendor-unabhaengig ueber die Belabox-Netzwerkschnittstelle laeuft.
    try:
        result["network_quality"] = _belabox_network_quality(belabox_profile, profile)
    except Exception as exc:
        result["network_quality"] = {"error": str(exc)}

    return result


async def poll_router(session: Session, belabox_profile: DeviceProfile, profile: DeviceProfile, label: str):
    if not profile.host:
        return
    _actively_polled_routers.add(label)
    try:
        await _poll_router_loop(session, belabox_profile, profile, label)
    finally:
        _actively_polled_routers.discard(label)


async def _poll_router_loop(session: Session, belabox_profile: DeviceProfile, profile: DeviceProfile, label: str):
    while not session.stopped.is_set():
        try:
            data = await _router_snapshot_with_timeout(belabox_profile, profile)
            _last_router_snapshot[label] = {
                "ok": data.get("ssh_ok"), "error": data.get("ssh_error"),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "data": data,
            }
            session.emit(label, data)
        except Exception as exc:
            session.emit(label, {"ok": False, "error": str(exc)})
        await asyncio.sleep(POLL_INTERVAL_ROUTER)


# ---------- Verbindungstest pro Geraet (unabhaengig von einer laufenden Session) ----------


def _connection_test(profile: DeviceProfile) -> dict:
    """Nur noch fuer die Belabox selbst gedacht (echtes SSH). Fuer Router
    siehe _router_connection_test() - die haben seit 2026-08-31 keinen
    Direktzugriff mehr, "Test Connection" muss also durch die Belabox gehen."""
    if not profile.host:
        return {"ok": False, "error": "Keine IP/Host angegeben"}
    start = time.monotonic()
    try:
        ssh = _ssh_connect(profile.host, profile.ssh_user, password=profile.ssh_password)
        try:
            _, stdout, stderr = ssh.exec_command("echo ok")
            out = stdout.read().decode(errors="replace").strip()
            err = stderr.read().decode(errors="replace").strip()
            elapsed_ms = round((time.monotonic() - start) * 1000)
            if out == "ok":
                return {"ok": True, "elapsed_ms": elapsed_ms}
            return {"ok": False, "error": err or f"unerwartete Antwort: {out!r}", "elapsed_ms": elapsed_ms}
        finally:
            ssh.close()
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return {"ok": False, "error": str(exc), "elapsed_ms": elapsed_ms}


def _router_connection_test(belabox_profile: DeviceProfile, profile: DeviceProfile) -> dict:
    """"Test Connection" fuer einen Router-Eintrag: ruft denselben
    _router_snapshot()-Pfad auf wie der normale Poll (inkl. Vendor-API +
    Netzwerkqualitaet ueber die Belabox), aber einmalig und mit Zeitmessung -
    so testet der Knopf im Frontend GENAU den Pfad, der auch im Dauerbetrieb
    genutzt wird, statt eines separaten (potenziell abweichenden) Checks."""
    if not profile.host:
        return {"ok": False, "error": "Keine Router-IP angegeben"}
    if not belabox_profile.host:
        return {"ok": False, "error": "Belabox nicht konfiguriert - ohne sie ist kein Router erreichbar"}
    start = time.monotonic()
    try:
        data = _router_snapshot(belabox_profile, profile)
        elapsed_ms = round((time.monotonic() - start) * 1000)
        if data.get("ssh_ok"):
            return {"ok": True, "elapsed_ms": elapsed_ms, "detail": data}
        return {"ok": False, "error": data.get("ssh_error") or data.get("api_error") or "unbekannter Fehler", "elapsed_ms": elapsed_ms}
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000)
        return {"ok": False, "error": str(exc), "elapsed_ms": elapsed_ms}


@app.post("/device/{which}/test")
async def device_test(which: str, profile: DeviceProfile):
    if which != "belabox" and not re.fullmatch(r"router\d+", which):
        raise HTTPException(status_code=400, detail="Unbekanntes Geraet")
    if which == "belabox" and NOALBS_MODE == "local_docker":
        # Nur auf der Appliance: Frontend schickt fuer Belabox dort keinen
        # Host mehr mit (siehe BELABOX_HOST oben) - hier erzwingen, sonst
        # testet dies gegen "". Im Produktiv-Setup (ssh_vm) bleibt der vom
        # Frontend gesendete Host unveraendert.
        profile.host = BELABOX_HOST
    if which == "belabox":
        return await asyncio.to_thread(_connection_test, profile)
    cfg = load_config()
    return await asyncio.to_thread(_router_connection_test, cfg.belabox, profile)


def _local_host_subnets() -> set[str]:
    """Ermittelt die eigenen /24-Subnetze DIESES Rechners (Mini-PC, auf dem
    das Dashboard laeuft) - dank network_mode:host im Docker-Compose-Setup
    sieht der Container dieselben Netzwerkinterfaces wie der Host selbst.
    Grundlage fuer den Heimnetz-Filter in _belabox_discover_devices()."""
    subnets: set[str] = set()
    try:
        result = subprocess.run(
            ["ip", "-4", "-br", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            cols = line.split()
            if len(cols) < 3 or cols[0] == "lo" or cols[0].startswith("wg") or cols[0].startswith("docker"):
                continue
            ip = cols[2].split("/")[0]
            subnets.add(".".join(ip.split(".")[:3]))
    except Exception:
        pass
    return subnets


def _belabox_discover_devices(belabox_profile: "DeviceProfile") -> list[dict]:
    """Listet NUR gerade aktiv erreichbare Geraete im lokalen Netz DER
    BELABOX auf (Router/Hotspots), damit das Frontend eine Auswahl statt
    eines Freitext-Feldes fuer die Router-IP anbieten kann (Nutzerwunsch
    2026-08-31). Bewusst STRENG gefiltert (Nutzerkorrektur 2026-08-31: eine
    erste Version zeigte auch laengst nicht mehr aktive "STALE"-Eintraege
    aus dem ARP-Cache, das war zu viel Rauschen) - nur Geraete, die der
    frische Ping-Sweep unten TATSAECHLICH JETZT beantwortet haben, landen
    in der Rueckgabe.

    Ablauf: 1) bekannte Subnetze der Belabox-Interfaces ermitteln (ausser
    wg0/lo), 2) alle bereits in der ARP-Tabelle bekannten IPs PLUS eine
    kurze Liste ueblicher Router-/Gateway-Adressen (.1-.5, .100, .101,
    .200, .254) parallel anpingen - das aktualisiert den ARP-Eintrag auf
    "REACHABLE" (frisch bestaetigt) oder "STALE"/"FAILED", je nachdem ob
    das Geraet gerade wirklich antwortet, 3) ARP-Tabelle erneut lesen und
    NUR "REACHABLE"-Eintraege zurueckgeben. IPv6 wird ignoriert (Router-
    Konfiguration ist durchgaengig IPv4)."""
    if not belabox_profile.host:
        return []

    try:
        _, iface_out, _ = _belabox_ssh_exec(
            belabox_profile, "ip -4 -br addr show 2>/dev/null", timeout=8
        )
    except Exception:
        return []

    subnets = []
    for line in iface_out.splitlines():
        cols = line.split()
        if len(cols) < 3 or cols[0] in ("lo", "wg0") or cols[0].startswith("wg"):
            continue
        ip = cols[2].split("/")[0]
        prefix = ".".join(ip.split(".")[:3])
        if prefix not in subnets:
            subnets.append(prefix)

    # Das Heimnetz-Subnetz herausfiltern, in dem dieser Mini-PC selbst
    # haengt (Nutzerentscheidung 2026-08-31): beide Belabox-Interfaces
    # (z.B. Heimnetz-WLAN und mobiler Router/Hotspot) sind technisch
    # gleichberechtigte Netzwerkverbindungen, es gibt kein Merkmal, das
    # "Router" von "normales WLAN" unterscheidet - ausser eben der
    # Tatsache, dass der Nutzer selbst (und damit dieser Mini-PC) im
    # Heimnetz sitzt, waehrend die Belabox das ZUSAETZLICH auch tut, wenn
    # sie sich gerade dort befindet. Ergebnis: Geraete aus demselben
    # Subnetz wie dieser Mini-PC werden nicht vorgeschlagen (das sind die
    # eigenen Heimnetz-Nachbarn, kein "Router" im Sinne dieses Features).
    local_subnets = _local_host_subnets()
    subnets = [s for s in subnets if s not in local_subnets]

    if not subnets:
        return []

    # Bereits bekannte IPs (auch laengst STALE) mit in den Sweep aufnehmen,
    # damit ein zuvor gesehener, gerade wieder aktiver Router nicht nur
    # ueber die Standard-Adressraten gefunden werden muss.
    known_ips: set[str] = set()
    try:
        _, prior_neigh, _ = _belabox_ssh_exec(
            belabox_profile, "ip neigh show 2>/dev/null", timeout=8
        )
        for line in prior_neigh.splitlines():
            cols = line.split()
            if not cols or ":" in cols[0]:
                continue
            ip = cols[0]
            # WICHTIG (Nutzerkorrektur 2026-08-31, live reproduziert): ohne
            # diesen Subnetz-Check schleppte known_ips auch bereits bekannte
            # Heimnetz-IPs (aus dem oben herausgefilterten subnets-Bereich)
            # weiter mit - die Ausgabe zeigte dadurch trotz Filter weiterhin
            # alle Heimnetz-Nachbarn, nicht nur Geraete im Router-Netz.
            if ".".join(ip.split(".")[:3]) not in subnets:
                continue
            known_ips.add(ip)
    except Exception:
        pass

    guess_ips = {
        f"{prefix}.{i}"
        for prefix in subnets
        for i in (1, 2, 3, 4, 5, 100, 101, 200, 254)
    }
    sweep_ips = known_ips | guess_ips

    # Ping-Sweep direkt auf der Belabox - aktualisiert die ARP-Tabelle auf
    # den JETZT tatsaechlich gemessenen Zustand, ohne zusaetzliche Software
    # auf der Belabox zu benoetigen (nmap o.ae. ist auf dem Belabox-Image
    # nicht vorinstalliert, ping schon).
    #
    # WICHTIG (Nutzerkorrektur 2026-08-31, live reproduziert): ALLE IPs auf
    # einmal parallel per "&" anzupingen liess auf einer Belabox mit
    # MEHREREN aktiven Interfaces (hier wlan0 mit vielen Zieladressen +
    # wlan2 mit dem eigentlichen Router) den Router-Eintrag ausfallen -
    # einzeln gepingt antwortete derselbe Router zuverlaessig sofort mit
    # REACHABLE. Vermutete Ursache: zu viele gleichzeitige ARP-Anfragen
    # ueber mehrere Funkschnittstellen gleichzeitig ueberlasten das
  # (WLAN-)Timing kurzzeitig. Fix: in kleinen Gruppen von 4 statt alles auf
    # einmal, mit kurzer Pause dazwischen, plus 1s Wartezeit nach dem
    # letzten Batch, bevor die ARP-Tabelle final gelesen wird (State-
    # Uebergang REACHABLE dauert einen Moment).
    ip_list = sorted(sweep_ips)
    batch_size = 4
    sweep_script_lines = []
    for start in range(0, len(ip_list), batch_size):
        batch = ip_list[start:start + batch_size]
        batch_cmd = " & ".join(f"ping -c1 -W1 {ip} >/dev/null 2>&1" for ip in batch)
        sweep_script_lines.append(f"({batch_cmd}); wait")
    sweep_script = "; ".join(sweep_script_lines) + "; sleep 1"
    try:
        _belabox_ssh_exec(belabox_profile, sweep_script, timeout=8 + len(ip_list))
    except Exception:
        pass

    try:
        _, neigh_out, _ = _belabox_ssh_exec(
            belabox_profile, "ip neigh show 2>/dev/null", timeout=8
        )
    except Exception:
        return []

    devices: list[dict] = []
    seen_ips: set[str] = set()
    for line in neigh_out.splitlines():
        cols = line.split()
        if not cols:
            continue
        ip = cols[0]
        if ":" in ip:
            continue  # IPv6 ueberspringen
        if ip in seen_ips:
            continue
        state = cols[-1] if cols else ""
        # Bewusst NUR "REACHABLE" (frisch vom obigen Sweep bestaetigt) -
        # "STALE"/"DELAY"/"PROBE"/"PERMANENT" koennen laengst abgeschaltete
        # Geraete sein, deren alter ARP-Eintrag noch im Kernel-Cache haengt.
        if state != "REACHABLE":
            continue
        # Nochmals gegen den (bereits um das eigene Heimnetz bereinigten)
        # subnets-Bereich pruefen - "ip neigh show" liest die KOMPLETTE
        # Kernel-ARP-Tabelle, nicht nur die vom obigen Sweep beruehrten
        # Adressen. Ohne diesen zweiten Check tauchten weiterhin bereits
        # anderweitig REACHABLE gewordene Heimnetz-Nachbarn in der Ausgabe
        # auf (Nutzerkorrektur 2026-08-31, live reproduziert).
        if ".".join(ip.split(".")[:3]) not in subnets:
            continue
        mac = None
        if "lladdr" in cols:
            mac = cols[cols.index("lladdr") + 1]
        iface = cols[2] if len(cols) > 2 and cols[1] == "dev" else None
        seen_ips.add(ip)
        devices.append({"ip": ip, "mac": mac, "iface": iface, "state": state})

    devices.sort(key=lambda d: tuple(int(p) for p in d["ip"].split(".")))
    return devices


@app.get("/belabox/discover-devices")
async def belabox_discover_devices():
    cfg = load_config()
    if NOALBS_MODE == "local_docker":
        belabox_profile = cfg.belabox.model_copy(update={"host": BELABOX_HOST})
    else:
        belabox_profile = cfg.belabox
    if not belabox_profile.host:
        return {"devices": [], "error": "Belabox nicht konfiguriert"}
    devices = await asyncio.to_thread(_belabox_discover_devices, belabox_profile)
    return {"devices": devices}


# ---------- Debug: rohe GL.iNet-API-Methode live testen (fuer /discover) ----------


class RouterProbeRequest(BaseModel):
    method: str
    params_json: str = "[]"


@app.post("/router/{index}/discover")
def router_discover(index: int, req: RouterProbeRequest):
    """Ruft eine beliebige JSON-RPC-Methode am eingeschalteten Router live auf,
    damit wir einmalig die richtige Methode fuer Signal-/Modem-Werte finden
    koennen (Methodennamen sind nicht offiziell auf Englisch dokumentiert).
    Nur fuer GL.iNet-Router sinnvoll (JSON-RPC-Methodennamen) - laeuft seit
    2026-08-31 ueber einen Portforward durch die Belabox wie der normale Poll."""
    cfg = load_config()
    if index < 0 or index >= len(cfg.routers) or not cfg.routers[index].host:
        raise HTTPException(status_code=400, detail=f"Kein Router mit Index {index} konfiguriert")
    profile = cfg.routers[index]
    if not cfg.belabox.host:
        raise HTTPException(status_code=400, detail="Belabox nicht konfiguriert - ohne sie ist kein Router erreichbar")
    try:
        from pyglinet import GlInet

        with _with_router_forward(cfg.belabox, profile.host, 443) as (local_host, local_port):
            client = GlInet(
                url=f"https://{local_host}:{local_port}/rpc",
                username=profile.ssh_user or "root",
                password=profile.ssh_password,
                verify_ssl_certificate=False,
            ).login()
            params = json.loads(req.params_json or "[]")
            result = client.request(req.method, params)
            return {"ok": True, "result": result}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _router_reboot(belabox_profile: DeviceProfile, profile: DeviceProfile) -> None:
    # Methode/Parameter aus pyglinets gebuendelter GL.iNet-API-Beschreibung
    # verifiziert (api_description.json, system/reboot: in_example
    # {"method":"call","params":["","system","reboot"]}) - dieselbe
    # ["system", "reboot", {}]-Form wie das bereits genutzte "get_status".
    # Nur fuer vendor=="glinet" verfuegbar (JSON-RPC-Methode); ueber
    # Portforward durch die Belabox, wie der normale Poll seit 2026-08-31.
    if profile.vendor != "glinet":
        raise ValueError("Fernneustart ist nur fuer GL.iNet-Router implementiert")
    from pyglinet import GlInet

    with _with_router_forward(belabox_profile, profile.host, 443) as (local_host, local_port):
        client = GlInet(
            url=f"https://{local_host}:{local_port}/rpc",
            username=profile.ssh_user or "root",
            password=profile.ssh_password,
            verify_ssl_certificate=False,
        )
        client.login()
        client.request("call", ["system", "reboot", {}])


@app.post("/router/{index}/reboot")
async def router_reboot(index: int):
    cfg = load_config()
    if index < 0 or index >= len(cfg.routers) or not cfg.routers[index].host:
        raise HTTPException(status_code=400, detail=f"Kein Router mit Index {index} konfiguriert")
    if not cfg.belabox.host:
        raise HTTPException(status_code=400, detail="Belabox nicht konfiguriert - ohne sie ist kein Router erreichbar")
    profile = cfg.routers[index]
    try:
        await asyncio.to_thread(_router_reboot, cfg.belabox, profile)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        # Nach einem Reboot ist die gecachte Session ohnehin hinfaellig - der
        # naechste Poll baut automatisch eine frische auf, statt gegen die
        # tote Verbindung zu laufen.
        _drop_router_client(profile)
    return {"ok": True}


# ---------- Test-Session steuern ----------


def _start_session() -> Session:
    """Gemeinsame Startlogik fuer /test/start UND den Auto-Start beim
    Hochfahren des Containers (siehe start_default_session) - eine Session
    soll dauerhaft laufen, unabhaengig davon ob/wann ein Browser das
    Dashboard oeffnet. Sonst faengt ein Container-Neustart (z.B. durch ein
    Deployment) niemanden auf, solange kein Browser-Tab neu geladen wird -
    genau das ist am 2026-08-19/20 einmal live passiert (Session tot nach
    einem Redeploy, Browser-Tab blieb offen, aber niemand hat sie neu
    gestartet -> die Auto-Reload-Funktion konnte einen echten Ausfall nicht
    auffangen)."""
    global current_session, _belabox_live_last_update
    cfg = load_config()
    session = Session(cfg)
    session.tasks.append(asyncio.create_task(session.broadcaster()))
    session.tasks.append(asyncio.create_task(poll_srtla(session)))
    session.tasks.append(asyncio.create_task(poll_obs(session)))
    session.tasks.append(asyncio.create_task(poll_obs_scene_items(session)))
    session.tasks.append(asyncio.create_task(poll_obs_preview(session)))
    session.tasks.append(asyncio.create_task(poll_noalbs(session)))
    session.tasks.append(asyncio.create_task(poll_belabox(session, cfg.belabox)))
    belabox_live_task = asyncio.create_task(poll_belabox_live(session, cfg.belabox))
    session.tasks.append(belabox_live_task)
    _belabox_live_last_update = time.monotonic()
    session.tasks.append(asyncio.create_task(_belabox_live_watchdog(session, cfg.belabox, belabox_live_task)))
    for i, profile in enumerate(cfg.routers):
        session.tasks.append(asyncio.create_task(poll_router(session, cfg.belabox, profile, f"router{i+1}")))
    current_session = session
    return session


@app.post("/test/start")
async def start_test():
    if current_session is not None:
        raise HTTPException(status_code=409, detail="Es laeuft bereits ein Test")
    session = _start_session()
    return {"ok": True, "session_id": session.id}


@app.post("/test/stop")
async def stop_test():
    global current_session
    if current_session is None:
        raise HTTPException(status_code=409, detail="Es laeuft kein Test")
    await current_session.stop()
    session_id = current_session.id
    current_session = None
    return {"ok": True, "session_id": session_id}


class MarkerRequest(BaseModel):
    label: str = "Ton verzerrt"


@app.post("/test/mark")
def mark_event(req: MarkerRequest):
    if current_session is None:
        raise HTTPException(status_code=409, detail="Es laeuft kein Test")
    current_session.emit("marker", {"label": req.label})
    return {"ok": True}


@app.get("/test/status")
def test_status():
    if current_session is None:
        return {"running": False}
    return {"running": True, "session_id": current_session.id}


def _obs_call(method: str) -> dict:
    try:
        with _obs_client_ctx() as client:
            getattr(client, method)()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/obs/stream/start")
def obs_stream_start():
    return _obs_call("start_stream")


@app.post("/obs/stream/stop")
def obs_stream_stop():
    return _obs_call("stop_stream")


@app.post("/obs/record/start")
def obs_record_start():
    return _obs_call("start_record")


@app.post("/obs/record/stop")
def obs_record_stop():
    return _obs_call("stop_record")


async def _belabox_call(action: str, overrides: Optional[dict] = None) -> dict:
    cfg = load_config()
    profile = cfg.belabox
    if not profile.host:
        raise HTTPException(status_code=409, detail="Kein Belabox-Host konfiguriert")
    try:
        return await _belabox_ws_command(profile.host, profile.ui_password, action, overrides)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class BelaboxStartRequest(BaseModel):
    max_br: Optional[int] = None
    pipeline: Optional[str] = None


@app.post("/belabox/stream/start")
async def belabox_stream_start(req: BelaboxStartRequest = BelaboxStartRequest()):
    overrides = {}
    overrides.update(_auto_srtla_target_overrides())
    if req.max_br is not None:
        overrides["max_br"] = req.max_br
    if req.pipeline is not None:
        overrides["pipeline"] = req.pipeline
    return await _belabox_call("start", overrides or None)


@app.post("/belabox/stream/stop")
async def belabox_stream_stop():
    return await _belabox_call("stop")


class BelaboxNetifRequest(BaseModel):
    name: str
    ip: str
    enabled: bool


@app.post("/belabox/netif")
async def belabox_set_netif(req: BelaboxNetifRequest):
    cfg = load_config()
    profile = cfg.belabox
    if not profile.host:
        raise HTTPException(status_code=409, detail="Kein Belabox-Host konfiguriert")
    try:
        result = await _belabox_set_netif(profile.host, profile.ui_password, req.name, req.ip, req.enabled)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # handleNetif() in belaUI.js bestaetigt NUR an den Absender selbst, nicht
    # per Broadcast - der dauerhafte Zuhoerer (poll_belabox_live) wuerde diese
    # Aenderung sonst nie sehen (siehe Kommentar in _belabox_set_netif). Den
    # geteilten Zustand hier direkt mit-aktualisieren (nicht nur einmalig
    # emitten), damit ein spaeteres Update durch den Zuhoerer selbst nicht
    # wieder den alten Bonding-Stand zurueckschreibt.
    _belabox_live_state["interfaces"] = result.get("interfaces", {})
    if result.get("max_br") is not None:
        _belabox_live_state["max_br"] = result.get("max_br")
    if result.get("is_streaming") is not None:
        _belabox_live_state["is_streaming"] = result.get("is_streaming")
    if current_session is not None:
        current_session.emit("belabox_live", {
            "ok": True,
            **_belabox_live_state,
        })
    return result


class BelaboxBitrateRequest(BaseModel):
    max_br: int


@app.post("/belabox/bitrate")
async def belabox_set_bitrate(req: BelaboxBitrateRequest):
    cfg = load_config()
    profile = cfg.belabox
    if not profile.host:
        raise HTTPException(status_code=409, detail="Kein Belabox-Host konfiguriert")
    if not (500 <= req.max_br <= 12000):
        raise HTTPException(status_code=400, detail="Bitrate muss zwischen 500 und 12000 kbps liegen")
    try:
        return await _belabox_set_bitrate(profile.host, profile.ui_password, req.max_br)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


async def _noalbs_service_call(action: str) -> dict:
    ok, err = await asyncio.to_thread(_noalbs_service_command, action)
    if not ok:
        raise HTTPException(status_code=502, detail=err)
    return {"ok": True}


@app.post("/noalbs/service/start")
async def noalbs_service_start():
    return await _noalbs_service_call("start")


@app.post("/noalbs/service/stop")
async def noalbs_service_stop():
    return await _noalbs_service_call("stop")


@app.get("/noalbs/thresholds")
async def noalbs_get_thresholds():
    try:
        thresholds = await asyncio.to_thread(_noalbs_get_thresholds)
        return {"ok": True, "thresholds": thresholds}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class NoalbsThresholdRequest(BaseModel):
    key: str
    value: Optional[int] = None


@app.post("/noalbs/thresholds")
async def noalbs_set_threshold(req: NoalbsThresholdRequest):
    if req.key not in _NOALBS_THRESHOLD_KEYS:
        raise HTTPException(status_code=400, detail="Unbekannter Schwellenwert")
    try:
        thresholds = await asyncio.to_thread(_noalbs_write_threshold, req.key, req.value)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # NOALBS liest config.json nur einmal beim Start - im Binary gibt es keine
    # Strings zu SIGHUP/inotify/reload (per strings-Analyse verifiziert), ein
    # Neustart ist also zwingend noetig, damit der neue Wert tatsaechlich
    # greift. Im lokalen Docker-Modus bedeuten "stop"/"start" SIGSTOP/SIGCONT
    # (siehe _noalbs_service_command_local_docker) - reines Pausieren/
    # Fortsetzen liesse NOALBS die Config NIE neu einlesen. Nur "restart"
    # (SIGTERM, supervisord respawnt automatisch) erreicht dort tatsaechlich
    # einen Neustart; im SSH-VM-Modus bleibt das bewaehrte stop+start
    # (echtes systemctl stop/start) unveraendert.
    if NOALBS_MODE == "local_docker":
        ok, err = await asyncio.to_thread(_noalbs_service_command_local_docker, "restart")
    else:
        ok, err = await asyncio.to_thread(_noalbs_service_command, "stop")
        if ok:
            ok, err = await asyncio.to_thread(_noalbs_service_command, "start")
    if not ok:
        raise HTTPException(
            status_code=502,
            detail=f"Wert gespeichert, aber Neustart fehlgeschlagen: {err}",
        )
    return {"ok": True, "thresholds": thresholds}


@app.get("/noalbs/settings", response_model=NoalbsConfig)
async def noalbs_get_settings():
    try:
        return await asyncio.to_thread(_noalbs_get_settings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/noalbs/settings")
async def noalbs_set_settings(settings: NoalbsConfig):
    try:
        await asyncio.to_thread(_noalbs_write_settings, settings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Selbe Begruendung wie bei /noalbs/thresholds oben: NOALBS liest
    # config.json nur einmal beim Start, ein Neustart ist zwingend noetig.
    if NOALBS_MODE == "local_docker":
        ok, err = await asyncio.to_thread(_noalbs_service_command_local_docker, "restart")
    else:
        ok, err = await asyncio.to_thread(_noalbs_service_command, "stop")
        if ok:
            ok, err = await asyncio.to_thread(_noalbs_service_command, "start")
    if not ok:
        raise HTTPException(
            status_code=502,
            detail=f"Gespeichert, aber Neustart fehlgeschlagen: {err}",
        )
    return {"ok": True}


@app.post("/obs/source/fix")
async def obs_source_fix():
    cfg = load_config()
    ok, err = await asyncio.to_thread(_restart_media_source, cfg.obs_media_source)
    if not ok:
        raise HTTPException(status_code=502, detail=err)
    return {"ok": True}


@app.get("/obs/preview/status")
def obs_preview_status():
    return {"enabled": _obs_preview_enabled}


@app.post("/obs/preview/enable")
def obs_preview_enable():
    global _obs_preview_enabled
    _obs_preview_enabled = True
    return {"ok": True, "enabled": True}


@app.post("/obs/preview/disable")
def obs_preview_disable():
    global _obs_preview_enabled
    _obs_preview_enabled = False
    # Verhindert, dass ein neu verbindender Client (z.B. nach Seiten-Reload)
    # ueber den "Sofort-Aufholen"-Mechanismus im WS-Endpoint noch das letzte,
    # eingefrorene Vorschaubild aus der Zeit vor dem Ausschalten angezeigt
    # bekommt.
    if current_session is not None:
        current_session.last_by_source.pop("obs_preview", None)
    return {"ok": True, "enabled": False}


class SceneItemVisibilityRequest(BaseModel):
    scene: str
    item_id: int
    enabled: bool


@app.post("/obs/source/visibility")
def obs_set_source_visibility(req: SceneItemVisibilityRequest):
    try:
        with _obs_client_ctx() as client:
            client.set_scene_item_enabled(req.scene, req.item_id, req.enabled)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class SourceMuteRequest(BaseModel):
    name: str
    muted: bool


@app.post("/obs/source/mute")
def obs_set_source_mute(req: SourceMuteRequest):
    try:
        with _obs_client_ctx() as client:
            client.set_input_mute(req.name, req.muted)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class SourceVolumeRequest(BaseModel):
    name: str
    volume: float


@app.post("/obs/source/volume")
def obs_set_source_volume(req: SourceVolumeRequest):
    try:
        with _obs_client_ctx() as client:
            client.set_input_volume(req.name, vol_mul=req.volume)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class SceneItemIndexRequest(BaseModel):
    scene: str
    item_id: int
    index: int


@app.post("/obs/source/index")
def obs_set_source_index(req: SceneItemIndexRequest):
    try:
        with _obs_client_ctx() as client:
            client.set_scene_item_index(req.scene, req.item_id, req.index)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/sessions")
def list_sessions():
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), reverse=True)
    return [f.stem for f in files]


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    path = SESSIONS_DIR / f"{session_id}.jsonl"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session nicht gefunden")
    return FileResponse(path, media_type="application/x-ndjson")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Middleware greift bei WebSockets nicht (nur HTTP-Requests) - deshalb
    # hier dieselbe Pruefung wie in auth_middleware() manuell wiederholt.
    # WICHTIG: erst accept(), DANN ggf. mit Code schliessen - ein close() vor
    # accept() lehnt das Upgrade nur als nacktes HTTP 403 ab (live beobachtet,
    # 2026-08-21) und der eigentliche Code 4401 kommt nie beim Frontend an,
    # das genau darauf wartet, um zur Login-Seite umzuleiten.
    await websocket.accept()
    if not _session_valid(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=4401)
        return
    if current_session is None:
        await websocket.send_text(json.dumps({"source": "system", "data": {"info": "kein aktiver Test"}}))
        await websocket.close()
        return
    current_session.clients.add(websocket)
    # Sofort-Aufholen: letzter bekannter Stand pro Quelle + aktuell noch
    # offene Findings, damit die Karten/das Panel nicht leer bleiben, nur weil
    # zufaellig noch kein neues Ereignis seit dem Verbinden passiert ist.
    try:
        for source, event in current_session.last_by_source.items():
            if source == "obs_preview" and not _obs_preview_enabled:
                continue
            await websocket.send_text(json.dumps(event))
        for event in current_session.active_findings.values():
            await websocket.send_text(json.dumps(event))
    except Exception:
        pass
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        current_session.clients.discard(websocket)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
