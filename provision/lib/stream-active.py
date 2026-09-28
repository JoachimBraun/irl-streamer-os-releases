#!/usr/bin/env python3
"""
IRL Streamer OS - "Laeuft gerade ein Stream?" (V1.85, ersetzt die
RX-Queue-Heuristik, die fast immer 0 lieferte -> Update killte Live-Streams)

Quellen (Stream gilt als AKTIV, sobald EINE davon "aktiv" meldet):
  1. OBS-Websocket (127.0.0.1:4455): GetStreamStatus / GetRecordStatus
     -> outputActive. Minimaler WebSocket-Client nur mit der Python-
     Standardbibliothek (auf dem Host ist kein obsws-python installiert).
  2. SRT-Live-Server-Statistik des belabox-receiver (http://127.0.0.1:8181/
     stats, identisch zum Diagnose-Dashboard): "publishers" nicht leer ->
     ein Encoder (Belabox) sendet gerade.

Bewusst NICHT genutzt: UDP-Bytezaehler pro Port - /proc/net/snmp kennt nur
Summen, per-Port-Zaehler braeuchten eigene iptables-Regeln. SLS-Publisher
deckt denselben Fall (Belabox sendet) zuverlaessiger ab.

Ergebnis (stdout JSON) + Exit-Code:
  0 = kein Stream aktiv (alle Quellen eindeutig "inaktiv"/nicht laufend)
  1 = Stream aktiv
  2 = unbekannt (eine Quelle lieferte einen unerwarteten Fehler, z.B.
      falsches OBS-Passwort, Timeout, kaputtes JSON) - der Aufrufer
      entscheidet (automatische Updates: wie AKTIV behandeln).
"Verbindung abgelehnt" (OBS/Container laeuft nicht) gilt als eindeutig
inaktiv - ohne laufendes OBS/SLS kann nichts gestreamt werden.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import urllib.error
import urllib.request

OBS_HOST = os.environ.get("IRL_OBS_HOST", "127.0.0.1")
OBS_PORT = int(os.environ.get("IRL_OBS_PORT", "4455"))
SLS_STATS_URL = os.environ.get("IRL_SLS_STATS_URL", "http://127.0.0.1:8181/stats")
PROJECT_DIR = os.environ.get("IRL_PROJECT_DIR", "/opt/irl-streamer-os")
TIMEOUT = float(os.environ.get("IRL_STREAM_CHECK_TIMEOUT", "4"))

ACTIVE, INACTIVE, UNKNOWN = "active", "inactive", "unknown"


def obs_password():
    """OBS-WS-Passwort: zuerst root-only State-Datei, dann Dashboard-.env."""
    p = os.path.join(PROJECT_DIR, "state", "obs-websocket-password.txt")
    try:
        with open(p) as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    try:
        with open(os.path.join(PROJECT_DIR, "docker", "irl-diagnostics.env")) as f:
            for line in f:
                if line.startswith("OBS_PASSWORD="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


# --- minimaler WebSocket-Client (RFC 6455, nur Text-Frames) ---------------
class WS:
    def __init__(self, host, port, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Handshake abgebrochen")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("Handshake zu gross")
        head, self.rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("kein WebSocket-Upgrade")

    def _recv_exact(self, n):
        out = b""
        if self.rest:
            out, self.rest = self.rest[:n], self.rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("Verbindung geschlossen")
            out += chunk
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack("!H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack("!Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        while True:
            b1, b2 = self._recv_exact(2)
            op = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            if n > 4 * 1024 * 1024:
                raise ConnectionError("Frame zu gross")
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x8:
                raise ConnectionError("OBS hat die Verbindung geschlossen (Auth?)")
            if op == 0x1:
                return json.loads(payload)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def obs_auth(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def check_obs():
    try:
        ws = WS(OBS_HOST, OBS_PORT, TIMEOUT)
    except ConnectionRefusedError:
        return INACTIVE, "OBS-Websocket nicht erreichbar (OBS laeuft nicht)"
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, f"OBS-Verbindung: {exc}"
    try:
        hello = ws.recv()
        ident = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            ident["d"]["authentication"] = obs_auth(obs_password(), auth["salt"], auth["challenge"])
        ws.send(ident)
        if ws.recv().get("op") != 2:
            return UNKNOWN, "OBS-Identify fehlgeschlagen"
        states = {}
        for i, req in enumerate(("GetStreamStatus", "GetRecordStatus")):
            ws.send({"op": 6, "d": {"requestType": req, "requestId": str(i)}})
            while True:
                msg = ws.recv()
                if msg.get("op") == 7 and msg["d"].get("requestId") == str(i):
                    break
            status = msg["d"].get("requestStatus") or {}
            if not status.get("result"):
                return UNKNOWN, f"OBS {req} fehlgeschlagen"
            states[req] = bool((msg["d"].get("responseData") or {}).get("outputActive"))
        if any(states.values()):
            return ACTIVE, f"OBS: {states}"
        return INACTIVE, "OBS streamt/nimmt nicht auf"
    except Exception as exc:  # noqa: BLE001 - jeder Fehler = unbekannt
        return UNKNOWN, f"OBS-Abfrage: {type(exc).__name__}: {exc}"
    finally:
        ws.close()


def check_sls():
    try:
        with urllib.request.urlopen(SLS_STATS_URL, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ConnectionRefusedError):
            return INACTIVE, "SLS-Statistik nicht erreichbar (Container laeuft nicht)"
        return UNKNOWN, f"SLS-Statistik: {exc}"
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, f"SLS-Statistik: {type(exc).__name__}: {exc}"
    return parse_sls(data)


def parse_sls(data):
    """Format wie im Diagnose-Dashboard: {"status":"ok","publishers":{...}}
    (publishers: dict Pfad->Stats, leer/fehlend = kein Encoder)."""
    if not isinstance(data, dict):
        return UNKNOWN, "SLS-Statistik: unerwartetes Format"
    pubs = data.get("publishers")
    if pubs:
        return ACTIVE, f"SLS: {len(pubs)} Publisher aktiv"
    if data.get("status") == "ok" or pubs is not None:
        return INACTIVE, "SLS: kein Publisher"
    return UNKNOWN, "SLS-Statistik: unerwartetes Format"


def combine(results):
    states = [s for s, _ in results]
    if ACTIVE in states:
        return ACTIVE
    if UNKNOWN in states:
        return UNKNOWN
    return INACTIVE


def main():
    results = [check_obs(), check_sls()]
    state = combine(results)
    print(json.dumps({"state": state, "details": [d for _, d in results]}, ensure_ascii=False))
    return {INACTIVE: 0, ACTIVE: 1}.get(state, 2)


if __name__ == "__main__":
    sys.exit(main())
