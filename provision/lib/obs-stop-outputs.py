#!/usr/bin/env python3
"""IRL Streamer OS - vor dem Beenden von OBS Sendung und Aufnahme sauber stoppen.

Laeuft als ExecStop der OBS-Unit (irl-streamer-obs.service), also bei JEDEM
'systemctl --user stop': Dashboard-Knopf "OBS stoppen", Sicherung/Wiederherstellung,
Herunterfahren (Shutdown-Inhibitor), Abmelden.

Warum (Review 12.11.2026, auf der Test-VM gemessen): OBS beendet sich bei SIGTERM ueber
QApplication::quit(). Qt 6 schliesst dabei zuerst die Fenster, und laeuft gerade eine
Sendung oder Aufnahme, fragt OBS "Wirklich beenden?" (ConfirmOnExit, Standard an) und
wartet auf einen Klick, den niemand macht. Jeder Stop hing dann bis zum systemd-Timeout
(90 s), danach SIGKILL - eine Aufnahme wurde nicht abgeschlossen, Einstellungen nicht
gespeichert. Dieses Skript stoppt vorher Sendung, Aufnahme, Replay-Puffer und virtuelle
Kamera per obs-websocket und wartet, bis alle Ausgaben aus sind; danach schickt systemd
das SIGTERM und OBS schliesst ohne Rueckfrage. Die Rueckfrage beim Klick auf X bleibt.

Nie fatal: laeuft OBS nicht (schon beendet, Absturz) oder antwortet es nicht, endet das
Skript sofort mit 0 - systemd beendet OBS dann wie bisher.
"""
import contextlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

OBS_HOST = os.environ.get("IRL_OBS_HOST", "127.0.0.1")
OBS_PORT = int(os.environ.get("IRL_OBS_PORT", "4455"))
WS_CONFIG = Path(os.environ.get(
    "IRL_OBS_WS_CONFIG", str(Path.home() / ".config/obs-studio/plugin_config/obs-websocket/config.json")))
CONNECT_TIMEOUT = 3.0
MAX_WAIT = float(os.environ.get("IRL_OBS_STOP_MAX_WAIT", "20"))

# (Status-Abfrage, Stop-Befehl) - gleiche Ausgaben, die OBS fuer die Rueckfrage zaehlt
OUTPUTS = (
    ("GetStreamStatus", "StopStream"),
    ("GetRecordStatus", "StopRecord"),
    ("GetReplayBufferStatus", "StopReplayBuffer"),
    ("GetVirtualCamStatus", "StopVirtualCam"),
)


def log(msg):
    print(f"[irl-obs-stop-outputs] {msg}", flush=True)


def _ws_module():
    """WebSocket-Client aus stream-active.py (gleiches Verzeichnis, nur Standardbibliothek)."""
    path = Path(__file__).with_name("stream-active.py")
    spec = importlib.util.spec_from_file_location("irl_stream_active", path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ws_password():
    try:
        cfg = json.loads(WS_CONFIG.read_text())
    except (OSError, ValueError):
        return ""
    return cfg.get("server_password") or ""


class ObsSession:
    def __init__(self, ws, auth_fn, password):
        self.ws = ws
        self.n = 0
        hello = ws.recv()
        ident = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            ident["d"]["authentication"] = auth_fn(password, auth["salt"], auth["challenge"])
        ws.send(ident)
        if ws.recv().get("op") != 2:
            raise ConnectionError("OBS-Identify fehlgeschlagen")

    def request(self, req):
        """(ok, responseData). ok=False z.B. wenn es die Ausgabe nicht gibt (kein Replay-Puffer)."""
        self.n += 1
        rid = str(self.n)
        self.ws.send({"op": 6, "d": {"requestType": req, "requestId": rid}})
        while True:
            msg = self.ws.recv()
            if msg.get("op") == 7 and (msg.get("d") or {}).get("requestId") == rid:
                d = msg["d"]
                return bool((d.get("requestStatus") or {}).get("result")), d.get("responseData") or {}


def active_outputs(s):
    aktiv = []
    for status_req, stop_req in OUTPUTS:
        ok, data = s.request(status_req)
        if ok and data.get("outputActive"):
            aktiv.append((status_req, stop_req))
    return aktiv


def stop_outputs(connect, auth_fn, password, max_wait=MAX_WAIT, sleep=time.sleep):
    try:
        ws = connect()
    except OSError as exc:
        log(f"OBS-Websocket nicht erreichbar ({type(exc).__name__}) - nichts zu tun")
        return 0
    try:
        s = ObsSession(ws, auth_fn, password)
        aktiv = active_outputs(s)
        if not aktiv:
            log("keine Sendung/Aufnahme aktiv")
            return 0
        for _, stop_req in aktiv:
            ok, _ = s.request(stop_req)
            log(f"{stop_req}: {'angefordert' if ok else 'abgelehnt'}")
        frist = time.monotonic() + max_wait
        rest = aktiv
        while time.monotonic() < frist:
            sleep(0.5)
            rest = active_outputs(s)
            if not rest:
                log("alle Ausgaben beendet - OBS kann ohne Rueckfrage schliessen")
                return 0
        log(f"WARNUNG: nach {max_wait:.0f} s noch aktiv: {[r for r, _ in rest]} - systemd beendet OBS trotzdem")
    except Exception as exc:  # noqa: BLE001 - nie fatal fuer den Stop
        log(f"OBS-Abfrage fehlgeschlagen ({type(exc).__name__}: {exc}) - systemd beendet OBS wie bisher")
    finally:
        with contextlib.suppress(Exception):
            ws.close()
    return 0


def main():
    mod = _ws_module()
    return stop_outputs(lambda: mod.WS(OBS_HOST, OBS_PORT, CONNECT_TIMEOUT), mod.obs_auth, ws_password())


if __name__ == "__main__":
    sys.exit(main())
