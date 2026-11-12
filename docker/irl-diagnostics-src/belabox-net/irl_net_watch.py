#!/usr/bin/env python3
"""irl-net-watch - Haenger-Waechter fuer die Sendekette der BELABOX (IRL Streamer OS).

Idee nach IRL4YOU BOX (pipbox_watch.py, MIT-Lizenz, (c) IRL4YOU), fuer belaUI/belacoder neu geschrieben.

belacoder beendet sich bei einem Pipeline-Stillstand selbst (stall_check) und belaUI startet ihn neu. Haengt aber seine
Hauptschleife (Deadlock, Treiber im Zustand D, ...), laeuft auch dieser Selbstschutz nicht mehr: die Sendung steht, ohne
dass etwas passiert. Dieser Waechter liest die Kennzahlen-Datei, die der gepatchte belacoder jede Sekunde neu schreibt
(BELACODER_STATS_FILE), und greift ein, wenn waehrend einer laufenden Sendung
  - die Datei laenger als STALE_S nicht erneuert wurde (Hauptschleife haengt), oder
  - der Encoder laenger als NOFRAME_S keine Bilder liefert (fps < 1), oder
  - laenger als NOSEND_S kein einziges Paket mehr gesendet wurde.
Eingriff: Diagnosebericht schreiben, belacoder sanft beenden (SIGTERM, nach 5 s SIGKILL) - belaUI startet ihn sofort neu.
Bremsen: erst START_GRACE_S nach dem Start von belacoder, danach COOLDOWN_S Pause, hoechstens MAX_PER_HOUR Eingriffe je Stunde.

Flugschreiber (Review 11.11.2026 C6, eigener Faden): alle FLIGHT_EVERY_S eine kurze Zeile mit Kennwerten der Box
(Temperatur, Takt, Last, Speicher, belacoder/srtla_send, Bitrate, Sendewerte je Netzweg, Bild-in-Bild), sofort per
fdatasync auf der Platte, damit nach einem harten Haenger die letzten Sekunden lesbar sind (siehe Abschnitt unten).

Aufruf: irl_net_watch.py            Dauerlauf (Dienst irl-net-watch.service)
        irl_net_watch.py --once     eine Pruefung, Ergebnis als JSON (Test/Diagnose)
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

STATS = os.environ.get("IRL_NET_STATS", "/run/irl-net/belacoder.json")
RUN = os.environ.get("IRL_NET_RUN", "/run/irl-net")
LOGDIR = os.environ.get("IRL_NET_LOGDIR", "/var/log/irl-net")
REPORT = "haenger-diagnose.txt"
INTERVAL = 2.0
START_GRACE_S = 30.0
STALE_S = 15.0
NOFRAME_S = 15.0
NOSEND_S = 15.0
COOLDOWN_S = 90.0
MAX_PER_HOUR = 4
REPORT_KEEP = 6
REPORT_MARK = "===== HAENGER "


def belacoder_pid():
    """PID des laufenden belacoder (der Prozess heisst so, auch wenn die Datei per dpkg-divert ersetzt ist)."""
    try:
        out = subprocess.run(["pgrep", "-x", "belacoder"], capture_output=True, text=True, timeout=5).stdout.split()
        return int(out[0]) if out else None
    except Exception:
        return None


def proc_age(pid):
    """Sekunden seit dem Start des Prozesses."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            start_ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        return up - start_ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return 0.0


