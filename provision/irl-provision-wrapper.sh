#!/usr/bin/env bash
# IRL Streamer OS - grafischer Fortschrittsbalken fuer die Ersteinrichtung
#
# Ersetzt das bisherige nackte Terminalfenster durch einen Zenity-
# Fortschrittsbalken (Prozentanzeige). Das komplette Rohlog von
# provision.sh laeuft PARALLEL unveraendert in eine Logdatei, damit bei
# einem Fehler weiterhin alles nachvollziehbar ist (Nutzerwunsch
# 2026-09-01: "Fortschrittsbalken mit Prozentanzeige, Rohlog laeuft
# parallel in eine Datei mit").
#
# Funktionsweise: provision.sh gibt normale log()-Zeilen aus PLUS
# spezielle "PROGRESS:<prozent>:<text>"-Zeilen an den markierten
# Hauptschritten (siehe progress()-Funktion in provision.sh).
#   1. "tee" schreibt JEDE Zeile (Rohlog UND Progress-Marker) 1:1 in die
#      Logdatei, unabhaengig davon was danach mit dem Stream passiert.
#   2. "grep"+"sed" filtern NUR die PROGRESS-Zeilen heraus und wandeln sie
#      in das Format um, das "zenity --progress" auf stdin erwartet
#      (eine Zahl = Prozent, "# Text" = Statuszeile).
#
# sudo laeuft ueber einen NOPASSWD-sudoers-Eintrag (siehe provision.sh,
# Abschnitt Lizenz-Icon) - kein Passwort-Prompt noetig, gleiches Muster
# wie beim "Lizenz aktivieren"-Dialog.

set -uo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
LOG_FILE="${PROJECT_DIR}/state/provision-install.log"
sudo mkdir -p "$(dirname "${LOG_FILE}")"
sudo touch "${LOG_FILE}"
sudo chown "$(id -u):$(id -g)" "${LOG_FILE}" 2>/dev/null || true

# PIPESTATUS[0] (Exit-Code von provision.sh, nicht von tee/grep/sed am
# Ende der Pipe) entscheidet ueber Erfolg/Fehler am Schluss.
set -o pipefail

sudo bash "${PROJECT_DIR}/provision/provision.sh" 2>&1 \
  | tee -a "${LOG_FILE}" \
  | grep --line-buffered '^PROGRESS:' \
  | sed -u -E 's/^PROGRESS:([0-9]+):(.*)$/\1\n# \2/' \
  | zenity --progress \
      --title="IRL Streamer OS wird eingerichtet" \
      --text="Einrichtung wird gestartet..." \
      --percentage=0 \
      --auto-close \
      --width=420

PROVISION_EXIT="${PIPESTATUS[0]}"

if [ "${PROVISION_EXIT}" -eq 0 ]; then
  zenity --info --title="IRL Streamer OS" \
    --text="Einrichtung abgeschlossen.\n\nDas vollstaendige Protokoll liegt unter:\n${LOG_FILE}" \
    --width=420 2>/dev/null || true
else
  zenity --error --title="IRL Streamer OS - Fehler" \
    --text="Bei der Einrichtung ist ein Fehler aufgetreten (Exit-Code ${PROVISION_EXIT}).\n\nDetails im Protokoll:\n${LOG_FILE}\n\nDas Icon 'IRL Streamer OS einrichten' kann gefahrlos erneut angeklickt werden (die Einrichtung ist wiederholbar)." \
    --width=460 2>/dev/null || true
fi

exit "${PROVISION_EXIT}"
