#!/usr/bin/env python3
"""IRL-PiP Dienst (irl-pip.service) fuer die BELABOX.

- haelt die Bild-in-Bild-Konfiguration (/opt/irl-pip/state/pip.json)
- erzeugt daraus die belacoder-Pipeline (custom/irl_pip) und die Steuerdatei des Mischers (/run/irl-pip/mix.conf)
- passt die Anordnung live an (Position, Rahmen, Versatz: ohne Neustart)
- bei Kamera-Ausfall/-Rueckkehr: schreibt die Pipeline neu und beendet belacoder; belaUI startet ihn mit der neuen Datei
- kleine HTTP-Schnittstelle (Port 8781, Bearer-Token, nur private Netze) fuer das IRL Diagnostics Dashboard

Nur Standardbibliothek. Aendert nichts an belaUI/nginx. Rueckbau: install.sh uninstall.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import irl_pip_gen as gen  # noqa: E402

VERSION = "0.1.0"
STATE_DIR = os.environ.get("IRL_PIP_STATE", "/opt/irl-pip/state")
CONFIG_FILE = os.path.join(STATE_DIR, "pip.json")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
RUN_DIR = os.environ.get("IRL_PIP_RUN", "/run/irl-pip")
MIX_CONF = os.path.join(RUN_DIR, "mix.conf")
PIPELINE_FILE = os.environ.get("IRL_PIP_PIPELINE", "/usr/share/belacoder/pipelines/custom/irl_pip")
BELA_TMP = "/tmp/belacoder_pipeline"
NGINX_STATS = "http://127.0.0.1:1936/"
PORT = int(os.environ.get("IRL_PIP_PORT", "8781"))
BIND = os.environ.get("IRL_PIP_BIND", "127.0.0.1")   # Produkt: nur lokal; das Dashboard greift per SSH + irl-pip-cli zu
MIN_RESTART_GAP = 8.0       # Sekunden zwischen zwei Neustarts des Encoders
ADD_DEBOUNCE = 4.0          # neu hinzugekommene Kamera muss so lange stabil senden, bevor neu gestartet wird
PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fe80::/10", "fc00::/7", "::1/128")]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def atomic_write(path, text, mode=0o644):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_live():
    """{kamera: (breite, hoehe)} der gerade zum nginx-rtmp sendenden Kameras."""
    try:
        with urllib.request.urlopen(NGINX_STATS, timeout=2) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return None
    live = {}
    for app in root.iter("application"):
        if (app.findtext("name") or "") != "publish":
            continue
        for st in app.iter("stream"):
            name = st.findtext("name") or ""
            if st.find("publishing") is None or not gen.CAM_RE.match(name):
                continue
            try:
                w = int(st.findtext("meta/video/width") or 0)
                h = int(st.findtext("meta/video/height") or 0)
            except ValueError:
                w = h = 0
            live[name] = (w, h) if w and h else gen.DEFAULT_RES
    return live


def find_belacoder():
    """(pid, pipelinepfad) des laufenden belacoder oder None."""
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % d, "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if argv and os.path.basename(argv[0].decode(errors="replace")) == "belacoder" and len(argv) > 1:
            return int(d), argv[1].decode(errors="replace")
    return None


class Core:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = gen.validate_config(gen.DEFAULT_CONFIG)
        self.auto_restart = True
        self.layout = None
        self.written_text = None         # Inhalt der zuletzt geschriebenen Pipeline-Datei (custom/irl_pip)
        self.live = {}
        self.live_since = {}
        self.restarts = 0
        self.last_restart = 0.0
        self.last_restart_cause = ""
        self.pending_restart = False
        self.user_restart_ok = False
        self.cfg_pending = False         # Konfiguration wurde geaendert und noch nicht in der laufenden Pipeline wirksam
        self.last_error = ""
        self.load()

    # ---- Konfiguration -------------------------------------------------------------------------------------
    def load(self):
        try:
            with open(CONFIG_FILE) as f:
                d = json.load(f)
            self.cfg = gen.validate_config(d.get("pip", {}))
            self.auto_restart = bool(d.get("auto_restart", True))
        except FileNotFoundError:
            pass
        except Exception as e:
            self.last_error = "Konfiguration unlesbar, Standard wird benutzt: %s" % e
            log(self.last_error)

    def save(self):
        atomic_write(CONFIG_FILE, json.dumps({"pip": self.cfg, "auto_restart": self.auto_restart}, indent=1), 0o600)

    def set_config(self, raw, restart_ok):
        with self.lock:
            d = dict(raw)
            auto = d.pop("auto_restart", self.auto_restart)
            cfg = gen.validate_config(d)
            self.cfg, self.auto_restart = cfg, bool(auto)
            self.save()
            self.user_restart_ok = bool(restart_ok)
            self.cfg_pending = True
        before = self.restarts
        self.tick(force=True)
        with self.lock:
            return {"needs_restart": self.pending_restart, "restarted": self.restarts > before}

    # ---- Hauptschleife -------------------------------------------------------------------------------------
    def tick(self, force=False):  # force: sofort neu auswerten (API); die Schleife ruft ohne auf
        live = read_live()
        now = time.time()
        with self.lock:
            if live is None:
                self.last_error = "nginx-Statistik nicht erreichbar"
                return
            self.last_error = ""
            for k in list(self.live_since):
                if k not in live:
                    del self.live_since[k]
            for k in live:
                self.live_since.setdefault(k, now)
            self.live = live
            layout = gen.resolve_layout(self.cfg, live)
            self.layout = layout
            if layout is None:
                return
            text = gen.render_pipeline(layout, MIX_CONF)
            # Steuerdatei des Mischers: immer aktuell halten (wirkt live, ohne Neustart)
            conf = gen.render_mix_conf(layout)
            try:
                os.makedirs(RUN_DIR, exist_ok=True)
                if not os.path.exists(MIX_CONF) or open(MIX_CONF).read() != conf:
                    atomic_write(MIX_CONF, conf)
                # Pipeline-Datei fuer den naechsten Start ueber belaUI immer aktuell halten
                if text != self.written_text:
                    atomic_write(PIPELINE_FILE, text)
                    self.written_text = text
            except OSError as e:
                self.last_error = "Datei schreiben: %s" % e
                log(self.last_error)
            bela = find_belacoder()
            running_text = None
            if bela and bela[1] == BELA_TMP:
                try:
                    running_text = open(BELA_TMP).read()
                except OSError:
                    running_text = None
            if running_text is None or "irlpipmix" not in running_text:
                # belacoder laeuft gar nicht oder mit fremder Pipeline: nichts anfassen
                self.pending_restart = False
                return
            needs = running_text.strip() != text.strip()
            self.pending_restart = needs
            if not needs:
                self.user_restart_ok = False
                self.cfg_pending = False
                return
            running_cams = set(re.findall(r"publish/([A-Za-z0-9_-]+)", running_text))
            # neu hinzugekommene Kamera erst nach kurzer Stabilitaetszeit einbinden (Kameras zucken beim Verbinden)
            added = any(c not in running_cams and now - self.live_since.get(c, now) < ADD_DEBOUNCE
                        for c in [layout["main"]] + [p["cam"] for p in layout["pips"]])
            gap_ok = now - self.last_restart >= MIN_RESTART_GAP
            # Eine laufende Kamera der Pipeline ist weg: die Sendung ist ohnehin gestoert, also gleich neu aufbauen.
            broken = any(c not in live for c in running_cams)
            # Automatisch nur bei Kamera-Ereignissen; reine Konfigurationsaenderungen warten auf die Bestaetigung des Benutzers
            # (das Bild ist beim Neustart kurz weg).
            auto_ok = self.auto_restart and not added and (not self.cfg_pending or broken)
            if gap_ok and (self.user_restart_ok or auto_ok):
                self.restart_encoder(bela[0], text, "benutzer" if self.user_restart_ok else "kamera")

    def restart_encoder(self, pid, text, cause):
        try:
            atomic_write(BELA_TMP, text, 0o644)
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            self.last_error = "Neustart fehlgeschlagen: %s" % e
            log(self.last_error)
            return
        self.restarts += 1
        self.last_restart = time.time()
        self.last_restart_cause = cause
        self.user_restart_ok = False
        self.cfg_pending = False
        self.pending_restart = False
        log("Encoder neu gestartet (%s), belaUI startet ihn mit der neuen Pipeline" % cause)

    # ---- Zustand fuer die API ------------------------------------------------------------------------------
    def state(self):
        with self.lock:
            bela = find_belacoder()
            ours = False
            if bela and bela[1] == BELA_TMP:
                try:
                    ours = "irlpipmix" in open(BELA_TMP).read()
                except OSError:
                    pass
            mixer = None
            try:
                mixer = json.load(open(MIX_CONF + ".stats"))
            except Exception:
                pass
            cams = [{"key": k, "width": v[0], "height": v[1], "since": int(self.live_since.get(k, time.time()))}
                    for k, v in sorted(self.live.items())]
            lay = None
            if self.layout:
                L = self.layout
                lay = {"main": L["main"], "main_res": list(L["main_res"]), "audio": L["audio"],
                       "pips": [{"cam": p["cam"], "slot": p["slot"], "w": p["w"], "h": p["h"],
                                 "x": gen.pip_position(L, p)[0], "y": gen.pip_position(L, p)[1]} for p in L["pips"]]}
            return {"version": VERSION, "cameras": cams, "config": self.cfg, "auto_restart": self.auto_restart,
                    "layout": lay, "streaming": {"active": bela is not None, "ours": ours, "pid": bela[0] if bela else None},
                    "pending_restart": self.pending_restart, "restarts": self.restarts, "mixer": mixer,
                    "error": self.last_error, "time": int(time.time())}


CORE = Core()


def load_token():
    try:
        return open(TOKEN_FILE).read().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "irl-pip"

    def log_message(self, format, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0].split("%")[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
        except ValueError:
            return False
        return any(ip in n for n in PRIVATE_NETS)

    def _auth(self):
        tok = load_token()
        got = self.headers.get("Authorization", "")
        return bool(tok) and got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), tok.encode())

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "Netz nicht erlaubt"})
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self._send(200, {"ok": True, "version": VERSION})
        if not self._auth():
            return self._send(401, {"error": "Anmeldung noetig"})
        if path == "/api/state":
            return self._send(200, CORE.state())
        self._send(404, {"error": "unbekannt"})

    def do_PUT(self):
        self._write()

    def do_POST(self):
        self._write()

    def _write(self):
        if not self._allowed():
            return self._send(403, {"error": "Netz nicht erlaubt"})
        if not self._auth():
            return self._send(401, {"error": "Anmeldung noetig"})
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 65536:
                return self._send(413, {"error": "zu gross"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "kein gueltiges JSON"})
        if path == "/api/config":
            try:
                restart = bool(body.pop("restart", False)) if isinstance(body, dict) else False
                res = CORE.set_config(body, restart)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True, **res, "state": CORE.state()})
        if path == "/api/restart":
            with CORE.lock:
                CORE.user_restart_ok = True
            CORE.tick(force=True)
            return self._send(200, {"ok": True, "state": CORE.state()})
        self._send(404, {"error": "unbekannt"})


def loop():
    while True:
        try:
            CORE.tick()
        except Exception as e:  # nie die Schleife verlieren
            CORE.last_error = "Schleife: %s" % e
            log("Fehler in der Schleife:", repr(e))
        time.sleep(1.0)


def main():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    if not load_token():
        log("KEIN Token in %s - Schnittstelle bleibt gesperrt (install.sh legt ihn an)" % TOKEN_FILE)
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log("irl-pip %s auf Port %d" % (VERSION, PORT))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    srv.serve_forever()


if __name__ == "__main__":
    main()
