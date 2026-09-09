#!/usr/bin/env python3
"""IRL Streamer OS - Host-Control-Watcher (Nutzerwunsch 07.09.2026: OBS-
Notfallknopf + PC-Neustart-Knopf im Diagnose-Dashboard).

Das Dashboard laeuft in einem Docker-Container (network_mode: host, aber
KEIN Zugriff auf Host-Prozesse/den echten Reboot-Befehl). OBS laeuft als
normales Desktop-Programm in der grafischen Sitzung des Nutzers 'streamer'
- weder im Container erreichbar noch kann der Container 'systemctl reboot'
fuer den echten Host ausloesen.

Dieses Skript laeuft stattdessen als root-systemd-Dienst DIREKT auf dem
Host (nicht im Container) und beobachtet einen gemeinsamen, beschreibbaren
Ordner (siehe HOST_CONTROL_DIR, per docker-compose.yml read-write in den
Container gemountet). Der Container schreibt dort eine Trigger-Datei
('<action>.trigger', leerer Inhalt reicht), dieses Skript fuehrt die
passende Aktion aus und schreibt danach eine Ergebnisdatei
('<action>.result', JSON) zurueck, die der Container abpollt.

Bewusst simples Datei-Polling statt inotify/watchdog-Bibliothek - keine
zusaetzliche Python-Abhaengigkeit noetig, 1x pro Sekunde reicht fuer diesen
Anwendungsfall (Start/Stop/Reboot sind seltene, manuelle Aktionen, keine
Echtzeitanforderung).
"""
import json
import os
import subprocess
import time
from pathlib import Path

CONTROL_DIR = Path("/var/lib/irl-streamer-host-control")
TARGET_USER = "streamer"
POLL_SECONDS = 1


def log(msg):
    print(f"[irl-streamer-host-control] {msg}", flush=True)


def _user_systemctl_env() -> dict:
    """Umgebung fuer 'systemctl --user'-Aufrufe als der eingeloggte
    Desktop-Nutzer - identisches Muster wie in provision.sh an mehreren
    Stellen (XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS aus der UID
    abgeleitet). Kein loginctl enable-linger noetig, da 'streamer' bereits
    per Autologin dauerhaft grafisch eingeloggt ist - die Session/der
    systemd --user-Dienst existiert also bereits.
    """
    uid = subprocess.run(
        ["id", "-u", TARGET_USER], capture_output=True, text=True, check=True
    ).stdout.strip()
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    return env


def _run_as_streamer(args: list[str]) -> subprocess.CompletedProcess:
    # WICHTIG (Bug gefunden 07.09.2026, live reproduziert): 'sudo -u
    # streamer -E' gibt die Python-seitig gesetzten Umgebungsvariablen
    # NICHT weiter, wenn sudoers dafuer keine explizite
    # 'Defaults:streamer setenv'/env_keep-Freigabe hat (Standard-sudoers
    # hat die nicht) - sudo ignoriert '-E' in dem Fall einfach lautlos,
    # OHNE Fehler. 'systemctl --user' bekam dadurch weder
    # XDG_RUNTIME_DIR noch DBUS_SESSION_BUS_ADDRESS und schlug mit
    # "$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined" fehl.
    # Fix: die beiden Variablen stattdessen explizit ALS TEIL DES
    # AUSGEFUEHRTEN BEFEHLS ueber 'env VAR=wert ...' setzen - das
    # funktioniert unabhaengig von sudoers-env-Policies, weil es kein
    # Environment-Preserving von sudo selbst braucht.
    env = _user_systemctl_env()
    env_args = [f"XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']}", f"DBUS_SESSION_BUS_ADDRESS={env['DBUS_SESSION_BUS_ADDRESS']}"]
    return subprocess.run(
        ["sudo", "-u", TARGET_USER, "env"] + env_args + args,
        capture_output=True, text=True, timeout=20,
    )


