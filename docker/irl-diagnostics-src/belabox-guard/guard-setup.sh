#!/bin/bash
# IRL Streamer OS - RTMP-Schutzschicht auf der BELABOX einrichten / aktualisieren / entfernen. Laeuft AUF der Box als root.
# Das Dashboard entpackt das Paket nach /opt/irl-rtmp-guard und ruft dieses Skript per SSH auf.
#
#   bash /opt/irl-rtmp-guard/guard-setup.sh install     einspielen bzw. UNTERBRECHUNGSFREI aktualisieren
#   bash /opt/irl-rtmp-guard/guard-setup.sh uninstall   alles wieder entfernen
#   bash /opt/irl-rtmp-guard/guard-setup.sh status      eine JSON-Zeile
#
# Aufbau: Eine Schutzschicht-Instanz (Dienst irl-rtmp-guard, Port 19350) nimmt per Firewall-Umleitung (iptables nat PREROUTING,
# 1935 -> 19350) die Verbindungen der Kameras an, prueft/repariert den RTMP-Strom und reicht ihn an nginx (127.0.0.1:1935) weiter.
# Die Umleitung existiert nur, solange die Instanz laeuft. Fuer ein Update gibt es eine zweite Instanz (irl-rtmp-guard-b, 19351):
# die neue Version startet dort, bekommt per Umleitung alle NEUEN Verbindungen, die alte laeuft aus, bis ihre Kameras sich neu
# verbinden (guard-retire.sh) - bestehende Streams werden nie unterbrochen. Lokale Verbindungen (belacoder -> 127.0.0.1:1935)
# laufen nicht durch die Umleitung. belaUI, nginx, belacoder und srtla werden nicht veraendert. Es werden keine Pakete installiert.
set -u
ROOT=${GUARD_ROOT:-/opt/irl-rtmp-guard}          # (die GUARD_*-Variablen dienen nur den Tests)
UNITDIR=${GUARD_UNITDIR:-/etc/systemd/system}
RUNDIR=${GUARD_RUNDIR:-/run}
UA=irl-rtmp-guard.service
UB=irl-rtmp-guard-b.service
ACTION=${1:-}
[ "$(id -u)" = 0 ] || [ -n "${GUARD_TEST:-}" ] || { echo "Bitte als root ausfuehren." >&2; exit 1; }

file_version() { sha256sum "$ROOT/rtmp_guard.py" 2>/dev/null | cut -c1-12; }
status_file() { if [ "$1" = A ]; then echo "$RUNDIR/irl-rtmp-guard/status.json"; else echo "$RUNDIR/irl-rtmp-guard-b/status.json"; fi; }
unit_of() { if [ "$1" = A ]; then echo "$UA"; else echo "$UB"; fi; }
port_of() { if [ "$1" = A ]; then echo 19350; else echo 19351; fi; }
running_version() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$(status_file "$1")" 2>/dev/null
}
rule_port() {
  for p in 19350 19351; do
    # iptables -C schreibt die Regel je nach Version auf stdout - nur der Rueckgabewert zaehlt
    if iptables -t nat -C PREROUTING -p tcp --dport 1935 -j REDIRECT --to-ports "$p" >/dev/null 2>&1; then echo "$p"; return; fi
  done
  echo ""
}
listening() { ss -tln 2>/dev/null | grep -q ":$1 "; }
wait_listening() { for _ in $(seq 1 50); do listening "$1" && return 0; sleep 0.2; done; return 1; }

