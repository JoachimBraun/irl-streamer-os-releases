#!/bin/bash
# IRL Streamer OS - Sendekette der BELABOX einrichten / aktualisieren / entfernen. Laeuft AUF der Box als root.
# Das Dashboard entpackt das Paket nach /opt/irl-net und ruft dieses Skript per SSH auf (Autopilot wie die RTMP-Schutzschicht).
#
#   bash /opt/irl-net/net-setup.sh install     einspielen bzw. aktualisieren (waehrend einer Sendung: belaUI-Neustart zurueckgestellt)
#   bash /opt/irl-net/net-setup.sh uninstall   alles zurueck auf das BELABOX-Original
#   bash /opt/irl-net/net-setup.sh status      eine JSON-Zeile
#
# Inhalt (Ideen aus IRL4YOU BOX v1.1.1, siehe README.md):
#   1. Ersatz-Internetweg je Netzkarte      /etc/dhcp/dhclient-exit-hooks.d/irl-net-fallback-route
#   2. belacoder = Original + nur Kennzahlen-Datei (Overlay, Haenger-Waechter)  /usr/bin/belacoder (Original per dpkg-divert)
#      Regler unveraendert. srtla_send bleibt Original (fruehere Wegewahl-Patches werden beim Einspielen entfernt).
#      Einstellung ueber die Umgebung von belaUI: /etc/systemd/system/belaUI.service.d/irl-net.conf
#   3. Haenger-Waechter                        irl-net-watch.service
# Live-Vergleich 25.10. (nur Mobilfunk, je 10 min): mit den IRL4YOU-Regler-/Wegewahl-Patches 23-27 Einbrueche < 800 kbit und
# 1800-3100 verlorene Pakete, Original 6 und 35 -> diese Patches sind bewusst NICHT mehr dabei.
# Der belacoder passt nur zum Upstream-Stand BC_UPSTREAM. Liefert ein BELABOX-Update einen anderen Stand, bleibt (bzw. wird
# wieder) das Original aktiv - nie ein gepatchtes Programm gegen eine neuere Box.
set -u
ROOT=${NET_ROOT:-/opt/irl-net}
UNITDIR=/etc/systemd/system
HOOK=/etc/dhcp/dhclient-exit-hooks.d/irl-net-fallback-route
DROPIN_DIR=$UNITDIR/belaUI.service.d
DROPIN=$DROPIN_DIR/irl-net.conf
WATCH_UNIT=irl-net-watch.service
BC_UPSTREAM=ccce9ca
BC_OURS=ccce9ca-irl3
ACTION=${1:-}
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausfuehren." >&2; exit 1; }

streaming() { pgrep -x belacoder >/dev/null || pgrep -x srtla_send >/dev/null; }
ver() { [ -x "$1" ] && timeout 5 "$1" -v 2>/dev/null | head -1 | tr -d '\r'; }
diverted() { dpkg-divert --listpackage "$1" 2>/dev/null | grep -qx LOCAL; }

# $1 Programmpfad, $2 Paketdatei, $3 erwarteter Upstream-Stand, $4 eigener Stand
put_binary() {
  local dst=$1 src=$2 up=$3 ours=$4 orig
  orig=$dst; diverted "$dst" && orig=$dst.distrib
  local have; have=$(ver "$orig")
  if [ "${have:0:7}" != "$up" ]; then
    echo "$(basename "$dst")=original (Box hat Stand '${have:-?}', Patches passen nur zu $up)"
    if diverted "$dst"; then rm -f "$dst"; dpkg-divert --local --rename --remove "$dst" >/dev/null 2>&1; fi
    return 0
  fi
  if ! diverted "$dst"; then
    dpkg-divert --local --rename --divert "$dst.distrib" --add "$dst" >/dev/null || { echo "dpkg-divert fuer $dst fehlgeschlagen" >&2; return 1; }
  fi
  install -m 0755 "$ROOT/$src" "$dst.irl-neu" && mv -f "$dst.irl-neu" "$dst"
  [ "$(ver "$dst")" = "$ours" ] || { echo "$dst meldet nicht $ours" >&2; rm -f "$dst"; dpkg-divert --local --rename --remove "$dst" >/dev/null 2>&1; return 1; }
  echo "$(basename "$dst")=$ours"
}

