#!/usr/bin/env bash
# IRL Streamer OS - Belabox vorbereiten + eigenen WireGuard-Public-Key ausgeben
#
# Wird per SCP+SSH automatisch vom Mini-PC-Skript
# provision/irl-streamer-fernzugriff-einrichten.sh uebertragen und remote
# ausgefuehrt (Nutzerentscheidung 2026-08-31: automatischer Schluesseltausch
# statt manuellem Copy-Paste, das zu Tippfehlern fuehrte). Nicht fuer
# manuellen Direktaufruf gedacht.
#
# Idempotent: mehrfacher Aufruf schadet nicht, vorhandenes Schluesselpaar
# bleibt erhalten.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Bitte mit sudo ausfuehren." >&2
  exit 1
fi

# SSH dauerhaft bootfest machen - ab Werk deaktiviert
# belabox-firstboot-sshconfig.service SSH nach dem allerersten Boot wieder
# (Security-by-default). BelaUIs eigener SSH-Schalter (start_ssh/stop_ssh)
# ruft nur "systemctl start/stop ssh" auf, NIE "enable" - SSH bleibt sonst
# nur bis zum naechsten Stromverlust an.
systemctl enable --now ssh >/dev/null 2>&1 || true

if ! command -v wg >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y wireguard >/dev/null
fi

mkdir -p /etc/wireguard
chmod 700 /etc/wireguard
if [ ! -f /etc/wireguard/privatekey ]; then
  umask 077
  wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
fi

# Bewusst als LETZTE Ausgabezeile - der Aufrufer (Mini-PC-Skript) liest nur
# die letzte Zeile von stdout aus, um den Schluessel zuverlaessig aus
# eventuellem apt-Output/Warnungen herauszufiltern.
cat /etc/wireguard/publickey
