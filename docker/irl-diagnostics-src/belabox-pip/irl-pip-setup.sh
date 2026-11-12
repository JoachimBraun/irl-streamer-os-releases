#!/bin/bash
# IRL Streamer OS - Bild-in-Bild (PiP) auf der BELABOX einrichten / entfernen. Laeuft AUF der Box als root.
# Das Dashboard entpackt das Paket nach /opt/irl-pip und ruft dieses Skript per SSH auf.
#
#   bash /opt/irl-pip/irl-pip-setup.sh install     Plugin + Dienst + Pipeline "custom/irl_pip" einrichten
#   bash /opt/irl-pip/irl-pip-setup.sh uninstall   alles wieder entfernen (Konfiguration inkl.)
#   bash /opt/irl-pip/irl-pip-setup.sh status      eine JSON-Zeile
#
# Ausserhalb von /opt/irl-pip wird NUR Folgendes angelegt (alles in /opt/irl-pip/MANIFEST):
#   - GStreamer-Plugin libgstirlpip2.so (IRL-PiP 2.0)   - belacoder-Pipeline custom/irl_pip
#   - Dienst irl-pip.service                - Werkzeug /usr/local/sbin/irl-pip-cli
# PiP 1.0 (libgstirlpip.so, irl_pip_gen.py) ist seit V1.112 entfernt; install raeumt Reste davon weg.
#   - Dienst irl-pip-cpu.service (schnelle CPU-Kerne fest auf Hoechsttakt, siehe unten)
# belaUI, nginx, belacoder und srtla werden nicht veraendert. Das einzige zusaetzliche Debian-Paket ist
# gstreamer1.0-tools (gst-launch-1.0, fuer Kamera-Schnappschuesse im Dashboard - alle GStreamer-Bibliotheken
# selbst sind auf der Box schon vorhanden, das Paket liefert nur den duennen CLI-Wrapper dazu).
set -u
ROOT=${IRL_PIP_ROOT:-/opt/irl-pip}
# Rest von PiP 1.0 (bis V1.111): wird bei install/uninstall entfernt
LEGACY_PLUGIN_DST=/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstirlpip.so
PLUGIN2_DST=${IRL_PIP_PLUGIN2:-/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstirlpip2.so}
PIPE_DST=${IRL_PIP_PIPELINE:-/usr/share/belacoder/pipelines/custom/irl_pip}
UNITDIR=${IRL_PIP_UNITDIR:-/etc/systemd/system}
UNIT_DST=$UNITDIR/irl-pip.service
CLI_DST=${IRL_PIP_CLI:-/usr/local/sbin/irl-pip-cli}
CPU_UNIT_DST=$UNITDIR/irl-pip-cpu.service
BELA_TMP=${IRL_PIP_BELA_TMP:-/tmp/belacoder_pipeline}
# Schnelle Kerne (RK3588: A76, max 2,3-2,4 GHz) fest auf Hoechsttakt. Live 24.10.: mit schedutil liefen sie beim
# stossweisen Mischen (30x/s kurz) nur mit 816 MHz -> 16 ms je Bild, 1,7 % der Bilder zu spaet (Ruckler);
# mit performance 8,6 ms und 1 verspaetetes Bild/min. Nur Kerne mit >= 2 GHz Hoechsttakt, die A55 bleiben geregelt.
# Als eigene Datei (nicht inline in ExecStart): systemd wuerde $p in der Unit selbst ersetzen.
CPU_SCRIPT=$ROOT/bin/irl-pip-cpu.sh
ACTION=${1:-}
[ "$(id -u)" = 0 ] || [ -n "${PIP_TEST:-}" ] || { echo "Bitte als root ausfuehren." >&2; exit 1; }

pip_streaming() {
  # Sendet belacoder gerade mit der Bild-in-Bild-Pipeline? Laufende Kamera-Zubringer zaehlen ebenfalls (sie laufen nach
  # einem belacoder-Neustart kurz weiter, der neue belacoder nimmt ihre Bilder sofort wieder). [k]: pgrep -f soll keine
  # Shell finden, deren Kommandozeile nur das Suchwort enthaelt.
  { pgrep -x belacoder >/dev/null && grep -q irlpip2vsrc "$BELA_TMP" 2>/dev/null; } || pgrep -f 'irlpip2vsin[k]' >/dev/null
}

