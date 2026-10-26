"""OBS-Statistik-Overlay (Browserquelle): reine Logik ohne FastAPI.

Ein schmaler Streifen am Bildrand zeigt Live-Werte der Sendekette (LIVE + Dauer, Gesamtbitrate mit Verlauf,
Anteil je Internetweg, Laufzeit zum Server, Encoder-Bilder/s, Box-Temperatur, aktive PiP-Szene).

Grundsaetze:
- Zugriff ohne Dashboard-Login, aber nur mit geheimem Schluessel (Konstantzeit-Vergleich).
- Ausgabe nur ueber eine feste Liste von Feldern (Whitelist). Nie IP-Adressen, Stream-ID/-Pfade, Server-Adressen,
  Passwoerter oder WLAN-Namen - auch nicht in Namen, die der Nutzer selbst vergeben hat (siehe safe_label).
- Alle Quellen sind bereits vorhanden und werden nur gelesen (letzte Ereignisse der laufenden Diagnose-Sitzung).
  Die Nutzdaten werden hoechstens 1x pro Sekunde berechnet und mit allen Betrachtern geteilt.
- Die Encoder-Kennzahlen-Datei der Belabox wird hoechstens alle 2 s per vorhandener SSH-Verbindung gelesen,
  im Hintergrund (eine Anfrage wartet nie auf SSH), und nur solange jemand das Overlay ansieht.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

SETTINGS_FILENAME = "overlay_settings.json"
MAX_VIEWERS = 3                 # gleichzeitige Overlay-Fenster (wie IRL4YOU)
VIEWER_TIMEOUT_S = 10.0         # ein Fenster gilt als geschlossen, wenn es so lange nichts mehr abgefragt hat
GLOBAL_RATE_PER_S = 15.0        # alle Abfragen zusammen (Schutz gegen Dauerfeuer mit gueltigem Schluessel)
PAYLOAD_CACHE_S = 1.0
HISTORY_LEN = 60                # Sekunden Verlauf fuer die Mini-Kurve
SOURCE_STALE_S = 15.0           # aelter = Quelle gilt als weg
BELACODER_FILE = "/run/irl-net/belacoder.json"
BELACODER_MIN_INTERVAL_S = 2.0
BELACODER_STALE_S = 6.0
BELACODER_ERROR_BACKOFF_S = 10.0

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_VIEWER_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6_RE = re.compile(r"\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}\b")


# ---------------------------------------------------------------- Einstellungen (eigene Datei, NICHT device_config)
def new_key() -> str:
    return secrets.token_urlsafe(24)


def load_settings(data_dir: Path) -> dict:
    p = Path(data_dir) / SETTINGS_FILENAME
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        raw = {}
    key = raw.get("key") if isinstance(raw.get("key"), str) and _KEY_RE.match(raw.get("key") or "") else ""
    return {"enabled": bool(raw.get("enabled")) and bool(key), "key": key}


def save_settings(data_dir: Path, settings: dict, writer: Optional[Callable[[Path, str], None]] = None) -> dict:
    s = {"enabled": bool(settings.get("enabled")), "key": settings.get("key") or ""}
    if s["enabled"] and not s["key"]:
        s["key"] = new_key()
    p = Path(data_dir) / SETTINGS_FILENAME
    text = json.dumps(s, indent=2)
    if writer is not None:
        writer(p, text)
    else:
        p.write_text(text)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return s


def key_ok(settings: dict, given: Optional[str]) -> bool:
    """Konstantzeit-Vergleich; leerer/abgeschalteter Schluessel passt nie."""
    want = settings.get("key") or ""
    if not want or not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(given.encode(), want.encode())


# ---------------------------------------------------------------- Betrachter-Begrenzung + Rate-Limit
class ViewerGate:
    """Hoechstens MAX_VIEWERS gleichzeitige Fenster (je Fenster eine zufaellige Kennung `v`) und insgesamt
    hoechstens GLOBAL_RATE_PER_S Abfragen pro Sekunde (Token-Eimer). Hinter Caddy kommen alle Anfragen von
    127.0.0.1, deshalb zaehlt die Fenster-Kennung statt der IP."""

    def __init__(self, max_viewers: int = MAX_VIEWERS, rate: float = GLOBAL_RATE_PER_S,
                 clock: Callable[[], float] = time.monotonic):
        self.max_viewers = max_viewers
        self.rate = rate
        self.clock = clock
        self.viewers: dict[str, float] = {}
        self.tokens = rate * 2
        self.last = clock()
        self.lock = threading.Lock()

    def admit(self, viewer: Optional[str], fallback: str = "anon") -> Optional[str]:
        """None = zugelassen, sonst Grund ('viewers' | 'rate')."""
        now = self.clock()
        vid = viewer if isinstance(viewer, str) and _VIEWER_RE.match(viewer) else "x-" + str(fallback)[:40]
        with self.lock:
            self.tokens = min(self.rate * 2, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens < 1:
                return "rate"
            for k, t in list(self.viewers.items()):
                if now - t > VIEWER_TIMEOUT_S:
                    del self.viewers[k]
            if vid not in self.viewers and len(self.viewers) >= self.max_viewers:
                return "viewers"
            self.tokens -= 1
            self.viewers[vid] = now
            return None

    def active(self) -> int:
        now = self.clock()
        with self.lock:
            return sum(1 for t in self.viewers.values() if now - t <= VIEWER_TIMEOUT_S)

    def reset(self):
        with self.lock:
            self.viewers.clear()


# ---------------------------------------------------------------- Hilfen
def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def _event_age(event: Optional[dict], now_utc: datetime) -> Optional[float]:
    if not isinstance(event, dict):
        return None
    try:
        ts = datetime.fromisoformat(str(event.get("ts")))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now_utc - ts).total_seconds()


def _fresh_data(last_by_source: dict, source: str, now_utc: datetime) -> Optional[dict]:
    ev = (last_by_source or {}).get(source)
    age = _event_age(ev, now_utc)
    if not isinstance(ev, dict) or age is None or age > SOURCE_STALE_S:
        return None
    data = ev.get("data")
    if not isinstance(data, dict) or data.get("ok") is False:
        return None
    return data


def _contains_address(text: str) -> bool:
    if _IPV4_RE.search(text):
        return True
    for tok in _IPV6_RE.findall(text):
        try:
            ipaddress.IPv6Address(tok)
            return True
        except ValueError:
            continue
    return False


def safe_label(label: str, fallback: str) -> str:
    """Kurzer Anzeigename ohne Adressen. Nutzer-Namen wie 'Router 192.168.8.1' fallen auf den Ersatznamen zurueck."""
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    if not text or _contains_address(text) or "://" in text or "@" in text:
        return fallback
    return text[:14]


def iface_kind(name: str) -> str:
    n = (name or "").lower()
    if n.startswith(("wlan", "wlp", "wl")):
        return "WLAN"
    if n.startswith(("wwan", "ppp", "wwp", "modem", "cdc")):
        return "Mobil"
    if n.startswith(("usb", "rndis", "enx")):
        return "USB"
    if n.startswith(("eth", "en", "end")):
        return "LAN"
    return "Netz"


def _prefix24(ip: str) -> Optional[str]:
    try:
        a = ipaddress.IPv4Address(str(ip).strip())
    except ValueError:
        return None
    return ".".join(str(a).split(".")[:3])


def link_label(iface_name: str, iface_ip: str, routers: list) -> str:
    """Router-Name aus der Konfiguration (gleiches /24-Netz wie die Router-Adresse, wie im Dashboard), sonst Typ."""
    pre = _prefix24(iface_ip)
    if pre:
        for i, r in enumerate(routers or []):
            host = (r.get("host") if isinstance(r, dict) else getattr(r, "host", "")) or ""
            label = (r.get("label") if isinstance(r, dict) else getattr(r, "label", "")) or ""
            if host and _prefix24(host) == pre:
                return safe_label(label, "Router %d" % (i + 1))
    return iface_kind(iface_name)


LINK_IDLE_WARN_S = 5.0          # erst nach so vielen Sekunden ganz ohne Verkehr wird ein Weg gelb
_link_last_active: dict = {}    # Name -> letzter Zeitpunkt mit Verkehr (monotonic)


def build_links(netif: dict, routers: list, last_active: Optional[dict] = None, now: Optional[float] = None) -> list:
    """belaUI-netif {name: {ip, tp (Bytes/s), enabled, error?}} -> Chips [{name, kbps, pct, state}] ohne IPs.

    Jeder Weg bleibt IMMER im Streifen (User-Wunsch 26.10.): in belaUI abgeschaltete Wege erscheinen ausgegraut
    (state "off", ohne Mbit-Wert - ihr Restverkehr ist z. B. der Verwaltungstunnel), im Dashboard konfigurierte Router
    ohne passende Netzkarte der Belabox (Kabel ab, Router aus) ebenfalls ausgegraut (state "gone"). Frueher fielen
    abgeschaltete Wege ohne Verkehr weg - schwankte der Restverkehr um 0, sprang der Chip im Sekundentakt.
    """
    if not isinstance(netif, dict):
        netif = {}
    last_active = _link_last_active if last_active is None else last_active
    now = time.monotonic() if now is None else now
    rows = []
    matched = set()
    for name in sorted(netif):
        it = netif.get(name)
        if not isinstance(it, dict):
            continue
        enabled = bool(it.get("enabled"))
        tp = _num(it.get("tp"))
        kbps = int(round(tp * 8 / 1000)) if tp is not None and tp >= 0 else 0
        pre = _prefix24(it.get("ip") or "")
        for i, r in enumerate(routers or []):
            host = (r.get("host") if isinstance(r, dict) else getattr(r, "host", "")) or ""
            if host and pre and _prefix24(host) == pre:
                matched.add(i)
        if not enabled:
            kbps = 0
        label = link_label(name, it.get("ip") or "", routers)
        # Gelb nur, wenn ein eingeschalteter Weg mehrere Sekunden GAR KEINEN Verkehr hat. Wenig Verkehr ist normal:
        # srtla verteilt nach Kapazitaet, ein starker Weg (Heimnetz) nimmt fast alles, die anderen tragen nur
        # Keepalives (~0,1 Mbit) - das ist kein Fehler (User-Feedback 26.10.).
        if kbps > 0 or not enabled or name not in last_active:
            last_active[name] = now
        idle_s = now - last_active[name]
        if it.get("error"):
            state = "bad"
        elif not enabled:
            state = "off"
        elif idle_s >= LINK_IDLE_WARN_S:
            state = "warn"
        else:
            state = "ok"
        rows.append({"name": label, "kbps": kbps, "state": state})
    for i, r in enumerate(routers or []):
        host = (r.get("host") if isinstance(r, dict) else getattr(r, "host", "")) or ""
        label = (r.get("label") if isinstance(r, dict) else getattr(r, "label", "")) or ""
        if host and i not in matched and _prefix24(host):
            rows.append({"name": safe_label(label, "Router %d" % (i + 1)), "kbps": 0, "state": "gone"})
    total = sum(r["kbps"] for r in rows if r["state"] not in ("off", "gone"))
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["name"]] = seen.get(r["name"], 0) + 1
    count: dict[str, int] = {}
    for r in rows:
        if seen[r["name"]] > 1:
            count[r["name"]] = count.get(r["name"], 0) + 1
            r["name"] = "%s %d" % (r["name"][:12], count[r["name"]])
        r["pct"] = int(round(100 * r["kbps"] / total)) if total > 0 and r["state"] not in ("off", "gone") else 0
    return rows[:6]


def pick_publisher(stats) -> Optional[dict]:
    pubs = stats.get("publishers") if isinstance(stats, dict) else None
    if not isinstance(pubs, dict) or not pubs:
        return None
    for path in sorted(pubs):
        if str(path).endswith("/belabox") and isinstance(pubs[path], dict):
            return pubs[path]
    first = pubs[sorted(pubs)[0]]
    return first if isinstance(first, dict) else None


def parse_belacoder(text: str) -> Optional[dict]:
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    out = {}
    for k in ("time", "fps", "bitrate_kbps", "rtt_ms", "send_mbps", "snd_buf_ms", "snd_buf_pkts",
              "retrans_total", "loss_total", "drop_total", "sent_total"):
        v = _num(raw.get(k))
        if v is not None:
            out[k] = v
    return out or None


def _lvl(value: Optional[float], warn: float, bad: float, higher_is_worse: bool = True) -> str:
    if value is None:
        return ""
    if higher_is_worse:
        return "bad" if value >= bad else "warn" if value >= warn else "ok"
    return "bad" if value <= bad else "warn" if value <= warn else "ok"


# ---------------------------------------------------------------- Belacoder-Datei (Hintergrund-Lesen, gecacht)
class BelacoderReader:
    """Liest /run/irl-net/belacoder.json hoechstens alle 2 s im Hintergrund-Thread. Fehlt die Datei oder ist die
    Box nicht erreichbar, liefert get() einfach None (Feld wird im Overlay weggelassen)."""

    def __init__(self, fetch: Callable[[], Optional[str]], clock: Callable[[], float] = time.monotonic):
        self.fetch = fetch
        self.clock = clock
        self.data: Optional[dict] = None
        self.fetched_at = 0.0
        self.next_try = 0.0
        self.last_time_val = None
        self.time_changed_at = 0.0
        self.busy = False
        self.lock = threading.Lock()

    def _run(self):
        try:
            text = self.fetch()
            parsed = parse_belacoder(text) if text else None
            ok = True
        except Exception:
            parsed, ok = None, False
        now = self.clock()
        with self.lock:
            self.busy = False
            self.next_try = now + (BELACODER_MIN_INTERVAL_S if ok else BELACODER_ERROR_BACKOFF_S)
            if parsed is None:
                self.data = None
                return
            if parsed.get("time") != self.last_time_val:
                self.last_time_val = parsed.get("time")
                self.time_changed_at = now
            self.data, self.fetched_at = parsed, now

    def poke(self, wanted: bool = True, background: bool = True):
        now = self.clock()
        with self.lock:
            if not wanted or self.busy or now < self.next_try:
                return
            self.busy = True
        if background:
            threading.Thread(target=self._run, name="overlay-belacoder", daemon=True).start()
        else:
            self._run()

    def get(self) -> Optional[dict]:
        now = self.clock()
        with self.lock:
            if self.data is None or now - self.fetched_at > BELACODER_STALE_S:
                return None
            if self.last_time_val is not None and now - self.time_changed_at > BELACODER_STALE_S:
                return None     # Datei wird nicht mehr neu geschrieben (belacoder steht)
            return dict(self.data)


# ---------------------------------------------------------------- Nutzdaten
class OverlayState:
    """Geteilter Zustand: zuletzt berechnete Nutzdaten (1 s), Bitraten-Verlauf, eigener Live-Beginn."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.cached: Optional[dict] = None
        self.cached_at = -1e9
        self.history: list = []          # [(sekunde, mbps|None)]
        self.live_since: Optional[float] = None
        self.lock = threading.Lock()

    def _sample(self, sec: int, mbps: Optional[float]):
        if self.history and self.history[-1][0] == sec:
            self.history[-1] = (sec, mbps)
        else:
            if self.history:   # Luecken (keine Abfrage) als fehlende Werte auffuellen
                gap = min(sec - self.history[-1][0] - 1, HISTORY_LEN)
                for i in range(max(0, gap)):
                    self.history.append((sec - gap + i, None))
            self.history.append((sec, mbps))
        del self.history[:-HISTORY_LEN]

    def payload(self, build: Callable[[], dict]) -> dict:
        now = self.clock()
        with self.lock:
            if self.cached is not None and now - self.cached_at < PAYLOAD_CACHE_S:
                return self.cached
            data = build()
            live = bool(data.get("live"))
            if live and self.live_since is None:
                self.live_since = now
            elif not live:
                self.live_since = None
            if live and data.get("live_s") is None and self.live_since is not None:
                data["live_s"] = int(now - self.live_since)
            self._sample(int(now), data.get("rate_mbps"))
            data["hist"] = [None if v is None else round(v, 2) for _, v in self.history]
            self.cached, self.cached_at = data, now
            return data