def read_stats(path=STATS):
    """(Inhalt oder None, Aenderungszeit oder None). Das Alter zaehlt auch, wenn der Inhalt unlesbar ist."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None, None
    try:
        with open(path) as f:
            data = json.load(f)
        return (data if isinstance(data, dict) else None), mtime
    except Exception:
        return None, mtime


class Watch:
    def __init__(self):
        self.pid = None
        self.noframe_since = None
        self.last_sent = None
        self.sent_since = None
        self.acted = []          # Zeitpunkte der Eingriffe (letzte Stunde)
        self.last_act = 0.0
        self.last_reason = ""

    def check(self, now, pid, age, stats, mtime):
        """Reine Entscheidung (testbar). Rueckgabe: Grund fuer einen Eingriff oder None."""
        if pid != self.pid:                         # neuer belacoder (oder keiner): Zaehler zuruecksetzen
            self.pid, self.noframe_since, self.last_sent, self.sent_since = pid, None, None, None
        if pid is None or age < START_GRACE_S:
            return None
        if mtime is None:
            return None                             # keine Kennzahlen (ungepatchter belacoder): nichts tun
        # Datei aelter als der laufende Prozess = Rest des Vorgaengers, noch keine eigene Messung
        if now - mtime > age:
            return None
        if now - mtime > STALE_S:
            return "Kennzahlen seit %.0f s nicht erneuert (belacoder haengt)" % (now - mtime)
        fps = float((stats or {}).get("fps", 0) or 0)
        if fps < 1.0:
            self.noframe_since = self.noframe_since or now
            if now - self.noframe_since > NOFRAME_S:
                return "Encoder liefert seit %.0f s keine Bilder" % (now - self.noframe_since)
        else:
            self.noframe_since = None
        sent = (stats or {}).get("sent_total")
        if isinstance(sent, int) and sent >= 0:
            if sent != self.last_sent:
                self.last_sent, self.sent_since = sent, now
            elif self.sent_since and now - self.sent_since > NOSEND_S:
                return "seit %.0f s kein Paket gesendet" % (now - self.sent_since)
        return None

    def may_act(self, now):
        self.acted = [t for t in self.acted if now - t < 3600]
        return now - self.last_act >= COOLDOWN_S and len(self.acted) < MAX_PER_HOUR


def sh(cmd, limit=6000):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout[-limit:]
    except Exception as exc:
        return "(%s)" % exc


def write_report(reason, pid, stats):
    os.makedirs(LOGDIR, exist_ok=True)
    path = os.path.join(LOGDIR, REPORT)
    parts = [REPORT_MARK + time.strftime("%Y-%m-%d %H:%M:%S") + " =====", "Grund: " + reason, "belacoder PID: %s" % pid,
             "Kennzahlen: " + json.dumps(stats or {}),
             "--- Faeden von belacoder (Zustand/wchan) ---",
             sh("for t in /proc/%s/task/*; do echo \"$(cat $t/comm) $(awk '{print $3}' $t/stat) $(cat $t/wchan 2>/dev/null)\"; done" % pid),
             "--- Prozesse im Zustand D ---", sh("ps -eo pid,stat,wchan:30,comm | awk '$2 ~ /D/'"),
             "--- Last/Speicher ---", sh("uptime; free -m"),
             "--- dmesg (Ende) ---", sh("dmesg | tail -n 30"),
             "--- Bild-in-Bild ---", sh("/usr/local/sbin/irl-pip-cli state 2>/dev/null | head -c 1500"), ""]
    old = ""
    try:
        with open(path) as f:
            old = f.read()
    except OSError:
        pass
    blocks = [b for b in old.split(REPORT_MARK) if b.strip()][-(REPORT_KEEP - 1):]
    with open(path + ".tmp", "w") as f:
        f.write("".join(REPORT_MARK + b for b in blocks) + "\n".join(parts))
    os.replace(path + ".tmp", path)


def stop_belacoder(pid):
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        pass


def write_status(w, last_check):
    os.makedirs(RUN, exist_ok=True)
    data = {"version": 1, "last_check": last_check, "interventions_last_hour": len(w.acted),
            "last_intervention": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(w.last_act)) if w.last_act else None,
            "last_reason": w.last_reason}
    with open(os.path.join(RUN, "watch.json.tmp"), "w") as f:
        json.dump(data, f)
    os.replace(os.path.join(RUN, "watch.json.tmp"), os.path.join(RUN, "watch.json"))


# ---------------------------------------------------------------- Flugschreiber (Review 11.11.2026 C6)
# Nach einem harten Haenger (HW-Lockup, nur Strom aus/an hilft) wusste niemand, was davor lief: der 30-s-Logger des
# Langzeittests vom 06.11. schrieb ohne fsync, sein letzter Eintrag lag 24 s vor dem Haenger. Deshalb hier alle
# FLIGHT_EVERY_S eine Zeile (~250 Byte), angehaengt und per fdatasync sofort auf der Platte.
# - Ring aus zwei Dateien (flugschreiber.log + .1) je FLIGHT_MAX_BYTES -> hoechstens 2 MB, reicht fuer ~2 x 11 h.
# - Ablage unter /var/lib, nicht /var/log: manche Board-Images legen /var/log in den RAM (z. B. armbian-ramlog), dann
#   waere nach einem Haenger alles weg.
# - START-Zeile beim Start, STOP-Zeile bei SIGTERM (Herunterfahren, Dienst-Neustart). Endet die Datei beim Start mit
#   einem ANDEREN Boot ohne STOP, ist die Box unsauber neu gestartet (Haenger oder Strom weg): die letzten
#   FLIGHT_KEEP_LINES Zeilen dieses Boots kommen nach letzter-haenger.txt (net-setup.sh status -> Dashboard).
# - Eigener Faden: ein haengender Plattenzugriff darf den Haenger-Waechter nie aufhalten; jeder Fehler wird geschluckt.
# - Bewusst NICHT: journald-SyncIntervalSec, pstore, automatisch weitersenden (Block-4-Entscheidung, Plan C6 Punkte 2-4).
FLIGHT_DIR = os.environ.get("IRL_NET_FLIGHTDIR", "/var/lib/irl-net")
FLIGHT_LOG = "flugschreiber.log"
FLIGHT_LAST = "letzter-haenger.txt"
FLIGHT_EVERY_S = 10.0
FLIGHT_MAX_BYTES = 1024 * 1024
FLIGHT_KEEP_LINES = 30
PROC = os.environ.get("IRL_NET_PROC", "/proc")
SYSROOT = os.environ.get("IRL_NET_SYSROOT", "/sys")
PIP_RUN = os.environ.get("IRL_NET_PIP_RUN", "/run/irl-pip")
PIP_FRESH_S = 15.0            # Bild-in-Bild-Statistik aelter -> Mischer laeuft nicht (keine Platz-Werte schreiben)
_BOOT_RE = re.compile(r"(?:^|\s)boot=(\S+)")


def _read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _first_float(text, div=1.0):
    try:
        return float(text.split()[0]) / div
    except Exception:
        return None


def _utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def boot_id(proc=None):
    """Erste 8 Zeichen der Kernel-boot_id (wie im Langzeit-Log), '-' wenn unlesbar."""
    return _read(os.path.join(proc or PROC, "sys/kernel/random/boot_id")).strip().replace("-", "")[:8] or "-"


def scan_procs(proc=None):
    """Ein Durchlauf durch /proc: belacoder-PID, srtla_send lebt, Anzahl Bild-in-Bild-Zubringer, Schnappschuss laeuft."""
    proc = proc or PROC
    res = {"bc": 0, "srtla": 0, "pip": 0, "snap": 0}
    try:
        names = os.listdir(proc)
    except OSError:
        return res
    for n in names:
        if not n.isdigit():
            continue
        comm = _read(os.path.join(proc, n, "comm")).strip()
        if comm == "belacoder":
            res["bc"] = res["bc"] or int(n)
        elif comm == "srtla_send":
            res["srtla"] = 1
        elif comm.startswith("gst-launch"):              # Zubringer und Schnappschuss sind gst-launch-1.0
            cmd = _read(os.path.join(proc, n, "cmdline"))
            if "irlpip2vsink" in cmd:
                res["pip"] += 1
            elif "irl-pip-snap-" in cmd:
                res["snap"] = 1
    return res


def tx_counters(proc=None, sysroot=None):
    """Gesendete Bytes je Netzweg (/proc/net/dev); ohne lo, ohne WireGuard (Verwaltungstunnel), ohne Karten im Zustand down."""
    proc, sysroot = proc or PROC, sysroot or SYSROOT
    out = {}
    for line in _read(os.path.join(proc, "net/dev")).splitlines()[2:]:
        name, sep, rest = line.partition(":")
        name = name.strip()
        if not sep or name == "lo" or name.startswith("wg"):
            continue
        if _read(os.path.join(sysroot, "class/net", name, "operstate")).strip() == "down":
            continue
        try:
            out[name] = int(rest.split()[8])
        except (IndexError, ValueError):
            pass
    return out


def flight_line(now, prev, proc=None, sysroot=None, stats_path=None, pip_run=None):
    """Eine Zeile Kennwerte, rein lesend, jede Quelle einzeln abgesichert ('-' = nicht lesbar / nicht zutreffend).
    prev: Zaehlerstaende der letzten Zeile (kbit/s je Netzweg). Rueckgabe: (Zeile, neue Zaehlerstaende)."""
    proc, sysroot = proc or PROC, sysroot or SYSROOT
    stats_path, pip_run = stats_path or STATS, pip_run or PIP_RUN
    f = {"boot": boot_id(proc)}
    up = _first_float(_read(os.path.join(proc, "uptime")))
    f["up"] = "%d" % up if up is not None else "-"
    temps = [t for t in (_first_float(_read(p), 1000.0) for p in glob.glob(os.path.join(sysroot, "class/thermal/thermal_zone*/temp")))
             if t is not None]
    f["temp"] = "%.1f" % max(temps) if temps else "-"
    ddr = [d for d in (_first_float(_read(p), 1e6) for p in glob.glob(os.path.join(sysroot, "class/devfreq/*dmc*/cur_freq")))
           if d is not None]
    f["ddr"] = "%d" % max(ddr) if ddr else "-"
    cpu = [c for c in (_first_float(_read(p), 1000.0) for p in glob.glob(os.path.join(sysroot, "devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")))
           if c is not None]
    f["cpu"] = "%d" % max(cpu) if cpu else "-"
    load = _first_float(_read(os.path.join(proc, "loadavg")))
    f["load"] = "%.2f" % load if load is not None else "-"
    mem = "-"
    for line in _read(os.path.join(proc, "meminfo")).splitlines():
        if line.startswith("MemAvailable:"):
            kb = _first_float(line.split(":", 1)[1])
            mem = "%dM" % (kb // 1024) if kb is not None else "-"
    f["memfrei"] = mem
    p = scan_procs(proc)
    f["bc"] = str(p["bc"])
    for k in ("fps", "kbps", "rtt", "buf", "drop", "bcalt"):
        f[k] = "-"
    if p["bc"]:
        stats, mtime = read_stats(stats_path)
        st = stats or {}
        for k, src, fmt in (("fps", "fps", "%.1f"), ("kbps", "bitrate_kbps", "%d"), ("rtt", "rtt_ms", "%d"),
                            ("buf", "snd_buf_ms", "%d"), ("drop", "drop_total", "%d")):
            try:
                f[k] = fmt % float(st[src])
            except (KeyError, TypeError, ValueError):
                pass
        if mtime is not None:
            f["bcalt"] = "%d" % max(0, now - mtime)          # Alter der Kennzahlen-Datei (haengt belacoder?)
    f["srtla"] = str(p["srtla"])
    tx = tx_counters(proc, sysroot)
    dt = now - prev.get("_t", now)
    rates = ["%s:%d" % (n, max(0, b - prev[n]) * 8 / 1000.0 / dt) for n, b in sorted(tx.items()) if n in prev and dt > 0]
    f["tx"] = ",".join(rates) or "-"                       # kbit/s je Netzweg seit der letzten Zeile
    f["pip"] = str(p["pip"])
    f["age"] = f["ticks"] = f["szene"] = "-"
    spath = os.path.join(pip_run, "mix2.conf.stats")
    try:
        if now - os.stat(spath).st_mtime < PIP_FRESH_S:
            with open(spath) as fh:
                ms = json.load(fh)
            ages = [str(s.get("age_ms", "-")) if (s.get("in") or 0) > 0 else "-" for s in ms.get("slots", [])]
            while ages and ages[-1] == "-":
                ages.pop()
            f["age"] = ",".join(ages) or "-"            # ms seit dem letzten Bild je Platz (-1 = noch nie)
            f["ticks"] = str(int(ms.get("ticks", 0)))
    except Exception:
        pass
    try:
        f["szene"] = "%d" % max(0, now - os.stat(os.path.join(pip_run, "mix2.conf")).st_mtime)   # s seit Szenenwechsel
    except OSError:
        pass
    f["snap"] = str(p["snap"])
    tx["_t"] = now
    return _utc(now) + " " + " ".join("%s=%s" % kv for kv in f.items()), tx


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Flight:
    def __init__(self, dirpath=None, proc=None):
        self.dir = dirpath or FLIGHT_DIR
        self.path = os.path.join(self.dir, FLIGHT_LOG)
        self.proc = proc
        self.boot = boot_id(proc)
        self.prev = {}
        self.lock = threading.RLock()
        self.stopped = False

    def append(self, line, final=False):
        """Zeile anhaengen und per fdatasync sofort auf die Platte bringen; volle Datei wird zu .1 (Ring).
        Nach der STOP-Zeile (final) wird nichts mehr geschrieben - sonst hielte der naechste Boot ein sauberes
        Herunterfahren fuer einen Haenger."""
        with self.lock:
            if self.stopped:
                return
            os.makedirs(self.dir, exist_ok=True)
            new = False
            try:
                if os.path.getsize(self.path) >= FLIGHT_MAX_BYTES:
                    os.replace(self.path, self.path + ".1")
                    new = True
            except FileNotFoundError:
                new = True
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, (line.rstrip("\n") + "\n").encode())
                os.fdatasync(fd)
            finally:
                os.close(fd)
            self.stopped = final
            if new:                                      # neue Datei/Umbenennung selbst dauerhaft machen (einmal je Runde)
                _fsync_dir(self.dir)

    def check_unclean(self):
        """Letzte Zeile gehoert zu einem anderen Boot und ist kein STOP -> Box ist unsauber neu gestartet. Die letzten
        FLIGHT_KEEP_LINES Zeilen dieses Boots nach letzter-haenger.txt. Rueckgabe: letzte Zeile des alten Boots oder None.
        Nur Zeilen mit boot= zaehlen: ein beim Stromausfall zerrissener Rest (Nullbytes) am Dateiende wird uebergangen."""
        lines = [ln.replace("\0", "") for p in (self.path + ".1", self.path) for ln in _read(p).splitlines()]
        recs = [(m.group(1), ln) for ln in lines for m in [_BOOT_RE.search(ln)] if m]
        if not recs:
            return None
        old, last = recs[-1]
        if old == self.boot or last.split()[1:2] == ["STOP"]:
            return None
        tail = []
        for b, ln in reversed(recs):
            if b != old or len(tail) >= FLIGHT_KEEP_LINES:
                break
            tail.insert(0, ln)
        text = ("# Unsauberer Neustart der Belabox (Haenger oder Strom weg): Boot %s endete ohne STOP-Zeile.\n"
                "# Erkannt %s im neuen Boot %s. Zeiten in UTC, Felder siehe irl_net_watch.py flight_line().\n"
                % (old, _utc(time.time()), self.boot)) + "\n".join(tail) + "\n"
        path = os.path.join(self.dir, FLIGHT_LAST)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, text.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(path + ".tmp", path)
        _fsync_dir(self.dir)
        return tail[-1]

    def start(self):
        os.makedirs(self.dir, exist_ok=True)
        try:
            last = self.check_unclean()
            if last:
                print("Flugschreiber: unsauberer Neustart erkannt, letzte Zeile davor: " + last, flush=True)
        except Exception as exc:
            print("Flugschreiber: Pruefung auf unsauberen Neustart fehlgeschlagen: %s" % exc, flush=True)
        up = _first_float(_read(os.path.join(self.proc or PROC, "uptime"))) or 0.0
        torn = False
        try:
            with open(self.path, "rb") as fh:            # zerrissene letzte Zeile (Strom weg): START auf neuer Zeile
                fh.seek(-1, os.SEEK_END)
                torn = fh.read(1) != b"\n"
        except OSError:
            pass
        self.append("%s%s START boot=%s up=%d" % ("\n" if torn else "", _utc(time.time()), self.boot, up))

    def record(self, now=None):
        line, self.prev = flight_line(now or time.time(), self.prev, proc=self.proc)
        self.append(line)

    def stop(self):
        self.append("%s STOP boot=%s" % (_utc(time.time()), self.boot), final=True)


def flight_loop(fl, stop_evt):
    """Alle FLIGHT_EVERY_S eine Zeile (monotoner Takt). Fehler nur melden, wenn sich die Meldung aendert."""
    next_t, last_err = time.monotonic(), ""
    while not stop_evt.is_set():
        try:
            fl.record()
            last_err = ""
        except Exception as exc:                          # Flugschreiber darf nie den Dienst beenden
            if str(exc) != last_err:
                last_err = str(exc)
                print("Flugschreiber: Schreiben fehlgeschlagen: %s" % exc, flush=True)
        next_t += FLIGHT_EVERY_S
        mono = time.monotonic()
        if next_t < mono:                                 # Platte hing laenger als ein Takt: neu ansetzen statt nachholen
            next_t = mono + FLIGHT_EVERY_S
        stop_evt.wait(next_t - mono)


def start_flight():
    """Flugschreiber starten (START-Zeile, Faden, STOP bei SIGTERM). Fehler -> Waechter laeuft ohne Flugschreiber."""
    try:
        fl = Flight()
        fl.start()
    except Exception as exc:
        print("Flugschreiber aus: %s" % exc, flush=True)
        return None
    evt = threading.Event()

    def on_term(signum, frame):
        evt.set()
        try:
            fl.stop()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    threading.Thread(target=flight_loop, args=(fl, evt), name="flugschreiber", daemon=True).start()
    return fl


def main():
    w = Watch()
    once = "--once" in sys.argv
    if not once:
        start_flight()
    while True:
        now = time.time()
        pid = belacoder_pid()
        stats, mtime = read_stats()
        reason = w.check(now, pid, proc_age(pid) if pid else 0.0, stats, mtime)
        if once:
            print(json.dumps({"pid": pid, "stats_age": round(now - mtime, 1) if mtime else None, "reason": reason}))
            return 0
        if reason and w.may_act(now):
            w.last_act, w.last_reason = now, reason
            w.acted.append(now)
            print("Eingriff: " + reason, flush=True)
            try:
                write_report(reason, pid, stats)
            except Exception as exc:
                print("Bericht fehlgeschlagen: %s" % exc, flush=True)
            stop_belacoder(pid)
        try:
            write_status(w, time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
