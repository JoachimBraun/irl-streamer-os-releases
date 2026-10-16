#!/bin/bash
# IRL Streamer OS - Bild-in-Bild (PiP) auf der BELABOX einrichten / entfernen. Laeuft AUF der Box als root.
# Das Dashboard entpackt das Paket nach /opt/irl-pip und ruft dieses Skript per SSH auf.
#
#   bash /opt/irl-pip/irl-pip-setup.sh install     Plugin + Dienst + Pipeline "custom/irl_pip" einrichten
#   bash /opt/irl-pip/irl-pip-setup.sh uninstall   alles wieder entfernen (Konfiguration inkl.)
#   bash /opt/irl-pip/irl-pip-setup.sh status      eine JSON-Zeile
#
# Ausserhalb von /opt/irl-pip wird NUR Folgendes angelegt (alles in /opt/irl-pip/MANIFEST):
#   - GStreamer-Plugin libgstirlpip.so      - belacoder-Pipeline custom/irl_pip
#   - Dienst irl-pip.service                - Werkzeug /usr/local/sbin/irl-pip-cli
# belaUI, nginx, belacoder und srtla werden nicht veraendert. Das einzige zusaetzliche Debian-Paket ist
# gstreamer1.0-tools (gst-launch-1.0, fuer Kamera-Schnappschuesse im Dashboard - alle GStreamer-Bibliotheken
# selbst sind auf der Box schon vorhanden, das Paket liefert nur den duennen CLI-Wrapper dazu).
set -u
ROOT=/opt/irl-pip
PLUGIN_DST=/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstirlpip.so
PIPE_DST=/usr/share/belacoder/pipelines/custom/irl_pip
UNIT_DST=/etc/systemd/system/irl-pip.service
CLI_DST=/usr/local/sbin/irl-pip-cli
ACTION=${1:-}
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausfuehren." >&2; exit 1; }

restart_belaui() {
  # belaUI liest die Pipeline-LISTE nur beim eigenen Start. Waehrend einer Sendung nie neu starten.
  if pgrep -x belacoder >/dev/null; then echo "belaui_restart=zurueckgestellt (es wird gerade gesendet)"; return 1; fi
  systemctl restart belaUI && echo "belaui_restart=ok"
}

case "$ACTION" in
install)
  for f in libgstirlpip-aarch64.so irl-pip-ctl.py irl_pip_gen.py irl-pip-cli; do
    [ -f "$ROOT/$f" ] || { echo "FEHLT: $ROOT/$f" >&2; exit 1; }
  done
  [ -d /usr/share/belacoder/pipelines ] || { echo "Keine BELABOX (belacoder-Pipelines fehlen)" >&2; exit 1; }
  [ "$(uname -m)" = aarch64 ] || { echo "Nur aarch64 (RK3588) unterstuetzt" >&2; exit 1; }
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
  cp "$ROOT/irl-pip-ctl.py" "$ROOT/irl_pip_gen.py" "$ROOT/bin/"
  install -m 0644 "$ROOT/libgstirlpip-aarch64.so" "$PLUGIN_DST"
  install -m 0755 "$ROOT/irl-pip-cli" "$CLI_DST"
  [ -s "$ROOT/state/token" ] || { head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$ROOT/state/token"; chmod 600 "$ROOT/state/token"; }
  mkdir -p "$(dirname "$PIPE_DST")"
  python3 - <<'PY' || exit 1
import sys
sys.path.insert(0, "/opt/irl-pip/bin")
import irl_pip_gen as g
cfg = g.validate_config({"main": "cam1", "pips": [{"cam": "cam2"}]})
open("/usr/share/belacoder/pipelines/custom/irl_pip", "w").write(g.render_pipeline(g.resolve_layout(cfg, {"cam1": g.DEFAULT_RES, "cam2": (1280, 720)})))
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
  printf '%s\n' "$PLUGIN_DST" "$PIPE_DST" "$UNIT_DST" "$CLI_DST" > "$ROOT/MANIFEST"
  # Nicht "enable --now": das startet einen bereits laufenden Dienst NICHT neu, dann laeuft weiter der
  # alte Code im Arbeitsspeicher (z. B. ohne neue Konfigurationsfelder). Deshalb immer explizit (re)starten -
  # ungefaehrlich auch waehrend einer Sendung: irl-pip.service ist nur die Steuerung, belacoder laeuft getrennt weiter.
  systemctl daemon-reload && systemctl enable irl-pip.service >/dev/null 2>&1 && systemctl restart irl-pip.service
  sleep 2
  echo "dienst=$(systemctl is-active irl-pip.service)"
  restart_belaui || true   # bei laufender Sendung ABSICHTLICH zurueckgestellt - das ist kein Installationsfehler
  ;;
uninstall)
  systemctl disable --now irl-pip.service >/dev/null 2>&1
  rm -f "$UNIT_DST" "$PLUGIN_DST" "$PIPE_DST" "$CLI_DST"
  systemctl daemon-reload
  rm -rf /run/irl-pip
  if pgrep -x belacoder >/dev/null && grep -q irlpipmix /tmp/belacoder_pipeline 2>/dev/null; then
    echo "warnung=es wird noch mit der PiP-Pipeline gesendet; Sendung stoppen"
  fi
  rm -rf "$ROOT"
  restart_belaui
  echo "entfernt=ja"
  ;;
status)
  inst=0; [ -e "$PLUGIN_DST" ] && [ -e "$UNIT_DST" ] && [ -e "$PIPE_DST" ] && inst=1
  printf '{"installed": %s, "service": "%s", "plugin_ok": %s, "pipeline_ok": %s}\n' \
    "$([ $inst = 1 ] && echo true || echo false)" "$(systemctl is-active irl-pip.service 2>/dev/null)" \
    "$([ -e "$PLUGIN_DST" ] && echo true || echo false)" "$([ -e "$PIPE_DST" ] && echo true || echo false)"
  ;;
*) echo "Aufruf: $0 install|uninstall|status" >&2; exit 1;;
esac
