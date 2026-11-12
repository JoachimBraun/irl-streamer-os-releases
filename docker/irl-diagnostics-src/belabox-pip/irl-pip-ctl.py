#!/usr/bin/env python3
"""IRL-PiP Dienst (irl-pip.service) fuer die BELABOX.

- haelt die Bild-in-Bild-Konfiguration (/opt/irl-pip/state/pip.json)
- erzeugt daraus die belacoder-Pipeline (custom/irl_pip) und die Steuerdatei des Mischers (/run/irl-pip/mix.conf)
- passt die Anordnung live an (Position, Rahmen, Versatz: ohne Neustart)
- IRL-PiP 2.0 (seit V1.112 der einzige Weg, PiP 1.0 ist entfernt): Pipeline mit irlpip2-Elementen, alle Kameras
  dekodiert, Steuerdatei /run/irl-pip/mix2.conf mit Szenen. Szenenwechsel (irl-pip-cli scene <name>) schreibt nur die
  Steuerdatei neu (atomar) - kein Encoder-Neustart. Fehlt das irlpip2-Plugin, wird nichts geschrieben (Fehlermeldung).
- die belacoder-Pipeline enthaelt keine Kameras; je sendender Kamera laeuft ein Zubringer-Prozess (gst-launch-1.0,
  Klasse Feeders) in den gemeinsamen Speicher /dev/shm/irlpip2-slotN. Stirbt einer, Neustart nach 1 s. Kamera kommt/geht
  -> nur Zubringer + mix2.conf, KEIN Encoder-Neustart.
- Zubringer-Waechter: ein Zubringer, der lebt, aber keine Bilder mehr liefert, obwohl nginx weiter Daten der Kamera
  empfaengt (Decoder/rtmpsrc haengt), wird hart beendet und neu gestartet (Core._check_stalls).
- kurze Luecke ohne belacoder (belaUI startet ihn nach SRT-Abbruch neu): Zubringer laufen bis 30 s weiter.
- ein neu gestarteter Dienst startet eine laufende Sendung nie von selbst neu (nur vormerken), ausser eine Kamera der
  laufenden Pipeline fehlt.
- kleine HTTP-Schnittstelle (Port 8781, Bearer-Token, nur private Netze) fuer das IRL Diagnostics Dashboard

Nur Standardbibliothek. Aendert nichts an belaUI/nginx. Rueckbau: install.sh uninstall.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import irl_pip2_gen as gen2  # noqa: E402

VERSION = "0.3.0"
# Kennung des LAUFENDEN Codes (state.code, Review 11.11. C4): die ersten 12 Zeichen von hashlib.sha256(<rohe Bytes
# dieser Datei>).hexdigest() (Kleinbuchstaben 0-9a-f), ohne jede Umwandlung (kein Zeilenende-/Kodierungs-Abgleich).
# Einmal beim Start gelesen: ein spaeter ausgetauschtes bin/irl-pip-ctl.py aendert sie erst nach dem Neustart des
# Dienstes - genau das soll das Dashboard sehen. Es bildet dieselbe Kennung ueber seine Kopie belabox-pip/irl-pip-ctl.py
# (setup kopiert die Datei unveraendert nach bin/). "" = Datei nicht lesbar (Dashboard wertet das nicht als veraltet).
try:
    with open(os.path.abspath(__file__), "rb") as _f:
        CODE = hashlib.sha256(_f.read()).hexdigest()[:12]
except OSError:
    CODE = ""
STATE_DIR = os.environ.get("IRL_PIP_STATE", "/opt/irl-pip/state")
CONFIG_FILE = os.path.join(STATE_DIR, "pip.json")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
RUN_DIR = os.environ.get("IRL_PIP_RUN", "/run/irl-pip")
MIX2_CONF = os.path.join(RUN_DIR, "mix2.conf")       # IRL-PiP 2.0: Steuerdatei mit Szene (irlpip2vsrc/asrc)
PLUGIN2_FILE = os.environ.get("IRL_PIP_PLUGIN2", "/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstirlpip2.so")
PIPELINE_FILE = os.environ.get("IRL_PIP_PIPELINE", "/usr/share/belacoder/pipelines/custom/irl_pip")
BELA_TMP = "/tmp/belacoder_pipeline"
SHM_DIR = os.environ.get("IRL_PIP_SHM_DIR", "/dev/shm")    # gemeinsamer Speicher der Zubringer (irlpip2-slotN)
FEEDER_RESTART_S = 1.0      # Zubringer gestorben -> so lange warten, dann neu starten
FEEDER_STOP_S = 2.0         # nach SIGINT so lange auf sauberes Ende warten, dann SIGKILL
FEEDER_LOG_MAX = 256 * 1024 # Protokoll je Zubringer wird darueber gekuerzt
SHM_GC_S = 60.0             # Platz ohne laufenden Zubringer: Speicherdatei nach dieser Zeit loeschen (RAM frei)
NGINX_STATS = "http://127.0.0.1:1936/"
PORT = int(os.environ.get("IRL_PIP_PORT", "8781"))
BIND = os.environ.get("IRL_PIP_BIND", "127.0.0.1")   # Produkt: nur lokal; das Dashboard greift per SSH + irl-pip-cli zu
MIN_RESTART_GAP = 8.0       # Sekunden zwischen zwei Neustarts des Encoders
BELA_GONE_GRACE_S = 30.0    # Zubringer laufen nach dem Ende von belacoder so lange weiter: belaUI startet ihn nach einem
                            # SRT-Abbruch gleich neu, der neue findet dann sofort Bilder im Ring (Review 11.11. C2)
# Zubringer-Waechter (Review 11.11. C1): ein Zubringer, der lebt, aber keine Bilder mehr in seinen Platz legt (Decoder
# oder rtmpsrc haengt, EOS wird im Plugin verschluckt), endet nie von selbst -> bei Platz 0 waere das ganze Sendebild
# schwarz, bis die Kamera neu startet. Alle Schwellen liegen weit weg vom Normalbetrieb (30 fps = alle 33 ms ein Bild,
# Kamera sendet fest 4000 kbit = ca. 500 KB/s).
STALL_AGE_MS = 5000         # Platz ohne neues Bild seit mehr als so vielen ms (age_ms aus mix2.conf.stats; -1 = noch nie)
STALL_GRACE_S = 15.0        # erst pruefen, wenn der Zubringer so lange laeuft (RTMP-Verbindung + Decoder-Anlauf)
STALL_OBS_S = 2.0           # nginx-Zaehler der Kamera ueber mindestens so viele Sekunden beobachten ...
STALL_MIN_BPS = 64 * 1024   # ... und nginx muss dabei weiter >= 64 KB/s empfangen; sonst fehlt das Signal schon an der
                            # Kamera/im WLAN (Neustart braechte nichts; reiner Ton, AAC ca. 16-24 KB/s, loest nie aus)
STALL_COOLDOWN_S = 30.0     # Bremse: hoechstens ein Eingriff je Platz in diesem Abstand (Decoder-An/Aus auf dem RK3588)
STALL_STATS_FRESH_S = 3.0   # nur Statistik, deren Takt-Zaehler sich so kurz vorher bewegt hat (steht belacoder selbst,
                            # ist das Aufgabe von irl-net-watch, nicht dieses Waechters)
SNAP_LOCK = threading.Lock()       # nur ein Schnappschuss gleichzeitig (Hardware-Decoder teilen sich alle Kameras)
SNAP_TIMEOUT = 8.0          # Sekunden fuer gst-launch-1.0, danach abbrechen (Kamera liefert nicht/Pipeline haengt)
SNAP_MAX_INTERVAL = 4.0     # kein neuer Schnappschuss derselben Kamera schneller als alle 4s (Dashboard pollt alle 10s)
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


def uses_shm(text):
    """belacoder-Pipeline von IRL-PiP 2.0 mit entkoppelten Kameras (Taktgeber liest den gemeinsamen Speicher)?"""
    return bool(text) and "irlpip2vsrc" in text and ("shm=%s" % gen2.SHM_NAME) in text


def is_ours(text):
    """Laeuft belacoder mit unserer Pipeline (IRL-PiP 2.0)? Eine noch laufende alte PiP-1.0-Pipeline gilt als fremd:
    sie wird nicht angefasst, beim naechsten Start nimmt belaUI die neue Pipeline-Datei."""
    return bool(text) and "irlpip2vsrc" in text


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
            if st.find("publishing") is None or not gen2.CAM_RE.match(name):
                continue
            try:
                w = int(st.findtext("meta/video/width") or 0)
                h = int(st.findtext("meta/video/height") or 0)
            except ValueError:
                w = h = 0
            live[name] = (w, h) if w and h else gen2.DEFAULT_RES
    return live


def read_bytes_in():
    """{kamera: bytes_in} der gerade sendenden Kameras aus der nginx-Statistik (von der Kamera empfangene Bytes seit
    ihrer Anmeldung; neue Anmeldung = Zaehler beginnt bei 0) oder None. Nur fuer den Zubringer-Waechter und nur bei
    Verdacht abgefragt, nicht jede Sekunde."""
    try:
        with urllib.request.urlopen(NGINX_STATS, timeout=2) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return None
    out = {}
    for app in root.iter("application"):
        if (app.findtext("name") or "") != "publish":
            continue
        for st in app.iter("stream"):
            if st.find("publishing") is None:
                continue
            try:
                out[st.findtext("name") or ""] = int(st.findtext("bytes_in") or 0)
            except ValueError:
                pass
    return out


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


_snap_cache = {}   # cam -> {"t": time.monotonic(), "b64": ..., "w":, "h":} (monoton: Uhrsprung, Review 11.11. C8)


def capture_snapshot(cam, live):
    """Ein einzelnes JPEG-Bild aus dem gerade ankommenden RTMP-Stream der Kamera (lokal, kein zusaetzlicher
    Netzverkehr zur Kamera/zum Zuschauer - nur ein kurzer lokaler Hardware-Decode). Gecacht fuer SNAP_MAX_INTERVAL
    Sekunden, damit mehrfaches schnelles Nachfragen (z. B. zwei offene Dashboard-Tabs) nicht mehrfach decodiert."""
    if cam not in live:
        return {"error": "Kamera sendet gerade nicht"}
    cached = _snap_cache.get(cam)
    if cached and time.monotonic() - cached["t"] < SNAP_MAX_INTERVAL:
        return {"ok": True, "image_b64": cached["b64"], "width": cached["w"], "height": cached["h"]}
    if not SNAP_LOCK.acquire(timeout=SNAP_TIMEOUT):
        return {"error": "Schnappschuss-Hardware ist gerade belegt, bitte kurz erneut versuchen"}
    try:
        w, h = live.get(cam) or gen2.DEFAULT_RES
        with tempfile.NamedTemporaryFile(prefix="irl-pip-snap-", suffix=".jpg", delete=False) as tf:
            path = tf.name
        try:
            cmd = ["timeout", str(int(SNAP_TIMEOUT)), "gst-launch-1.0", "-q",
                   "rtmpsrc", "location=%s/%s" % (gen2.RTMP_BASE, cam), "do-timestamp=true", "!",
                   "flvdemux", "name=d", "d.video", "!",
                   "queue", "max-size-buffers=0", "max-size-bytes=0", "max-size-time=0", "!",
                   "h264parse", "!", "mppvideodec", "!", "videoconvert", "!",
                   "identity", "eos-after=1", "!", "jpegenc", "!",
                   "filesink", "location=%s" % path]
            r = subprocess.run(cmd, capture_output=True, timeout=SNAP_TIMEOUT + 2)
            if r.returncode != 0 or not os.path.getsize(path):
                return {"error": "Schnappschuss fehlgeschlagen: %s" % (r.stderr or b"").decode(errors="replace")[-300:]}
            data = open(path, "rb").read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        b64 = base64.b64encode(data).decode()
        _snap_cache[cam] = {"t": time.monotonic(), "b64": b64, "w": w, "h": h}
        return {"ok": True, "image_b64": b64, "width": w, "height": h}
    except subprocess.TimeoutExpired:
        return {"error": "Zeitueberschreitung beim Schnappschuss"}
    finally:
        SNAP_LOCK.release()


class Feeders:
    """Zubringer-Prozesse je Kamera-Platz (IRL-PiP 2.0). Je Platz ein Waechter-Thread: startet den Prozess, wartet auf
    sein Ende (kein Zombie: Popen.wait), startet ihn nach FEEDER_RESTART_S neu, solange der Platz gewuenscht ist.
    Prozess in eigener Sitzung (Prozessgruppe) -> Signale gehen an gst-launch samt Kindern. Ausgabe nach
    RUN_DIR/feeder-N.log (angehaengt, ueber FEEDER_LOG_MAX gekuerzt). Idee nach IRL4YOU pipbox_always.py (MIT)."""

    def __init__(self, popen=subprocess.Popen, killpg=os.killpg, restart_s=FEEDER_RESTART_S, stop_s=FEEDER_STOP_S):
        self.popen, self.killpg, self.restart_s, self.stop_s = popen, killpg, restart_s, stop_s
        self.lock = threading.Lock()
        self.slots = {}           # slot -> Zustand (argv, stop, thread, proc, starts, ...)
        self.stopped_at = {}      # slot -> Zeitpunkt (time.monotonic), seit dem dort kein Zubringer mehr laeuft

    def log_path(self, slot):
        return os.path.join(RUN_DIR, "feeder-%d.log" % slot)

    def set(self, wanted):
        """wanted: {slot: argv}. Fehlende Plaetze stoppen, neue starten, geaenderte (andere Kamera) neu starten."""
        with self.lock:
            for slot in list(self.slots):
                if slot not in wanted or self.slots[slot]["argv"] != wanted[slot]:
                    self._stop_locked(slot)
            for slot, argv in wanted.items():
                if slot not in self.slots:
                    self._start_locked(slot, list(argv))

    def _start_locked(self, slot, argv):
        st = {"argv": argv, "stop": threading.Event(), "proc": None, "starts": 0, "last_exit": None,
              "since": None, "since_mono": None, "stall_restarts": 0,
              "cam": next((a.rsplit("/", 1)[1] for a in argv if a.startswith("location=")), "")}
        st["thread"] = threading.Thread(target=self._loop, args=(slot, st), daemon=True, name="feeder%d" % slot)
        self.slots[slot] = st
        self.stopped_at.pop(slot, None)
        st["thread"].start()

    def _stop_locked(self, slot):
        st = self.slots.pop(slot, None)
        if st:
            st["stop"].set()
            self._signal(st.get("proc"), signal.SIGINT)
            self.stopped_at[slot] = time.monotonic()

    def _signal(self, p, sig):
        if p is None or p.poll() is not None:
            return
        try:
            self.killpg(p.pid, sig)
        except OSError:
            try:
                p.send_signal(sig)
            except OSError:
                pass

    def _trim_log(self, path):
        try:
            if os.path.getsize(path) > FEEDER_LOG_MAX:
                os.truncate(path, 0)
        except OSError:
            pass

    def _loop(self, slot, st):
        path = self.log_path(slot)
        while not st["stop"].is_set():
            self._trim_log(path)
            try:
                os.makedirs(RUN_DIR, exist_ok=True)
                with open(path, "ab") as lf:
                    p = self.popen(st["argv"], stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True)
            except (OSError, ValueError) as e:
                if st.get("warned") != type(e).__name__:
                    st["warned"] = type(e).__name__
                    log("Zubringer %d (%s) nicht startbar: %s" % (slot, st["cam"], e))
                if st["stop"].wait(5):
                    return
                continue
            st["proc"], st["since"], st["since_mono"] = p, time.time(), time.monotonic()
            st["starts"] += 1
            if st["starts"] == 1 or st["starts"] % 30 == 0:
                log("Zubringer %d (%s) gestartet (pid %d, Start Nr. %d)" % (slot, st["cam"], p.pid, st["starts"]))
            while not st["stop"].is_set():
                try:
                    p.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    self._trim_log(path)
            if st["stop"].is_set():
                self._signal(p, signal.SIGINT)
                try:
                    p.wait(timeout=self.stop_s)
                except subprocess.TimeoutExpired:
                    self._signal(p, signal.SIGKILL)
                    p.wait()
                st["last_exit"] = p.returncode
                return
            st["last_exit"] = p.returncode
            st["since"] = st["since_mono"] = None
            st["stop"].wait(self.restart_s)

    def running_since(self):
        """{slot: (Startzeit time.monotonic, kamera)} der gerade laufenden Zubringer (fuer den Zubringer-Waechter)."""
        with self.lock:
            return {slot: (st["since_mono"], st["cam"]) for slot, st in self.slots.items()
                    if st["proc"] is not None and st["proc"].poll() is None and st["since_mono"] is not None}

    def kick(self, slot, why):
        """Haengenden Zubringer hart beenden: SIGKILL an die Prozessgruppe, denn ein haengender Decoder reagiert auf SIGINT
        nicht zuverlaessig (gst-launch macht bei SIGINT nur einen Zustandswechsel). Die Waechter-Schleife (_loop) holt
        den Prozess ab und startet ihn nach restart_s neu. Den gemeinsamen Speicher uebernimmt der neue Zubringer
        (robuster Mutex, toter Schreiber wird erkannt). False, wenn auf dem Platz gerade kein Prozess laeuft."""
        with self.lock:
            st = self.slots.get(slot)
            p = st and st["proc"]
            if not p or p.poll() is not None:
                return False
            st["stall_restarts"] += 1
            cam, n = st["cam"], st["stall_restarts"]
        log("Zubringer %d (%s) haengt (%s) - neu gestartet (Eingriff Nr. %d)" % (slot, cam, why, n))
        self._signal(p, signal.SIGKILL)
        return True

    def stop_all(self, wait=True):
        with self.lock:
            threads = [st["thread"] for st in self.slots.values()]
            for slot in list(self.slots):
                self._stop_locked(slot)
        if wait:
            for t in threads:
                t.join(self.stop_s + 2)

    def gc_shm(self, configured, now=None):
        """Speicherdateien von Plaetzen loeschen, auf denen seit SHM_GC_S kein Zubringer laeuft (Kamera lange weg,
        keine Sendung) oder die keiner Kamera mehr zugeordnet sind. Die Ausgabe laesst den Speicher dann los."""
        now = time.monotonic() if now is None else now
        with self.lock:
            running = set(self.slots)
        for slot in range(gen2.MAX_SLOTS):
            if slot in running:
                continue
            since = self.stopped_at.setdefault(slot, now)
            if slot not in configured or now - since >= SHM_GC_S:
                path = os.path.join(SHM_DIR, "%s-slot%d" % (gen2.SHM_NAME, slot))
                try:
                    os.unlink(path)
                    log("Speicher %s freigegeben" % path)
                except OSError:
                    pass

    def state(self):
        with self.lock:
            out = []
            for slot, st in sorted(self.slots.items()):
                p = st["proc"]
                out.append({"slot": slot, "cam": st["cam"], "running": bool(p and p.poll() is None),
                            "pid": p.pid if p and p.poll() is None else None, "starts": st["starts"],
                            "last_exit": st["last_exit"], "since": int(st["since"]) if st["since"] else None,
                            "stall_restarts": st["stall_restarts"]})
            return out


class Core:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = gen2.validate_config(gen2.DEFAULT_CONFIG)
        self.auto_restart = True
        self.layout = None
        self.written_text = None         # Inhalt der zuletzt geschriebenen Pipeline-Datei (custom/irl_pip)
        self.live = {}
        self.live_since = {}
        self.restarts = 0
        self.last_restart = float("-inf")  # time.monotonic des letzten Encoder-Neustarts (Uhrsprung, Review 11.11. C8)
        self.last_restart_cause = ""
        self.pending_restart = False
        self.user_restart_ok = False
        # Konfiguration wurde geaendert und noch nicht in der laufenden Pipeline wirksam. Beim Dienststart WAHR
        # (Review 11.11. C4): laeuft schon eine Sendung mit abweichendem Pipeline-Text (z. B. neuer Generator nach einem
        # Paket-Update), wird der Neustart nur vorgemerkt (pending_restart) und wartet auf den Benutzer ("Neustart
        # anwenden") - ausser eine Kamera der laufenden Pipeline fehlt (broken). Gleicher Text -> sofort wieder FALSCH.
        self.cfg_pending = True
        self.shm_seen = None             # time.monotonic, zuletzt belacoder mit Speicher-Pipeline gesehen (C2)
        self.stall_obs = {}              # Zubringer-Waechter: slot -> (time.monotonic, nginx bytes_in, kamera)
        self.last_kick = {}              # Zubringer-Waechter: slot -> time.monotonic des letzten Eingriffs (Bremse)
        self.stall_kicks = 0             # Zubringer-Waechter: Eingriffe seit Dienststart (state.stall_restarts)
        self.stall_err = ""              # zuletzt gemeldeter Fehler des Waechters (nur einmal ins Journal)
        self.stats_ticks = None          # Takt-Zaehler aus mix2.conf.stats beim letzten Lesen ...
        self.stats_ticks_at = float("-inf")  # ... und wann (time.monotonic) er sich zuletzt bewegt hat
        self.last_error = ""
        self.feeders = Feeders()
        self.load()

    # ---- Konfiguration -------------------------------------------------------------------------------------
    def load(self):
        try:
            with open(CONFIG_FILE) as f:
                d = json.load(f)
            self.cfg = gen2.validate_config(d.get("pip", {}))
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
            cfg = gen2.validate_config(d)
            self.cfg, self.auto_restart = cfg, bool(auto)
            self.save()
            self.user_restart_ok = bool(restart_ok)
            self.cfg_pending = True
        before = self.restarts
        self.tick(force=True)
        with self.lock:
            return {"needs_restart": self.pending_restart, "restarted": self.restarts > before}

    def mode(self):
        """Wirksamer Modus: "v2", sobald das irlpip2-Plugin auf der Box liegt, sonst "" (dann laeuft kein PiP)."""
        return "v2" if os.path.exists(PLUGIN2_FILE) else ""

    def _write_mix2(self, layout):
        """Steuerdatei von IRL-PiP 2.0 schreiben (atomar: tmp + rename), nur wenn sich der Inhalt aendert."""
        conf = gen2.render_mix2_conf(layout)
        os.makedirs(RUN_DIR, exist_ok=True)
        if not os.path.exists(MIX2_CONF) or open(MIX2_CONF).read() != conf:
            atomic_write(MIX2_CONF, conf)
            return True
        return False

    def set_scene(self, name):
        """Szenenwechsel: nur die Steuerdatei neu schreiben (gemerkte Kamera-Liste, keine nginx-Abfrage) - wirkt beim
        naechsten Pruefen des Taktgebers (200 ms), ohne Pipeline-Neubau. Wirft ValueError bei unbekannter Szene."""
        t0 = time.monotonic()
        with self.lock:
            if gen2.find_scene(self.cfg, name) is None:
                raise ValueError("Unbekannte Szene: %s" % name)
            self.cfg["active_scene"] = name
            applied = False
            if self.mode() == "v2":
                layout = gen2.resolve_layout(self.cfg, self.live)
                if layout is not None:
                    self.layout = layout
                    try:
                        self._write_mix2(layout)
                        applied = True
                    except OSError as e:
                        self.last_error = "Datei schreiben: %s" % e
                        log(self.last_error)
            try:
                self.save()
            except OSError as e:
                log("Konfiguration speichern: %s" % e)
        ms = int((time.monotonic() - t0) * 1000)
        log("Szene '%s' (%s, %d ms)" % (name, "aktiv" if applied else "gemerkt", ms))
        return {"scene": name, "applied": applied, "ms": ms}

    # ---- Hauptschleife -------------------------------------------------------------------------------------
    def tick(self, force=False):  # force: sofort neu auswerten (API); die Schleife ruft ohne auf
        live = read_live()
        now = time.time()                # nur fuer die Anzeige (live_since)
        mono = time.monotonic()          # alle Abstaende (Uhrsprung, Review 11.11. C8)
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
            if self.mode() != "v2":
                self.last_error = "Das irlpip2-Plugin fehlt auf der Box - Bild-in-Bild ist nicht verfuegbar (Paket neu einspielen)"
                self.layout = None
                self.feeders.set({})
                return
            layout = gen2.resolve_layout(self.cfg, live)
            self.layout = layout
            if layout is None:
                self.feeders.set({})
                return
            text = gen2.render_pipeline(layout, MIX2_CONF)
            try:
                os.makedirs(RUN_DIR, exist_ok=True)
                # Steuerdatei mit der aktiven Szene (wirkt live, ohne Neustart)
                self._write_mix2(layout)
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
            # Zubringer nur, solange belacoder mit unserer Speicher-Pipeline sendet (spart Decoder/Strom ohne Sendung).
            # Kurze Luecke OHNE belacoder (belaUI startet ihn nach SRT-Abbruch neu, Zombie zaehlt als weg): Zubringer
            # laufen BELA_GONE_GRACE_S weiter und folgen dabei weiter den Kameras, der neue belacoder hat sofort Bilder
            # (Review 11.11. C2). Eine fremde Pipeline (bewusster Wechsel) stoppt sie sofort, sonst konkurrieren
            # Decoder mit der Standard-RTMP-Pipeline.
            shm_live = bool(running_text and uses_shm(running_text))
            if shm_live:
                self.shm_seen = mono
            elif bela is not None:
                self.shm_seen = None     # anderer belacoder laeuft: die Nachlaufzeit ist vorbei, auch nach seinem Ende
            keep = shm_live or (self.shm_seen is not None and mono - self.shm_seen < BELA_GONE_GRACE_S)
            self.feeders.set(gen2.feeders(layout) if keep else {})
            if shm_live:                 # in der Nachlaufzeit nicht: ohne belacoder gibt es keine frische Statistik
                try:
                    self._check_stalls(mono)
                except Exception as e:   # der Waechter darf den Takt (Speicher, Neustart-Logik) nie stoeren
                    if repr(e) != self.stall_err:
                        self.stall_err = repr(e)
                        log("Zubringer-Waechter: %s" % self.stall_err)
            self.feeders.gc_shm({s["slot"] for s in layout["slots"]})
            if running_text is None or not is_ours(running_text):
                # belacoder laeuft gar nicht oder mit fremder Pipeline: nichts anfassen
                self.pending_restart = False
                return
            needs = running_text.strip() != text.strip()
            self.pending_restart = needs
            if not needs:
                self.user_restart_ok = False
                self.cfg_pending = False
                return
            # Kameras stecken nicht in der Pipeline (Zubringer-Prozesse) -> neue Kameras brauchen keine Wartezeit.
            # Nur eine aeltere Pipeline mit eigenen Kamerazweigen kann Kameras enthalten: ist eine davon weg, ist die
            # Sendung ohnehin gestoert, also gleich neu aufbauen.
            running_cams = set(re.findall(r"publish/([A-Za-z0-9_-]+)", running_text))
            gap_ok = mono - self.last_restart >= MIN_RESTART_GAP
            broken = any(c not in live for c in running_cams)
            # Automatisch nur bei Kamera-Ereignissen; reine Konfigurationsaenderungen und ein frisch gestarteter Dienst
            # (cfg_pending beginnt WAHR) warten auf die Bestaetigung des Benutzers (das Bild ist beim Neustart kurz weg).
            auto_ok = self.auto_restart and (not self.cfg_pending or broken)
            if gap_ok and (self.user_restart_ok or auto_ok):
                self.restart_encoder(bela[0], text, "benutzer" if self.user_restart_ok else "kamera")

    def _fresh_stats(self, mono):
        """mix2.conf.stats (schreibt irlpip2vsrc in belacoder jede Sekunde) oder None, wenn sie nicht frisch ist. Frisch
        heisst: der Takt-Zaehler "ticks" hat sich in den letzten STALL_STATS_FRESH_S Sekunden bewegt - gemessen mit der
        monotonen Uhr dieses Dienstes, nicht ueber die mtime (Uhrsprung). Der erste gelesene Stand zaehlt nie (die Datei
        kann von einem stehenden belacoder liegen geblieben sein), erst eine Bewegung danach."""
        try:
            with open(MIX2_CONF + ".stats") as f:
                stats = json.load(f)
        except Exception:
            return None
        if not isinstance(stats, dict):
            return None
        ticks = stats.get("ticks")
        if ticks != self.stats_ticks:
            if self.stats_ticks is not None:
                self.stats_ticks_at = mono
            self.stats_ticks = ticks
        return stats if mono - self.stats_ticks_at <= STALL_STATS_FRESH_S else None

    def _check_stalls(self, mono):
        """Zubringer-Waechter (Review 11.11. C1): einen Zubringer ersetzen, der laeuft, aber kein Bild mehr in seinen
        Platz legt, obwohl nginx fuer die Kamera weiter Daten empfaengt. Eingriff nur, wenn ALLES zutrifft:
          1. Statistik frisch (_fresh_stats), belacoder laeuft mit der Speicher-Pipeline (Aufrufer);
          2. Zubringer laeuft seit mindestens STALL_GRACE_S;
          3. age_ms des Platzes > STALL_AGE_MS oder -1 (noch nie ein Bild); fehlt der Wert/ist er kein int -> nichts;
          4. nginx-Zaehler bytes_in der Kamera ueber mindestens STALL_OBS_S beobachtet, Rate >= STALL_MIN_BPS
             (neue Anmeldung = Zaehler kleiner, nginx nicht lesbar/Kamera fehlt -> Beobachtung beginnt neu);
          5. Bremse: letzter Eingriff auf diesem Platz mindestens STALL_COOLDOWN_S her.
        Ablauf bei einem haengenden Zubringer: nach mehr als 5 s ohne Bild Verdacht, 2 s spaeter Eingriff, nach 1 s neu."""
        stats = self._fresh_stats(mono)
        if stats is None:
            self.stall_obs.clear()
            return
        slots = stats.get("slots")
        slots = slots if isinstance(slots, list) else []
        suspect = {}
        for slot, (since, cam) in self.feeders.running_since().items():
            info = slots[slot] if 0 <= slot < len(slots) and isinstance(slots[slot], dict) else {}
            age = info.get("age_ms")
            if (mono - since < STALL_GRACE_S or not isinstance(age, int) or isinstance(age, bool)
                    or 0 <= age <= STALL_AGE_MS):
                continue                 # Anlaufzeit, unbekannt oder gesund
            suspect[slot] = (cam, age)
        for slot in list(self.stall_obs):
            if slot not in suspect:
                del self.stall_obs[slot]
        if not suspect:
            return
        counters = read_bytes_in() or {}
        for slot, (cam, age) in suspect.items():
            b = counters.get(cam)
            prev = self.stall_obs.get(slot)
            if b is None:                # nginx nicht lesbar oder Kamera dort weg: keine Aussage, neu beobachten
                self.stall_obs.pop(slot, None)
                continue
            if prev is None or b < prev[1] or prev[2] != cam:
                self.stall_obs[slot] = (mono, b, cam)
                continue
            dt = mono - prev[0]
            if dt < STALL_OBS_S:
                continue
            self.stall_obs[slot] = (mono, b, cam)
            rate = (b - prev[1]) / dt
            if rate < STALL_MIN_BPS:
                continue                 # nginx bekommt selbst kaum Daten: Neustart braechte nichts
            if mono - self.last_kick.get(slot, float("-inf")) < STALL_COOLDOWN_S:
                continue                 # Bremse
            why = "seit %d ms kein Bild" % age if age >= 0 else "noch kein Bild"
            if self.feeders.kick(slot, "%s, nginx empfaengt weiter %d KB/s" % (why, rate / 1024)):
                self.last_kick[slot] = mono
                self.stall_kicks += 1

    def restart_encoder(self, pid, text, cause):
        try:
            atomic_write(BELA_TMP, text, 0o644)
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            self.last_error = "Neustart fehlgeschlagen: %s" % e
            log(self.last_error)
            return
        self.restarts += 1
        self.last_restart = time.monotonic()
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
                    ours = is_ours(open(BELA_TMP).read())
                except OSError:
                    pass
            mode = self.mode()
            mixer = None
            try:
                mixer = json.load(open(MIX2_CONF + ".stats"))
            except Exception:
                pass
            cams = [{"key": k, "width": v[0], "height": v[1], "since": int(self.live_since.get(k, time.time()))}
                    for k, v in sorted(self.live.items())]
            lay = None
            if self.layout:
                L = self.layout
                lay = {"mode": "v2", "scene": L["scene"], "latency": L["latency"], "audio": L["audio"],
                       "slots": L["slots"], "layers": L["layers"]}
            return {"version": VERSION, "code": CODE, "cameras": cams, "config": self.cfg, "auto_restart": self.auto_restart,
                    "mode": mode, "mode_wanted": "v2", "plugin2": os.path.exists(PLUGIN2_FILE),
                    "scenes": [s["name"] for s in self.cfg.get("scenes", [])], "active_scene": self.cfg.get("active_scene", ""),
                    "layout": lay, "streaming": {"active": bela is not None, "ours": ours, "pid": bela[0] if bela else None},
                    "pending_restart": self.pending_restart, "restarts": self.restarts, "mixer": mixer,
                    "feeders": self.feeders.state(), "stall_restarts": self.stall_kicks,
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
        if path == "/api/snapshot":
            qs = urllib.parse.parse_qs(self.path.partition("?")[2])
            cam = (qs.get("cam") or [""])[0]
            if not gen2.CAM_RE.match(cam or ""):
                return self._send(400, {"error": "ungueltiger Kamera-Name"})
            with CORE.lock:
                live = dict(CORE.live)
            res = capture_snapshot(cam, live)
            return self._send(200 if res.get("ok") else 502, res)
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
        if path == "/api/scene":
            name = body.get("name") if isinstance(body, dict) else None
            if not isinstance(name, str) or not name:
                return self._send(400, {"error": "Szenen-Name fehlt"})
            try:
                res = CORE.set_scene(name)
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            return self._send(200, {"ok": True, **res})
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
    def _term(*_):
        CORE.feeders.stop_all()      # Zubringer sauber beenden (laufen in eigener Sitzung)
        os._exit(0)
    signal.signal(signal.SIGTERM, _term)
    srv.serve_forever()


if __name__ == "__main__":
    main()
