#!/usr/bin/env python3
"""
IRL Streamer OS - Relay-Tunnel Gesundheitspruefung (V1.87, Block B)

Ersetzt das bisherige "jede Minute Token holen + /verify". Pro Timer-Lauf
(jede Minute) wird LOKAL geprueft; der Server wird nur noch gebraucht, wenn
es wirklich etwas zu melden/holen gibt:

  Lokal (jede Minute, kein Server-Request):
    - Tunnel-Interface aktiv?  (Aufrufer: systemctl is-active)
    - Relay-Gateway 10.8.0.1 per Ping erreichbar? (3 Pakete, <= 4 s)
      Ausfall  -> Ampel sofort ROT (verified=false), spaetestens beim naechsten
                  Timer-Lauf (<= 60 s + ~5 s) -> Vorgabe User: <= 2 Minuten.
  Server (/verify, 1 Request):
    - stuendlicher Heartbeat (+ fester Geraete-Jitter 0..600 s)
    - sofort, sobald lokal wieder alles ok ist, aber noch ROT gemeldet war
    - bei Fehlern mit Backoff + Jitter (siehe backoff_seconds)
  Voller Ablauf (/relay/token + /provision, Aufrufer = Client-Skript):
    - nur wenn Tunnel/Config/State fehlen

Subcommands (Ausgabe = genau ein Wort auf stdout):
  decide  --state-dir D [--ping ok|fail|auto] [--tunnel-active 1|0]
          -> ok | red | verify | wait | full
  record  --state-dir D --result verified|unverified|error [--slug S] [--fingerprint F]
          -> schreibt relay-provision.json + relay-health.json
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time

RELAY_GATEWAY_IP = "10.8.0.1"
HEARTBEAT_SECONDS = 3600
HEARTBEAT_JITTER_MAX = 600
BACKOFF_FREE_TRIES = 2       # die ersten Fehlversuche: naechster Minutenlauf
BACKOFF_CAP_SECONDS = 900
BACKOFF_JITTER_MAX = 30

STATE_NAME = "relay-provision.json"
HEALTH_NAME = "relay-health.json"


def device_jitter(fingerprint: str) -> int:
    """Stabiler Versatz 0..HEARTBEAT_JITTER_MAX pro Geraet (verteilt die
    stuendlichen Heartbeats ueber die Flotte, ohne Zufall pro Lauf)."""
    h = hashlib.sha256((fingerprint or "").encode()).digest()
    return int.from_bytes(h[:4], "big") % (HEARTBEAT_JITTER_MAX + 1)


def backoff_seconds(fail_count: int, jitter: int = 0) -> int:
    """0 fuer die ersten Fehlversuche, dann 120, 240, 480, 900 (Deckel) + Jitter."""
    if fail_count <= BACKOFF_FREE_TRIES:
        return 0
    base = min(60 * 2 ** (fail_count - BACKOFF_FREE_TRIES), BACKOFF_CAP_SECONDS)
    return base + jitter


def decide(now: float, *, have_state: bool, have_config: bool, tunnel_active: bool,
           ping_ok: bool, verified: bool, fingerprint: str, last_verify_at: float,
           fail_count: int, next_try_at: float) -> str:
    """Reine Entscheidungslogik (ohne I/O) - siehe Modul-Doku."""
    if not (have_state and have_config and tunnel_active and fingerprint):
        # Voller Ablauf (Token+Provision) - bei Serverfehlern ebenfalls mit Backoff
        return "wait" if now < next_try_at else "full"
    if not ping_ok:
        return "red"
    # Tunnel lokal ok
    if now < next_try_at:
        return "wait" if not verified else "ok"
    if not verified:
        return "verify"          # Erholung: sofort melden
    if now - last_verify_at >= HEARTBEAT_SECONDS + device_jitter(fingerprint):
        return "verify"          # stuendlicher Heartbeat
    return "ok"


def ping_gateway(ip: str = RELAY_GATEWAY_IP) -> bool:
    try:
        r = subprocess.run(["ping", "-c", "3", "-i", "0.5", "-W", "2", "-q", ip],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: str, data: dict, mode: int = 0o600) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".relay-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(state_dir: str):
    sp = os.path.join(state_dir, STATE_NAME)
    hp = os.path.join(state_dir, HEALTH_NAME)
    return sp, hp, _read_json(sp), _read_json(hp)


def cmd_decide(a) -> str:
    now = time.time()
    sp, hp, state, health = load(a.state_dir)
    have_state = bool(state)
    have_config = os.path.exists(a.config_file)
    tunnel_active = a.tunnel_active == "1"
    # Erstlauf nach Update: noch kein Health-File -> Zeitpunkt des letzten
    # Schreibens der State-Datei als "letzter Verify" annehmen (kein Request-Sturm
    # direkt nach dem Update).
    if "last_verify_at" in health:
        last_verify = float(health["last_verify_at"])
    else:
        try:
            last_verify = os.stat(sp).st_mtime
        except OSError:
            last_verify = 0.0
    fingerprint = str(state.get("device_fingerprint") or "")
    precheck = decide(now, have_state=have_state, have_config=have_config,
                      tunnel_active=tunnel_active, ping_ok=True,
                      verified=bool(state.get("verified")), fingerprint=fingerprint,
                      last_verify_at=last_verify, fail_count=int(health.get("fail_count", 0)),
                      next_try_at=float(health.get("next_try_at", 0)))
    if precheck in ("full", "wait") and not (have_state and have_config and tunnel_active and fingerprint):
        # V1.89: Tunnel ist weg (oder Konfig fehlt) -> Ampel SOFORT rot, auch wenn der
        # Neuaufbau wegen eines laufenden Streams verschoben wird. Rot aendert nur
        # den Anzeigezustand, ruehrt den Stream nicht an; Gruen kommt erst nach
        # erfolgreicher Neuprovisionierung + Verify wieder (cmd_record).
        if have_state and not (tunnel_active and have_config) and state.get("verified"):
            state["verified"] = False
            _write_json_atomic(sp, state)
            health["red_since"] = health.get("red_since") or now
            _write_json_atomic(hp, health)
        return precheck
    if a.ping == "auto":
        ping_ok = ping_gateway(a.gateway)
    else:
        ping_ok = a.ping == "ok"
    action = decide(now, have_state=have_state, have_config=have_config,
                    tunnel_active=tunnel_active, ping_ok=ping_ok,
                    verified=bool(state.get("verified")), fingerprint=fingerprint,
                    last_verify_at=last_verify, fail_count=int(health.get("fail_count", 0)),
                    next_try_at=float(health.get("next_try_at", 0)))
    if action == "red":
        if state.get("verified"):
            state["verified"] = False
            _write_json_atomic(sp, state)
        else:
            os.utime(sp, None)   # "Zuletzt geprueft" im Dashboard aktuell halten
        health["red_since"] = health.get("red_since") or now
        _write_json_atomic(hp, health)
    elif action in ("ok", "wait"):
        os.utime(sp, None)
    return action


def cmd_record(a) -> str:
    now = time.time()
    sp, hp, state, health = load(a.state_dir)
    if a.result in ("verified", "unverified"):
        if a.slug:
            state["subdomain_slug"] = a.slug
        if a.fingerprint:
            state["device_fingerprint"] = a.fingerprint
        state["verified"] = a.result == "verified"
        _write_json_atomic(sp, state)
    health["last_server_call_at"] = now
    health["server_calls"] = int(health.get("server_calls", 0)) + 1
    if a.result == "verified":
        health.update(last_verify_at=now, fail_count=0, next_try_at=0)
        health.pop("red_since", None)
    else:
        n = int(health.get("fail_count", 0)) + 1
        health["fail_count"] = n
        health["next_try_at"] = now + backoff_seconds(n, random.randint(0, BACKOFF_JITTER_MAX))
        if a.result == "error":
            os.utime(sp, None) if os.path.exists(sp) else None
    _write_json_atomic(hp, health)
    return "recorded"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--state-dir", required=True)
    d.add_argument("--config-file", default="/etc/wireguard/wg-relay.conf")
    d.add_argument("--tunnel-active", choices=["1", "0"], default="1")
    d.add_argument("--ping", choices=["ok", "fail", "auto"], default="auto")
    d.add_argument("--gateway", default=RELAY_GATEWAY_IP)
    r = sub.add_parser("record")
    r.add_argument("--state-dir", required=True)
    r.add_argument("--result", choices=["verified", "unverified", "error"], required=True)
    r.add_argument("--slug", default="")
    r.add_argument("--fingerprint", default="")
    a = ap.parse_args(argv)
    print(cmd_decide(a) if a.cmd == "decide" else cmd_record(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