def handle_obs_start() -> dict:
    result = _run_as_streamer(["systemctl", "--user", "start", "irl-streamer-obs.service"])
    if result.returncode == 0:
        log("OBS gestartet (irl-streamer-obs.service)")
        return {"ok": True}
    log(f"OBS-Start fehlgeschlagen: {result.stderr.strip()}")
    return {"ok": False, "error": result.stderr.strip() or "systemctl start fehlgeschlagen"}


def handle_obs_stop() -> dict:
    # 'systemctl --user stop' sendet SIGTERM an die Haupt-PID der Unit -
    # das ist OBS selbst (siehe irl-streamer-obs.service-Kommentar,
    # ExecStart endet mit 'exec obs ...'), also ein sauberes Beenden.
    result = _run_as_streamer(["systemctl", "--user", "stop", "irl-streamer-obs.service"])
    if result.returncode == 0:
        log("OBS beendet (irl-streamer-obs.service)")
        return {"ok": True}
    log(f"OBS-Stop fehlgeschlagen: {result.stderr.strip()}")
    return {"ok": False, "error": result.stderr.strip() or "systemctl stop fehlgeschlagen"}


def handle_reboot() -> dict:
    log("Reboot angefordert - fahre in Kuerze neu")
    # Kein Blockieren hier auf das Ergebnis - der Prozess/die Maschine
    # verschwindet ohnehin gleich. Die Ergebnisdatei wird VOR dem
    # eigentlichen Reboot-Aufruf geschrieben (siehe main-Loop unten), damit
    # der Container ueberhaupt noch eine Bestaetigung sehen kann, bevor der
    # Host offline geht.
    subprocess.Popen(["systemctl", "reboot"])
    return {"ok": True}


ACTIONS = {
    "obs_start": handle_obs_start,
    "obs_stop": handle_obs_stop,
    "reboot": handle_reboot,
}


def _obs_process_running() -> bool:
    """Reine Prozess-Existenz-Pruefung - laeuft direkt auf dem Host (nicht
    im Container), sieht daher den echten OBS-Prozess der grafischen
    Sitzung ganz normal ueber pgrep."""
    try:
        result = subprocess.run(["pgrep", "-x", "obs"], capture_output=True, timeout=3)
        return result.returncode == 0
    except Exception:
        return False


def write_obs_status():
    """Schreibt periodisch den OBS-Laufstatus in eine gemeinsame Datei, die
    main.py im Container fuer die Gruen/Rot-Anzeige des OBS-Notfallknopfes
    ausliest (main.py selbst kann OBS nicht direkt sehen, siehe Modul-
    Docstring oben)."""
    status_file = CONTROL_DIR / "obs_status.json"
    tmp_file = CONTROL_DIR / "obs_status.json.tmp"
    data = {"running": _obs_process_running(), "checked_at": time.time()}
    # Ueber eine .tmp-Datei + os.replace() schreiben (atomarer Rename statt
    # direktem Ueberschreiben) - verhindert, dass main.py im Container
    # ausgerechnet mitten im Schreibvorgang eine kaputte/halbe JSON-Datei
    # zu lesen bekommt (der Container pollt alle paar Sekunden parallel).
    tmp_file.write_text(json.dumps(data))
    os.replace(tmp_file, status_file)


def main():
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Watcher gestartet, beobachte {CONTROL_DIR}")
    while True:
        for trigger_file in CONTROL_DIR.glob("*.trigger"):
            action = trigger_file.stem
            handler = ACTIONS.get(action)
            result_file = CONTROL_DIR / f"{action}.result"
            if handler is None:
                log(f"Unbekannte Aktion in Trigger-Datei ignoriert: {action}")
                trigger_file.unlink(missing_ok=True)
                continue
            log(f"Trigger empfangen: {action}")
            try:
                result = handler()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
                log(f"Aktion '{action}' warf eine Ausnahme: {exc}")
            result_file.write_text(json.dumps(result))
            trigger_file.unlink(missing_ok=True)
        write_obs_status()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
