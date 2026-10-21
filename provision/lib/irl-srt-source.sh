#!/usr/bin/env bash
# IRL SRT Quelle (stabil) - eigene OBS-Quelle bauen, pruefen, absichern.
#
#   irl-srt-source.sh ensure [--quiet]   (root) pruefen, bei Bedarf neu bauen
#   irl-srt-source.sh build              (root) aus dem Quelltext bauen + installieren
#   irl-srt-source.sh check              (jeder) laesst sich das Plugin mit dem
#                                        installierten OBS/ffmpeg laden? Exit 0/1
#
# Warum auf dem Geraet gebaut wird: Das Plugin muss exakt zur installierten
# libobs- und ffmpeg-Version passen. Baut man es bei jeder Provisionierung und
# nach JEDEM Paket-Update (apt-Hook, siehe provision.sh) neu, falls die
# Ladepruefung fehlschlaegt, ueberlebt es OBS- und Ubuntu-Updates. Klappt das
# nicht, schaltet irl-srt-source-select.py die Szene vor dem OBS-Start auf die
# Standard-Medienquelle zurueck - es gibt nie eine Quelle ohne Bild.
set -u
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)/obs-plugins/irl-srt-source"
PLUGIN_DIR=/usr/local/lib/x86_64-linux-gnu/obs-plugins
DATA_DIR=/usr/local/share/obs/obs-plugins/irl-srt-source
SO="${PLUGIN_DIR}/irl-srt-source.so"
QUIET=0
[ "${2:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = 1 ] || echo "$*"; }

check() {
  [ -f "$SO" ] || return 1
  # Plugin mit sofortiger Symbolaufloesung laden: fehlt eine Bibliothek oder
  # passt die libobs-/ffmpeg-Schnittstelle nicht, schlaegt das hier fehl -
  # genau wie spaeter in OBS, nur ohne OBS zu starten.
  python3 - "$SO" <<'PY' 2>/dev/null
import ctypes, sys
lib = ctypes.CDLL(sys.argv[1], mode=ctypes.RTLD_GLOBAL | 2)  # 2 = RTLD_NOW
for sym in ("obs_module_load", "obs_module_set_pointer", "obs_module_ver"):
    getattr(lib, sym)
PY
}

build() {
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  if ! command -v cmake >/dev/null || [ ! -f /usr/local/include/obs/obs-module.h ]; then
    say "IRL_SRT_SOURCE_BUILD_FAILED Bauwerkzeuge oder OBS-Header fehlen"
    return 1
  fi
  if ! cmake -S "$SRC_DIR" -B "$tmp" -DCMAKE_BUILD_TYPE=Release >"$tmp/log" 2>&1 ||
     ! cmake --build "$tmp" >>"$tmp/log" 2>&1; then
    say "IRL_SRT_SOURCE_BUILD_FAILED"
    [ "$QUIET" = 1 ] || tail -20 "$tmp/log"
    return 1
  fi
  install -d -m 0755 "$PLUGIN_DIR" "$DATA_DIR/locale"
  install -m 0644 "$tmp/irl-srt-source.so" "$SO.new"
  mv -f "$SO.new" "$SO"
  install -m 0644 "$SRC_DIR/data/locale/"*.ini "$DATA_DIR/locale/"
  # Fingerabdruck der Bauumgebung merken (welche OBS/ffmpeg-Version)
  { dpkg-query -W -f='${Package} ${Version}\n' obs-studio libavformat62 libavcodec62 2>/dev/null
    sha256sum "$SRC_DIR/irl-srt-source.c" | cut -d' ' -f1; } > "$DATA_DIR/build-info.txt"
  return 0
}

stamp_ok() {
  # Quelltext unveraendert und gleiche OBS/ffmpeg-Pakete wie beim letzten Bau?
  [ -f "$DATA_DIR/build-info.txt" ] || return 1
  local now
  now="$({ dpkg-query -W -f='${Package} ${Version}\n' obs-studio libavformat62 libavcodec62 2>/dev/null
           sha256sum "$SRC_DIR/irl-srt-source.c" | cut -d' ' -f1; })"
  [ "$now" = "$(cat "$DATA_DIR/build-info.txt")" ]
}

case "${1:-}" in
  check)
    if check; then say "IRL_SRT_SOURCE_OK"; exit 0; fi
    say "IRL_SRT_SOURCE_NOT_LOADABLE"; exit 1 ;;
  build)
    build && check && { say "IRL_SRT_SOURCE_BUILT"; exit 0; }
    exit 1 ;;
  ensure)
    if check && stamp_ok; then say "IRL_SRT_SOURCE_OK"; exit 0; fi
    if build && check; then say "IRL_SRT_SOURCE_BUILT"; exit 0; fi
    say "IRL_SRT_SOURCE_FAILED - Szene nutzt die Standard-Medienquelle"
    exit 1 ;;
  *)
    echo "Aufruf: $0 ensure|build|check [--quiet]" >&2; exit 2 ;;
esac