def build_payload(last_by_source: dict, belabox_live: dict, routers: list, belacoder: Optional[dict] = None,
                  pip_scene: Optional[str] = None, now_utc: Optional[datetime] = None) -> dict:
    """Setzt die Overlay-Daten nur aus erlaubten Feldern neu zusammen (nichts wird 1:1 durchgereicht)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    lbs = last_by_source or {}
    live_ev = _fresh_data(lbs, "belabox_live", now_utc)
    ssh_ev = _fresh_data(lbs, "belabox", now_utc)
    srt_ev = _fresh_data(lbs, "srtla", now_utc)
    pub = pick_publisher((srt_ev or {}).get("stats"))
    online = bool(live_ev or ssh_ev or pub)
    out: dict = {"ok": True, "online": online}
    if not online:
        return out
    bl = belabox_live if isinstance(belabox_live, dict) else {}
    is_streaming = bl.get("is_streaming") if live_ev else None
    live = bool(is_streaming) or (is_streaming is None and pub is not None)
    out["live"] = live

    rate = None
    if pub is not None:
        rate = _num(pub.get("mbpsRecvRate"))
        kbit = _num(pub.get("bitrate"))
        if rate is None and kbit is not None:
            rate = kbit / 1000
        up = _num(pub.get("uptime"))
        if live and up is not None:
            out["live_s"] = int(up)
        rtt = _num(pub.get("rtt"))
        if rtt is not None:
            out["rtt_ms"] = int(round(rtt))
    enc = belacoder or {}
    if rate is None and live and _num(enc.get("bitrate_kbps")) is not None:
        rate = enc["bitrate_kbps"] / 1000
    if "rtt_ms" not in out and _num(enc.get("rtt_ms")) is not None:
        out["rtt_ms"] = int(round(enc["rtt_ms"]))
    if rate is not None:
        out["rate_mbps"] = round(max(0.0, rate), 2)
        max_br = _num(bl.get("max_br"))
        if live and max_br and max_br > 0:
            ratio = rate * 1000 / max_br
            out["rate_lvl"] = "bad" if ratio < 0.25 else "warn" if ratio < 0.5 else "ok"
        elif live:
            out["rate_lvl"] = _lvl(rate, 2.5, 1.0, higher_is_worse=False)
    if "rtt_ms" in out:
        out["rtt_lvl"] = _lvl(out["rtt_ms"], 60, 150)
    fps = _num(enc.get("fps")) if live else None
    if fps is not None:
        out["fps"] = int(round(fps))
        out["fps_lvl"] = _lvl(fps, 27, 20, higher_is_worse=False)
    temp = _num((ssh_ev or {}).get("cpu_temp"))
    if temp is not None:
        out["temp_c"] = int(round(temp))
        out["temp_lvl"] = _lvl(temp, 70, 80)
    if live_ev:
        links = build_links(bl.get("interfaces") or {}, routers)
        if links:
            out["links"] = links
    if pip_scene:
        out["pip"] = safe_label(pip_scene, "PiP")
    return out