case "$ACTION" in
install)
  for c in python3 iptables ss systemctl sha256sum; do
    command -v "$c" >/dev/null 2>&1 || { echo "FEHLT: Programm $c" >&2; exit 1; }
  done
  for f in rtmp_guard.py guard-nat.sh guard-retire.sh irl-rtmp-guard.service irl-rtmp-guard-b.service; do
    [ -f "$ROOT/$f" ] || { echo "FEHLT: $ROOT/$f" >&2; exit 1; }
  done
  if [ -f "$ROOT/SHA256SUMS" ]; then (cd "$ROOT" && sha256sum -c --quiet SHA256SUMS) || { echo "Pruefsumme falsch" >&2; exit 1; }; fi
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$ROOT/rtmp_guard.py" || { echo "rtmp_guard.py fehlerhaft" >&2; exit 1; }
  listening 1935 || { echo "Kein RTMP-Server (nginx) auf Port 1935 - Schutzschicht nicht eingerichtet" >&2; exit 1; }
  chmod 0755 "$ROOT/guard-nat.sh" "$ROOT/guard-retire.sh" "$ROOT/guard-setup.sh"
  chmod 0644 "$ROOT/rtmp_guard.py"
  cp "$ROOT/irl-rtmp-guard.service" "$ROOT/irl-rtmp-guard-b.service" "$UNITDIR/"
  systemctl daemon-reload
  newv=$(file_version)
  aA=0; aB=0
  systemctl is-active --quiet "$UA" && aA=1
  systemctl is-active --quiet "$UB" && aB=1
  if [ $aA = 1 ] && [ $aB = 1 ]; then
    echo "schutzschicht=wartet (die alte Instanz laeuft noch aus, naechster Versuch spaeter)"
    exit 0
  fi
  cur=""
  [ $aA = 1 ] && cur=A
  [ $aB = 1 ] && cur=B
  if [ -z "$cur" ]; then
    target=A
  else
    if [ "$(running_version "$cur")" = "$newv" ]; then
      systemctl enable "$(unit_of "$cur")" >/dev/null 2>&1
      echo "schutzschicht=aktuell version=$newv instanz=$cur"
      exit 0
    fi
    if [ "$cur" = A ]; then target=B; else target=A; fi
  fi
  tu=$(unit_of "$target"); tp=$(port_of "$target")
  systemctl enable --now "$tu" || { echo "Start von $tu fehlgeschlagen" >&2; exit 1; }
  wait_listening "$tp" || { echo "Instanz $target lauscht nicht auf $tp" >&2; systemctl disable --now "$tu" >/dev/null 2>&1; exit 1; }
  sleep 1
  [ "$(rule_port)" = "$tp" ] || { echo "Umleitung zeigt nicht auf Instanz $target" >&2; exit 1; }
  if [ -n "$cur" ]; then
    # alte Instanz ausserhalb dieser Sitzung auslaufen lassen (stoppt, sobald keine Kamera mehr an ihr haengt)
    systemd-run --quiet --collect --unit "irl-rtmp-guard-retire-$(date +%s)" /bin/bash "$ROOT/guard-retire.sh" "$(unit_of "$cur")" "$(port_of "$cur")" >/dev/null 2>&1 \
      || echo "hinweis=alte Instanz $cur muss von Hand gestoppt werden"
    echo "schutzschicht=aktualisiert version=$newv instanz=$target (alte Instanz $cur laeuft aus)"
  else
    echo "schutzschicht=eingerichtet version=$newv instanz=$target"
  fi
  ;;
uninstall)
  systemctl disable --now "$UA" "$UB" >/dev/null 2>&1
  for u in $(systemctl list-units --no-legend --plain 'irl-rtmp-guard-retire-*' 2>/dev/null | awk '{print $1}'); do systemctl stop "$u" >/dev/null 2>&1; done
  if [ -x "$ROOT/guard-nat.sh" ]; then "$ROOT/guard-nat.sh" del 19350 >/dev/null 2>&1; "$ROOT/guard-nat.sh" del 19351 >/dev/null 2>&1; fi
  for p in 19350 19351; do
    while iptables -t nat -C PREROUTING -p tcp --dport 1935 -j REDIRECT --to-ports "$p" >/dev/null 2>&1; do
      iptables -t nat -D PREROUTING -p tcp --dport 1935 -j REDIRECT --to-ports "$p"
    done
  done
  rm -f "$UNITDIR/irl-rtmp-guard.service" "$UNITDIR/irl-rtmp-guard-b.service"
  systemctl daemon-reload
  rm -rf "$ROOT"
  echo "entfernt=ja (Logs bleiben in /var/log/irl-rtmp-guard)"
  ;;
status)
  UNITDIR=$UNITDIR RUNDIR=$RUNDIR python3 - "$ROOT" <<'PY'
import json, os, subprocess, sys
root = sys.argv[1]
def run(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""
def read_json(p):
    try:
        return json.load(open(p))
    except Exception:
        return {}
installed = os.path.isfile(root + "/rtmp_guard.py") and os.path.isfile(os.environ.get("UNITDIR", "/etc/systemd/system") + "/irl-rtmp-guard.service")
out = {"installed": installed}
if installed:
    import hashlib
    out["version"] = hashlib.sha256(open(root + "/rtmp_guard.py", "rb").read()).hexdigest()[:12]
    try:
        out["bundle"] = open(root + "/BUNDLE_HASH").read().strip()
    except OSError:
        out["bundle"] = ""
    inst = {}
    for k, unit, port, st in (("A", "irl-rtmp-guard.service", 19350, os.environ.get("RUNDIR", "/run") + "/irl-rtmp-guard/status.json"),
                              ("B", "irl-rtmp-guard-b.service", 19351, os.environ.get("RUNDIR", "/run") + "/irl-rtmp-guard-b/status.json")):
        active = run("systemctl", "is-active", unit).strip() == "active"
        if not active:
            continue
        s = read_json(st)
        conns = len([l for l in run("ss", "-Htn", "state", "established", "( sport = :%d )" % port).splitlines() if l.strip()])
        inst[k] = {"port": port, "running_version": s.get("version", ""), "connections": conns, "repairs": s.get("repairs", 0),
                   "giveups": s.get("giveups", 0), "last_repair": s.get("last_repair"), "uptime": s.get("uptime", 0)}
    out["instances"] = inst
    rule = ""
    for p in (19350, 19351):
        if subprocess.run(["iptables", "-t", "nat", "-C", "PREROUTING", "-p", "tcp", "--dport", "1935", "-j", "REDIRECT",
                           "--to-ports", str(p)], capture_output=True).returncode == 0:
            rule = str(p)
            break
    out["rule_port"] = rule
    out["service"] = "active" if inst else "inactive"
print(json.dumps(out))
PY
  ;;
*)
  echo "Aufruf: $0 install|uninstall|status" >&2
  exit 2
  ;;
esac