restore_binary() {
  if diverted "$1"; then rm -f "$1"; dpkg-divert --local --rename --remove "$1" >/dev/null 2>&1; fi
}

restart_belaui() {
  if streaming; then echo "belaui_restart=zurueckgestellt (es wird gerade gesendet)"; return 0; fi
  systemctl restart belaUI && echo "belaui_restart=ok"
}

# Ersatzrouten sofort fuer alle schon verbundenen dhclient-Karten setzen (sonst erst beim naechsten DHCP-Ereignis)
apply_fallback_now() {
  local ifc gw idx
  for p in /sys/class/net/*; do
    ifc=${p##*/}
    pgrep -f "dhclient.*[ .]$ifc( |$)" >/dev/null 2>&1 || pgrep -f "dhclient.*$ifc\.leases" >/dev/null 2>&1 || continue
    gw=$(ip route show table "$ifc" 2>/dev/null | awk '/^default via/ {print $3; exit}')
    idx=$(cat "/sys/class/net/$ifc/ifindex" 2>/dev/null)
    [ -n "$gw" ] && [ -n "$idx" ] && ip route replace default via "$gw" dev "$ifc" metric $((100 + idx)) 2>/dev/null && echo "ersatzweg=$ifc"
  done
}

case "$ACTION" in
install)
  [ "$(uname -m)" = aarch64 ] || { echo "Nur aarch64 (RK3588) unterstuetzt" >&2; exit 1; }
  [ -f /opt/belaUI/belaUI.js ] || { echo "Keine BELABOX (belaUI fehlt)" >&2; exit 1; }
  for f in belacoder-aarch64 irl-net-fallback-route irl_net_watch.py irl-net-watch.service; do
    [ -f "$ROOT/$f" ] || { echo "FEHLT: $ROOT/$f" >&2; exit 1; }
  done
  if [ -f "$ROOT/SHA256SUMS" ]; then (cd "$ROOT" && sha256sum -c --quiet SHA256SUMS) || { echo "Pruefsumme falsch" >&2; exit 1; }; fi
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$ROOT/irl_net_watch.py" || { echo "irl_net_watch.py fehlerhaft" >&2; exit 1; }
  mkdir -p /run/irl-net /var/log/irl-net
  # 1. Ersatz-Internetweg
  install -m 0644 "$ROOT/irl-net-fallback-route" "$HOOK"
  apply_fallback_now
  # 2. Programme (laufende Prozesse behalten ihre alte Datei, die neue gilt ab dem naechsten Start)
  restore_binary /usr/bin/srtla_send; echo "srtla_send=original"
  put_binary /usr/bin/belacoder belacoder-aarch64 "$BC_UPSTREAM" "$BC_OURS" || echo "warnung=belacoder nicht ersetzt"
  mkdir -p "$DROPIN_DIR"
  want=$(cat <<'CONF'
# IRL Streamer OS - Sendekette (net-setup.sh). belaUI gibt diese Umgebung an belacoder und srtla_send weiter.
#   BELACODER_STATS_FILE: Kennzahlen je Sekunde (Dashboard-Overlay, Haenger-Waechter). Sonst nichts - Regler und Wegewahl Original.
[Service]
Environment=BELACODER_STATS_FILE=/run/irl-net/belacoder.json
ExecStartPre=/bin/mkdir -p /run/irl-net
CONF
)
  changed=0
  [ "$(cat "$DROPIN" 2>/dev/null)" = "$want" ] || { printf '%s\n' "$want" > "$DROPIN"; changed=1; }
  # 4. Haenger-Waechter
  install -m 0644 "$ROOT/irl-net-watch.service" "$UNITDIR/$WATCH_UNIT"
  systemctl daemon-reload
  systemctl enable "$WATCH_UNIT" >/dev/null 2>&1 && systemctl restart "$WATCH_UNIT"
  echo "waechter=$(systemctl is-active "$WATCH_UNIT")"
  # belaUI braucht die neue Umgebung: neu starten, wenn sie fehlt (nur ohne Sendung)
  bp=$(systemctl show -p MainPID --value belaUI 2>/dev/null)
  if [ $changed = 1 ] || ! tr '\0' '\n' < "/proc/${bp:-0}/environ" 2>/dev/null | grep -qx 'BELACODER_STATS_FILE=/run/irl-net/belacoder.json'; then
    restart_belaui
  else
    echo "belaui_restart=nicht noetig"
  fi
  ;;
