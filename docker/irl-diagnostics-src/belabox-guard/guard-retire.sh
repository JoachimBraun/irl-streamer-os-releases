#!/bin/bash
# Stoppt eine alte Schutzschicht-Instanz, sobald keine Kamera mehr an ihr haengt (nach einem Versionswechsel).
#   guard-retire.sh <Unit> <Port>
# Die neue Instanz nimmt schon alle neuen Verbindungen an; die alte bleibt nur so lange, bis ihre Kameras sich neu verbinden.
# Nach 24 Stunden wird sie auf jeden Fall beendet.
UNIT="${1:?Unit fehlt}"; PORT="${2:?Port fehlt}"
for _ in $(seq 1 8640); do
  if ! systemctl is-active --quiet "$UNIT"; then exit 0; fi
  if [ "$(ss -Htn state established "( sport = :$PORT )" 2>/dev/null | wc -l)" -eq 0 ]; then break; fi
  sleep 10
done
systemctl disable --now "$UNIT"
