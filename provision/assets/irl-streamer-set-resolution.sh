#!/usr/bin/env bash
# Wiederholungsschleife statt Einmalversuch - live gefunden (2026-08-25 auf
# der Test-VM): direkt nach dem Login ist Mutters Wayland-Monitor-Management
# manchmal noch nicht bereit, "gdctl" schlaegt dann fehl, was das Skript
# bisher per "2>/dev/null || true" stillschweigend verschluckt hat (manuell
# Sekunden spaeter ausgefuehrt hat derselbe Befehl anstandslos funktioniert).
# Eine Schleife ist robuster als eine geratene feste Verzoegerung, weil
# unklar ist wie lange eine schwaechere Mini-PC-GPU dafuer braucht.
for _ in $(seq 1 15); do
  CONNECTOR="$(gdctl show 2>/dev/null | grep -oP '(?<=Monitor )\S+' | head -1)"
  if [ -n "${CONNECTOR}" ] && gdctl show --verbose 2>/dev/null | grep -q '1920x1080@60\.000'; then
    if gdctl set --logical-monitor --monitor "${CONNECTOR}" --mode 1920x1080@60.000 --primary 2>/dev/null; then
      exit 0
    fi
  fi
  sleep 1
done