restart_belaui() {
  # belaUI liest die Pipeline-LISTE nur beim eigenen Start. Waehrend einer Sendung nie neu starten.
  if pgrep -x belacoder >/dev/null; then echo "belaui_restart=zurueckgestellt (es wird gerade gesendet)"; return 1; fi
  systemctl restart belaUI && echo "belaui_restart=ok"
}

case "$ACTION" in
install)
  for f in libgstirlpip2-aarch64.so irl-pip-ctl.py irl_pip2_gen.py irl-pip-cli; do
    [ -f "$ROOT/$f" ] || { echo "FEHLT: $ROOT/$f" >&2; exit 1; }
  done
  [ -d "$(dirname "$(dirname "$PIPE_DST")")" ] || { echo "Keine BELABOX (belacoder-Pipelines fehlen)" >&2; exit 1; }
  [ "$(uname -m)" = aarch64 ] || { echo "Nur aarch64 (RK3588) unterstuetzt" >&2; exit 1; }
  # Review 11.11. C4: waehrend einer Sendung mit Bild-in-Bild NICHTS aktivieren. Ein Dienst-Neustart beendet alle
  # Zubringer (cgroup), ein neuer Generator koennte den Encoder neu starten, und neu gestartete Zubringer wuerden das
  # neue Plugin laden, waehrend belacoder das alte behaelt. Auch kein apt-get (Download ueber die Internetwege der
  # Sendung). Die Dateien liegen entpackt in $ROOT; das naechste install ohne Sendung aktiviert sie (bin/BUNDLE_HASH
  # bleibt bis dahin alt -> Dashboard meldet "Paket veraltet").
  if pip_streaming; then echo "aktivierung=zurueckgestellt (es wird gerade mit Bild-in-Bild gesendet)"; exit 0; fi
  if ! command -v gst-launch-1.0 >/dev/null 2>&1; then
    echo "gst-launch-1.0 fehlt - installiere gstreamer1.0-tools (fuer Kamera-Schnappschuesse) ..."
    if DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gstreamer1.0-tools >/tmp/irl-pip-apt.log 2>&1; then
      echo "gst_tools_installiert=ja"
    else
      echo "warnung=gstreamer1.0-tools konnte nicht installiert werden (offline?) - Kamera-Schnappschuesse bleiben ohne Bild, Rest der Einrichtung laeuft trotzdem weiter" >&2
    fi
  fi
  if [ -f "$ROOT/SHA256SUMS" ]; then (cd "$ROOT" && sha256sum -c --quiet --ignore-missing SHA256SUMS) || { echo "Pruefsumme falsch" >&2; exit 1; }; fi
  mkdir -p "$ROOT/bin" "$ROOT/state"; chmod 700 "$ROOT/state"
  cp "$ROOT/irl-pip-ctl.py" "$ROOT/irl_pip2_gen.py" "$ROOT/bin/"
  install -m 0644 "$ROOT/libgstirlpip2-aarch64.so" "$PLUGIN2_DST"; echo "plugin2=ja"
  # Reste von PiP 1.0 entfernen (eine noch laufende Sendung behaelt die geladene Datei, das Loeschen stoert sie nicht)
  rm -f "$LEGACY_PLUGIN_DST" "$ROOT/bin/irl_pip_gen.py" "$ROOT/irl_pip_gen.py" "$ROOT/libgstirlpip-aarch64.so"
  install -m 0755 "$ROOT/irl-pip-cli" "$CLI_DST"
  [ -s "$ROOT/state/token" ] || { head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$ROOT/state/token"; chmod 600 "$ROOT/state/token"; }
  mkdir -p "$(dirname "$PIPE_DST")"
  ROOT="$ROOT" PIPE_DST="$PIPE_DST" python3 - <<'PY' || exit 1
import os, sys
sys.path.insert(0, os.environ["ROOT"] + "/bin")
import irl_pip2_gen as g
cfg = g.validate_config({"main": "cam1", "pips": [{"cam": "cam2"}]})
open(os.environ["PIPE_DST"], "w").write(g.render_pipeline(g.resolve_layout(cfg, {"cam1": g.DEFAULT_RES, "cam2": (1280, 720)})))
PY
  chmod 644 "$PIPE_DST"
  cat > "$UNIT_DST" <<UNIT
[Unit]
Description=IRL Streamer OS - Bild-in-Bild-Dienst (irl-pip)
After=network.target nginx.service

[Service]
Type=simple
Environment=IRL_PIP_BIND=127.0.0.1
ExecStart=/usr/bin/python3 $ROOT/bin/irl-pip-ctl.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
  cat > "$CPU_SCRIPT" <<'SH'
#!/bin/sh
# irl-pip-cpu.sh set|reset - schnelle Kerne (Hoechsttakt >= 2 GHz) auf performance bzw. zurueck auf schedutil
gov=performance; [ "${1:-set}" = reset ] && gov=schedutil
for p in /sys/devices/system/cpu/cpufreq/policy*; do
  [ "$(cat "$p/cpuinfo_max_freq" 2>/dev/null || echo 0)" -ge 2000000 ] && echo "$gov" > "$p/scaling_governor"
done
exit 0
SH
  chmod 755 "$CPU_SCRIPT"
  cat > "$CPU_UNIT_DST" <<UNIT
[Unit]
Description=IRL Streamer OS - schnelle CPU-Kerne auf Hoechsttakt (ruckelfreies Bild-in-Bild)

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh $CPU_SCRIPT set
ExecStop=/bin/sh $CPU_SCRIPT reset

[Install]
WantedBy=multi-user.target
UNIT
  printf '%s\n' "$PLUGIN2_DST" "$PIPE_DST" "$UNIT_DST" "$CLI_DST" "$CPU_UNIT_DST" > "$ROOT/MANIFEST"
  # Nicht "enable --now": das startet einen bereits laufenden Dienst NICHT neu, dann laeuft weiter der alte Code im
  # Arbeitsspeicher. Deshalb immer explizit (re)starten - NUR ohne Bild-in-Bild-Sendung (siehe oben): der Neustart
  # beendet alle Zubringer (Kamerabilder 2-4 s weg + Lernphase).
  systemctl daemon-reload && systemctl enable irl-pip.service >/dev/null 2>&1 && systemctl restart irl-pip.service
  # Takt setzen ist auch waehrend einer Sendung ungefaehrlich (keine Unterbrechung)
  systemctl enable irl-pip-cpu.service >/dev/null 2>&1 && systemctl restart irl-pip-cpu.service
  sleep 2
  echo "dienst=$(systemctl is-active irl-pip.service)"
  [ -f "$ROOT/BUNDLE_HASH" ] && cp "$ROOT/BUNDLE_HASH" "$ROOT/bin/BUNDLE_HASH"   # erst jetzt gilt der neue Stand
  echo "cpu_takt=$(systemctl is-active irl-pip-cpu.service) $(cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor 2>/dev/null | tr '\n' ' ')"
  restart_belaui || true   # bei laufender Sendung ABSICHTLICH zurueckgestellt - das ist kein Installationsfehler
  ;;
uninstall)
  systemctl disable --now irl-pip.service irl-pip-cpu.service >/dev/null 2>&1
  rm -f "$UNIT_DST" "$CPU_UNIT_DST" "$LEGACY_PLUGIN_DST" "$PLUGIN2_DST" "$PIPE_DST" "$CLI_DST"
  systemctl daemon-reload
  rm -rf /run/irl-pip
  rm -f /dev/shm/irlpip2-slot*    # gemeinsamer Speicher der Kamera-Zubringer (IRL-PiP 2.0)
  if pgrep -x belacoder >/dev/null && grep -q -e irlpip2vsrc -e irlpipmix /tmp/belacoder_pipeline 2>/dev/null; then
    echo "warnung=es wird noch mit der PiP-Pipeline gesendet; Sendung stoppen"
  fi
  rm -rf "$ROOT"
  restart_belaui
  echo "entfernt=ja"
  ;;
status)
  inst=0; [ -e "$PLUGIN2_DST" ] && [ -e "$UNIT_DST" ] && [ -e "$PIPE_DST" ] && inst=1
  printf '{"installed": %s, "service": "%s", "plugin_ok": %s, "pipeline_ok": %s, "plugin2_ok": %s, "bundle": "%s"}\n' \
    "$([ $inst = 1 ] && echo true || echo false)" "$(systemctl is-active irl-pip.service 2>/dev/null)" \
    "$([ -e "$PLUGIN2_DST" ] && echo true || echo false)" "$([ -e "$PIPE_DST" ] && echo true || echo false)" \
    "$([ -e "$PLUGIN2_DST" ] && echo true || echo false)" "$(tr -cd '0-9a-f' < "$ROOT/bin/BUNDLE_HASH" 2>/dev/null)"
  ;;
*) echo "Aufruf: $0 install|uninstall|status" >&2; exit 1;;
esac
