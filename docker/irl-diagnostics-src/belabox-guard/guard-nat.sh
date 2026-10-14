#!/bin/sh
# Umleitung eingehender RTMP-Verbindungen (Port 1935) auf die laufende Schutzschicht-Instanz.
#   guard-nat.sh add [PORT]   Umleitung auf PORT (Standard 19350) setzen, Umleitungen auf andere Instanzen entfernen
#   guard-nat.sh del [PORT]   Umleitung auf PORT entfernen; laeuft die andere Instanz noch, geht die Umleitung dorthin
# Instanz A = irl-rtmp-guard.service (19350), Instanz B = irl-rtmp-guard-b.service (19351): so laesst sich eine neue Version
# einspielen, ohne laufende Kameraverbindungen zu trennen (die bleiben bei der alten Instanz, neue gehen an die neue).
# Lokale Verbindungen (belacoder -> 127.0.0.1:1935) laufen nicht durch PREROUTING und bleiben unberuehrt.
ACT="$1"; PORT="${2:-19350}"
has_rule() { iptables -t nat -C PREROUTING -p tcp --dport 1935 -j REDIRECT --to-ports "$1" >/dev/null 2>&1; }
add_rule() { has_rule "$1" || iptables -t nat -I PREROUTING 1 -p tcp --dport 1935 -j REDIRECT --to-ports "$1"; }
del_port() { while has_rule "$1"; do iptables -t nat -D PREROUTING -p tcp --dport 1935 -j REDIRECT --to-ports "$1"; done; }
case "$ACT" in
  add)
    ok=0
    for _ in $(seq 1 30); do
      if ss -tln 2>/dev/null | grep -q ":$PORT "; then ok=1; break; fi
      sleep 0.2
    done
    [ "$ok" = 1 ] || { echo "Schutzschicht lauscht nicht auf $PORT - keine Umleitung gesetzt" >&2; exit 1; }
    add_rule "$PORT"
    for o in 19350 19351; do [ "$o" = "$PORT" ] || del_port "$o"; done
    ;;
  del)
    del_port "$PORT"
    other=19351; unit=irl-rtmp-guard-b.service
    if [ "$PORT" = 19351 ]; then other=19350; unit=irl-rtmp-guard.service; fi
    if systemctl is-active --quiet "$unit" && ss -tln 2>/dev/null | grep -q ":$other "; then
      add_rule "$other"
    fi
    exit 0
    ;;
  *) echo "usage: $0 add|del [PORT]" >&2; exit 2 ;;
esac
