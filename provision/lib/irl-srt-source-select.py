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

Aufruf: irl-srt-source-select.py <szenen.json> [<weitere.json> ...] [--check-cmd <irl-srt-source.sh>]
Ausgabe je Datei: SOURCE_PLUGIN / SOURCE_FALLBACK / SOURCE_UNCHANGED (+ Grund)
Dateien werden nur bei einer Aenderung geschrieben (atomar, mit .bak). Nie fatal.
"""
import json
import os
import subprocess
import sys

NAME = "Belabox Stream"
PLUGIN_ID = "irl_srt_source"
FALLBACK_ID = "ffmpeg_source"
DEFAULT_URL = "srt://localhost:8282?streamid=play/stream/belabox"
DEFAULT_BUFFER_MS = 1000


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


def process(path: str, want_plugin: bool) -> str:
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
    if want_plugin:
        if not media_srcs:
            return "SOURCE_PLUGIN (unveraendert)"
        for s in media_srcs:
            to_plugin(s)
        result = "SOURCE_PLUGIN"
    else:
        if not plugin_srcs:
            return "SOURCE_FALLBACK (unveraendert)"
        for s in plugin_srcs:
            to_fallback(s)
        result = "SOURCE_FALLBACK Plugin nicht ladbar"
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
    paths = [p for p in args if not p.endswith((".bak", ".tmp"))]
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    want_plugin = plugin_loadable(check_cmd)
    for p in paths:
        print(process(p, want_plugin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
