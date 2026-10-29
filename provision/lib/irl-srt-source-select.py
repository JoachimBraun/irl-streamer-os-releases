#!/usr/bin/env python3
"""Sorgt vor jedem OBS-Start dafuer, dass SRT-Empfangsquellen IMMER die eigene
'IRL SRT Quelle (stabil)' (irl_srt_source) nutzen - nie die Standard-Medienquelle.

  - Plugin ladbar: jede Standard-Medienquelle (ffmpeg_source) mit SRT-Adresse
    (srt://...) - auch eine vom Kunden spaeter selbst angelegte - wird auf das
    Plugin umgestellt (Name, Szenen-Eintraege, Filter bleiben erhalten).
  - Plugin NICHT ladbar (z.B. OBS-/Ubuntu-Update hat es unbrauchbar gemacht und
    der automatische Neubau scheiterte): alle Plugin-Quellen werden voruebergehend
    zur Standard-Medienquelle, damit das Bild erhalten bleibt (OBS zeigt eine
    Quelle unbekannten Typs sonst nur als leeren Platzhalter). Sobald das Plugin
    wieder geht, stellt der naechste Start automatisch zurueck.

Seit V1.113 (Review 29.10.2026) zusaetzlich: Quellen, die den eigenen Empfaenger
abspielen (srt://localhost:8282?streamid=play/stream/<x>), auf den geraeteeigenen
Streamnamen aus der NOALBS-Konfiguration umstellen (das Dashboard stellt die ID
um; laeuft OBS dabei nicht, zieht dieser Start die Quelle nach).

Aufruf: irl-srt-source-select.py <szenen.json> [<weitere.json> ...] [--check-cmd <irl-srt-source.sh>]
        [--noalbs <belabox/config.json>]
Ausgabe je Datei: SOURCE_PLUGIN / SOURCE_FALLBACK / SOURCE_UNCHANGED (+ Grund)
Dateien werden nur bei einer Aenderung geschrieben (atomar, mit .bak). Nie fatal.
"""
import json
import os
import re
import subprocess
import sys
from urllib.parse import parse_qsl, urlsplit

NAME = "Belabox Stream"
PLUGIN_ID = "irl_srt_source"
FALLBACK_ID = "ffmpeg_source"
DEFAULT_URL = "srt://localhost:8282?streamid=play/stream/belabox"
DEFAULT_BUFFER_MS = 1000
NOALBS_CONFIG = "/opt/irl-streamer-os/docker/belabox/config.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def receiver_name(noalbs_path: str):
    """Streamname, auf den NOALBS hoert (live/stream/<name>) - None wenn unbekannt."""
    try:
        with open(noalbs_path, encoding="utf-8") as f:
            cfg = json.load(f)
        for s in cfg["switcher"]["streamServers"]:
            srv = s.get("streamServer") or {}
            pub = str(srv.get("publisher") or "")
            if srv.get("type") == "SrtLiveServer" and pub.startswith("live/stream/"):
                name = pub[len("live/stream/"):]
                return name if _NAME_RE.fullmatch(name) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def rewrite_receiver_url(url, name):
    """Gleiche Regel wie stream_key.rewrite_receiver_url im Dashboard: nur der eigene
    Empfaenger (localhost/127.0.0.1:8282, play/stream/...), sonst None."""
    if not name or not isinstance(url, str) or not url.lower().startswith("srt://"):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in ("localhost", "127.0.0.1") or port != 8282:
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    sid = dict(query).get("streamid", "")
    if not sid.startswith("play/stream/") or sid == "play/stream/" + name:
        return None
    rest = "&".join(f"{k}={v}" for k, v in query if k != "streamid")
    new = f"srt://localhost:8282?streamid=play/stream/{name}"
    return new + ("&" + rest if rest else "")


def sync_stream_name(sources, name) -> int:
    n = 0
    for src in sources:
        st = src.get("settings")
        if isinstance(st, dict):
            new = rewrite_receiver_url(st.get("input"), name)
            if new:
                st["input"] = new
                n += 1
    return n


def plugin_loadable(check_cmd: str) -> bool:
    try:
        return subprocess.run(["bash", check_cmd, "check", "--quiet"], timeout=30).returncode == 0
    except Exception:
        return False


def is_srt_media_source(src: dict) -> bool:
    """Standard-Medienquelle, die einen SRT-Strom empfaengt (unser Anwendungsfall)."""
    if src.get("id") != FALLBACK_ID:
        return False
    st = src.get("settings") or {}
    url = str(st.get("input") or "").strip().lower()
    if url.startswith("srt://") and not st.get("is_local_file"):
        return True
    # Vorlagen-Quelle ohne gespeicherte Adresse
    return src.get("name") == NAME and not st.get("is_local_file")


def to_plugin(src: dict) -> None:
    old = src.get("settings") or {}
    url = old.get("input") or DEFAULT_URL
    buf = old.get("buffer_ms") or DEFAULT_BUFFER_MS
    src["id"] = src["versioned_id"] = PLUGIN_ID
    src["settings"] = {"input": url, "buffer_ms": buf, "reconnect_ms": old.get("reconnect_ms", 1000)}


def to_fallback(src: dict) -> None:
    old = src.get("settings") or {}
    url = old.get("input") or DEFAULT_URL
    src["id"] = src["versioned_id"] = FALLBACK_ID
    # Werte wie in der bisherigen Vorlage (V1.106) - bewaehrter Stand
    src["settings"] = {"close_when_inactive": True, "input": url, "is_local_file": False,
                       "reconnect_delay_sec": 4, "buffer_ms": old.get("buffer_ms", DEFAULT_BUFFER_MS)}


def process(path: str, want_plugin: bool, name=None) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return f"SOURCE_UNCHANGED Szenen-Datei nicht lesbar: {exc}"
    sources = data.get("sources", []) if isinstance(data, dict) else []
    plugin_srcs = [s for s in sources if s.get("id") == PLUGIN_ID]
    media_srcs = [s for s in sources if is_srt_media_source(s)]
    if not plugin_srcs and not media_srcs:
        return "SOURCE_UNCHANGED keine passende Quelle"
    changed = False
    if want_plugin:
        if not media_srcs:
            result = "SOURCE_PLUGIN (unveraendert)"
        else:
            for s in media_srcs:
                to_plugin(s)
            result, changed = "SOURCE_PLUGIN", True
    else:
        if not plugin_srcs:
            result = "SOURCE_FALLBACK (unveraendert)"
        else:
            for s in plugin_srcs:
                to_fallback(s)
            result, changed = "SOURCE_FALLBACK Plugin nicht ladbar", True
    if sync_stream_name(sources, name):
        result, changed = result + " +STREAMID", True
    if not changed:
        return result
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        os.replace(path, path + ".bak")
    except OSError:
        pass
    os.replace(tmp, path)
    return result


def main() -> int:
    args = sys.argv[1:]
    check_cmd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "irl-srt-source.sh")
    if "--check-cmd" in args:
        i = args.index("--check-cmd")
        check_cmd = args[i + 1]
        del args[i:i + 2]
    noalbs = NOALBS_CONFIG
    if "--noalbs" in args:
        i = args.index("--noalbs")
        noalbs = args[i + 1]
        del args[i:i + 2]
    paths = [p for p in args if not p.endswith((".bak", ".tmp"))]
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    want_plugin = plugin_loadable(check_cmd)
    name = receiver_name(noalbs)
    for p in paths:
        print(process(p, want_plugin, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