uninstall)
  systemctl disable --now "$WATCH_UNIT" >/dev/null 2>&1
  rm -f "$UNITDIR/$WATCH_UNIT" "$DROPIN"
  rmdir "$DROPIN_DIR" 2>/dev/null
  restore_binary /usr/bin/belacoder
  restore_binary /usr/bin/srtla_send
  rm -f "$HOOK"
  for p in /sys/class/net/*; do
    ifc=${p##*/}
    idx=$(cat "/sys/class/net/$ifc/ifindex" 2>/dev/null)
    [ -n "$idx" ] && ip route del default dev "$ifc" metric $((100 + idx)) 2>/dev/null
  done
  systemctl daemon-reload
  restart_belaui
  rm -rf "$ROOT" /run/irl-net
  echo "entfernt=ja (Berichte bleiben in /var/log/irl-net)"
  ;;
status)
  BC_OURS=$BC_OURS HOOK=$HOOK DROPIN=$DROPIN python3 - "$ROOT" <<'PY'
import json, os, subprocess, sys, time
root = sys.argv[1]
def run(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""
def ver(p):
    return run(p, "-v").splitlines()[0] if os.access(p, os.X_OK) and run(p, "-v") else ""
installed = os.path.isfile(root + "/net-setup.sh") and os.path.isfile("/etc/systemd/system/irl-net-watch.service")
out = {"installed": installed}
if installed:
    try:
        out["bundle"] = open(root + "/BUNDLE_HASH").read().strip()
    except OSError:
        out["bundle"] = ""
    out["belacoder"] = ver("/usr/bin/belacoder")
    out["srtla_send"] = ver("/usr/bin/srtla_send")
    out["belacoder_patched"] = out["belacoder"] == os.environ["BC_OURS"]
    out["srtla_patched"] = out["srtla_send"].endswith(("-irl1", "-irl2"))
    out["fallback_hook"] = os.path.isfile(os.environ["HOOK"])
    out["dropin"] = os.path.isfile(os.environ["DROPIN"])
    pid = run("systemctl", "show", "-p", "MainPID", "--value", "belaUI")
    try:
        env = open("/proc/%s/environ" % pid, "rb").read().split(b"\0")
        out["belaui_env"] = b"BELACODER_STATS_FILE=/run/irl-net/belacoder.json" in env
    except Exception:
        out["belaui_env"] = False
    out["watch"] = run("systemctl", "is-active", "irl-net-watch.service")
    try:
        w = json.load(open("/run/irl-net/watch.json"))
        out["interventions_last_hour"] = w.get("interventions_last_hour", 0)
        out["last_intervention"] = w.get("last_intervention")
        out["last_reason"] = w.get("last_reason", "")
    except Exception:
        pass
    try:
        out["stats_age_s"] = round(time.time() - os.stat("/run/irl-net/belacoder.json").st_mtime, 1)
    except OSError:
        out["stats_age_s"] = None
    out["fallback_routes"] = len([l for l in run("ip", "route", "show", "default").splitlines() if " metric 1" in l and "proto" not in l])
print(json.dumps(out))
PY
  ;;
*)
  echo "Aufruf: $0 install|uninstall|status" >&2
  exit 2
  ;;
esac
