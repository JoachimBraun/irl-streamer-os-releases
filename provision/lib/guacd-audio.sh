#!/usr/bin/env bash
# guacd mit Ton (docker/guacd-audio) bauen und als irl-guacd-audio:local bereitstellen.
#
#   guacd-audio.sh ensure   (root) bei Bedarf bauen; bei Fehlschlag offizielles guacd
#                           unter demselben Namen bereitstellen (Fernzugriff ohne Ton)
#   guacd-audio.sh status   welche Variante laeuft (AUDIO / FALLBACK / FEHLT)
#
# Laeuft bei JEDER Provisionierung und damit bei jedem IRL-Streamer-OS-Update
# (provision.sh vor "docker compose up"). Neu gebaut wird genau dann, wenn sich
# Dockerfile oder Patch geaendert haben (Fingerabdruck im Image-Label) oder
# zuletzt nur der Rueckfall gebaut werden konnte - so kommen neue guacd-
# Versionen mit jedem Update an, und ein einmal gescheiterter Bau (z.B. kein
# Internet) wird beim naechsten Lauf automatisch nachgeholt.
# Nie fatal: docker compose findet IMMER ein Image irl-guacd-audio:local.
set -u
DIR="$(cd "$(dirname "$0")/../.." && pwd)/docker/guacd-audio"
IMG=irl-guacd-audio:local
QUIET=0
[ "${2:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = 1 ] || echo "$*"; }

fingerprint() { cat "$DIR/Dockerfile" "$DIR/guacd-rdpsnd-v8-wave2.patch" | sha256sum | cut -c1-16; }
base_image() { sed -n 's/^ARG GUACD_BASE=//p' "$DIR/Dockerfile" | head -1; }
label() { docker image inspect -f "{{ index .Config.Labels \"$1\" }}" "$IMG" 2>/dev/null; }

case "${1:-}" in
  status)
    case "$(label org.irlstreameros.variant)" in
      audio) echo "AUDIO $(label org.irlstreameros.fingerprint)";;
      fallback) echo "FALLBACK";;
      *) echo "FEHLT";;
    esac ;;
  ensure)
    FP="$(fingerprint)"
    if [ "$(label org.irlstreameros.variant)" = audio ] && [ "$(label org.irlstreameros.fingerprint)" = "$FP" ]; then
      say "GUACD_AUDIO_OK $FP"; exit 0
    fi
    LOG="$(mktemp)"
    if timeout 1800 docker build --pull=false \
         --label org.irlstreameros.variant=audio --label "org.irlstreameros.fingerprint=$FP" \
         -t "$IMG" "$DIR" >"$LOG" 2>&1; then
      say "GUACD_AUDIO_BUILT $FP"; rm -f "$LOG"
      # alte, nicht mehr benutzte Bauergebnisse (vorige Versionen) wegraeumen
      docker image prune -f --filter "label=org.irlstreameros.variant" >/dev/null 2>&1 || true
      exit 0
    fi
    say "GUACD_AUDIO_BUILD_FAILED - nutze offizielles guacd (ohne Ton)"
    [ "$QUIET" = 1 ] || tail -15 "$LOG"
    rm -f "$LOG"
    # Rueckfall: offizielles Image unter unserem Namen (nur wenn noch keine
    # funktionierende Ton-Variante existiert - eine aeltere Ton-Version bleibt)
    if [ "$(label org.irlstreameros.variant)" != audio ]; then
      B="$(base_image)"
      docker image inspect "$B" >/dev/null 2>&1 || docker pull "$B" >/dev/null 2>&1 || true
      printf 'FROM %s\nLABEL org.irlstreameros.variant=fallback\n' "$B" |
        docker build -q -t "$IMG" - >/dev/null 2>&1 && say "GUACD_FALLBACK_READY"
    fi
    exit 1 ;;
  *)
    echo "Aufruf: $0 ensure|status [--quiet]" >&2; exit 2 ;;
esac
