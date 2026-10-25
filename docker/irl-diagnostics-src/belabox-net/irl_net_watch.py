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

Aufruf: irl_net_watch.py            Dauerlauf (Dienst irl-net-watch.service)
        irl_net_watch.py --once     eine Pruefung, Ergebnis als JSON (Test/Diagnose)
"""
import json
import os
import signal
import subprocess
import sys
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


def main():
    w = Watch()
    once = "--once" in sys.argv
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
