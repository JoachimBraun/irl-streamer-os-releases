"""Geraeteeigene, zufaellige SRT-StreamID (Review 29.10.2026, NEU1).

Problem bis V1.112: Der Empfaenger (SLS im belabox-receiver) nahm jeden Sender
unter der festen StreamID live/stream/belabox an, und der SRTLA-Port am Relay
ist oeffentlich. War die Belabox aus, konnte ein Fremder unter genau dieser ID
senden - NOALBS schaltete auf LIVE und OBS zeigte das fremde Bild.

Loesung: jedes Geraet bekommt einen zufaelligen Streamnamen (live/stream/<name>).
SLS selbst kennt keine Zugangspruefung, nimmt also weiter jeden Namen an - aber
OBS, NOALBS und das Dashboard beachten nur noch den eigenen Namen, ein fremder
Sender unter einem anderen Namen bleibt wirkungslos.

Umstellung ohne Ausfall (reine Logik hier, Ausfuehrung in main.py):
  1. Belabox zeigt auf DIESES Geraet und sendet gerade nicht -> neue ID in die
     belaUI-Konfiguration schreiben, Ergebnis zuruecklesen.
  2. Erst wenn die Belabox die neue ID nachweislich hat, Empfaenger (NOALBS +
     OBS-Quelle) umstellen.
Nie umgekehrt - so passt Sender zu Empfaenger in jedem Zwischenstand, oder es
wurde noch gar nichts geaendert.
"""
import re
import secrets
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

LEGACY_NAME = "belabox"
PUBLISHER_PREFIX = "live/stream/"
PLAYER_PREFIX = "play/stream/"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RECEIVER_HOSTS = {"localhost", "127.0.0.1"}
RECEIVER_PORT = 8282


def generate_name() -> str:
    """24 Zeichen [0-9a-f] = 96 Bit Zufall - nicht erratbar, aber noch abtippbar."""
    return secrets.token_hex(12)


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name))


def publisher_id(name: str) -> str:
    return PUBLISHER_PREFIX + name


def player_url(name: str) -> str:
    return f"srt://localhost:{RECEIVER_PORT}?streamid={PLAYER_PREFIX}{name}"


def name_from_publisher(sid) -> Optional[str]:
    """'live/stream/abc' -> 'abc'; alles andere -> None."""
    if not isinstance(sid, str) or not sid.startswith(PUBLISHER_PREFIX):
        return None
    name = sid[len(PUBLISHER_PREFIX):]
    return name if valid_name(name) else None


def receiver_name_from_noalbs(cfg) -> Optional[str]:
    """Streamname, auf den NOALBS (= der Empfaenger) gerade hoert."""
    try:
        servers = cfg["switcher"]["streamServers"]
    except (KeyError, TypeError):
        return None
    for s in servers if isinstance(servers, list) else []:
        srv = (s or {}).get("streamServer") or {}
        if srv.get("type") == "SrtLiveServer":
            return name_from_publisher(srv.get("publisher"))
    return None


def set_noalbs_name(cfg: dict, name: str) -> bool:
    """Setzt den Publisher aller SLS-Eintraege auf `name`. True = geaendert."""
    changed = False
    for s in cfg.get("switcher", {}).get("streamServers", []) or []:
        srv = (s or {}).get("streamServer") or {}
        if srv.get("type") == "SrtLiveServer" and srv.get("publisher") != publisher_id(name):
            srv["publisher"] = publisher_id(name)
            changed = True
    return changed


def rewrite_receiver_url(url, name: str) -> Optional[str]:
    """Neue OBS-Quelladresse, falls `url` auf den eigenen Empfaenger zeigt
    (srt://localhost:8282?streamid=play/stream/<x>); sonst None (fremde Quelle,
    nicht anfassen). Unveraendert -> None."""
    if not isinstance(url, str) or not url.lower().startswith("srt://"):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in RECEIVER_HOSTS or port != RECEIVER_PORT:
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    sid = dict(query).get("streamid", "")
    if not sid.startswith(PLAYER_PREFIX):
        return None
    if sid == PLAYER_PREFIX + name:
        return None
    rest = "&".join(f"{k}={v}" for k, v in query if k != "streamid")
    new = player_url(name)
    return new + ("&" + rest if rest else "")


def own_publishers(stats, name: Optional[str]):
    """SLS-Statistik nur mit dem eigenen Publisher (fremde Sender ausgeblendet).
    name None (Empfaenger unbekannt) -> unveraendert."""
    if not name or not isinstance(stats, dict):
        return stats
    pubs = stats.get("publishers")
    if not isinstance(pubs, dict):
        return stats
    own = {p: v for p, v in pubs.items() if str(p).rstrip("/").split("/")[-1] == name}
    return {**stats, "publishers": own}


def box_points_here(box_cfg, own_hosts) -> bool:
    """Zeigt die Belabox (belaUI-config.json) auf DIESES Geraet? Nur dann darf
    das Dashboard ihre StreamID anfassen (Nutzer mit 2 Geraeten / Fremd-Relays)."""
    if not isinstance(box_cfg, dict) or box_cfg.get("relay_server") or box_cfg.get("relay_account"):
        return False
    addr = str(box_cfg.get("srtla_addr") or "").strip().lower().rstrip(".")
    return bool(addr) and addr in {h.lower().rstrip(".") for h in own_hosts if h}


def plan(box_sid, receiver_name: Optional[str], target: str, box_streaming: bool, box_ours: bool) -> str:
    """Naechster Schritt der Umstellung:
    'done'            - Sender und Empfaenger nutzen die Ziel-ID
    'switch_receiver' - Belabox hat die Ziel-ID schon, Empfaenger noch nicht
    'set_box'         - Belabox auf Ziel-ID setzen (danach switch_receiver)
    'wait_streaming'  - Belabox sendet gerade, spaeter erneut
    'not_ours'        - Belabox zeigt nicht hierher / eigene Fremd-ID -> nicht anfassen
    """
    target_sid = publisher_id(target)
    if box_sid == target_sid:
        return "done" if receiver_name == target else "switch_receiver"
    if not box_ours:
        return "not_ours"
    # Nur die bekannten Altwerte umstellen: die feste Alt-ID oder die ID, auf die
    # der Empfaenger gerade hoert. Eine bewusst anders gesetzte ID bleibt.
    known = {publisher_id(LEGACY_NAME)}
    if receiver_name:
        known.add(publisher_id(receiver_name))
    if box_sid not in known:
        return "not_ours"
    if box_streaming:
        return "wait_streaming"
    return "set_box"
