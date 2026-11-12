#!/usr/bin/env bash
# Tastatur-Kick beim naechsten Login (Autostart, siehe provision.sh "Zusaetzlicher Kick-Fix"):
# GNOME uebernimmt den dconf-Default [('xkb', 'de')] in einer frischen Sitzung nicht zuverlaessig
# in den Compositor - ein echter Quellenwechsel us -> de aktiviert ihn. Danach entfernt sich der
# Autostart-Eintrag selbst.
# Review 11.11.2026 (A11): eigenes Skript statt "bash -c" in der Exec-Zeile - die maskierten
# Anfuehrungszeichen dort sind in .desktop-Dateien ungueltig ("Undefined escape sequence"),
# der Eintrag lief deshalb nie.
SCHEMA="org.gnome.desktop.input-sources"
AUTOSTART="${HOME}/.config/autostart/irl-streamer-keyboard-fix.desktop"

# Eigene Eingabequellen des Kunden nie ueberschreiben - nur beim Standard (de/leer) anstossen.
CURRENT="$(gsettings get "${SCHEMA}" sources 2>/dev/null || true)"
case "${CURRENT}" in
  "[('xkb', 'de')]"|"@a(ss) []"|"[]"|"")
    sleep 2
    gsettings set "${SCHEMA}" sources "[('xkb', 'us'), ('xkb', 'de')]"
    sleep 1
    gsettings set "${SCHEMA}" current 1
    sleep 1
    gsettings set "${SCHEMA}" sources "[('xkb', 'de')]"
    ;;
esac
rm -f "${AUTOSTART}"
