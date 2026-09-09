#!/usr/bin/env bash
# IRL Streamer OS - "Ist gerade ein Stream aktiv?"-Check fuer den
# Connectivity-Report-Timer (Task 4, Entscheidung 4 im Connectivity-Ampel-
# Plan: "stuendlich ist gut, wenn das keine Performance-Einbussen fuer
# einen Stream bedeutet").
#
# Wird als ExecCondition im begleitenden .service verwendet (siehe
# irl-connectivity-report.service) - liefert Exit-Code 0, wenn der stuendliche
# Connectivity-Check JETZT laufen darf (kein aktiver Stream), und einen
# Nicht-Null-Code, wenn er uebersprungen werden soll. systemd wertet einen
# ExecCondition-Fehlschlag NICHT als Service-Fehler, sondern beendet den
# Lauf einfach lautlos - der naechste stuendliche Timer-Tick reicht, es muss
# nichts nachgeholt werden.
#
# Erkennungsmethode: srtla_rec (der SRTLA-Empfaenger, laeuft lokal im
# belabox-receiver-Container auf Port 5000/udp, siehe testbed/irl-
# diagnostics-src/main.py BELABOX_CONTAINER_NAME) empfaengt nur waehrend
# eines aktiven Streams tatsaechlich Pakete - der Container selbst laeuft
# hingegen IMMER (auch ohne aktiven Stream), daher reicht ein reiner
# "laeuft der Prozess"-Check nicht aus. Es wird stattdessen die RX-Queue-
# Groesse des UDP-Sockets (Spalte 5 in /proc/net/udp, Format
# "tx_queue:rx_queue" in Hex) an zwei Zeitpunkten verglichen - ist sie an
# BEIDEN Zeitpunkten 0, kommt aktuell nichts an; jede von 0 verschiedene
# Messung gilt als "Traffic vorhanden" (konservativ, vermeidet Positive
# durch reine Momentaufnahme-Zufaelligkeit einzelner Nullwerte).
set -uo pipefail

BELABOX_CONTAINER_NAME="${BELABOX_CONTAINER_NAME:-belabox-receiver}"
SRTLA_PORT="${SRTLA_PORT:-5000}"
SAMPLE_INTERVAL_S="${SAMPLE_INTERVAL_S:-2}"

get_rx_queue_nonzero() {
    # Prueft, ob die RX-Queue des UDP-Sockets auf SRTLA_PORT gerade
    # ungleich 0 ist (= es liegen unverarbeitete eingehende Pakete an) -
    # docker exec statt lokalem ss, da srtla_rec im Container laeuft.
    docker exec "${BELABOX_CONTAINER_NAME}" sh -c \
        "awk -v port=\$(printf '%04X' ${SRTLA_PORT}) '\$2 ~ (\":\"port\"\$\") { split(\$5, q, \":\"); if (strtonum(\"0x\"q[2]) > 0) print \"1\"; else print \"0\" }' /proc/net/udp 2>/dev/null | head -1" \
        2>/dev/null || echo "0"
}

SAMPLE_1="$(get_rx_queue_nonzero)"
sleep "${SAMPLE_INTERVAL_S}"
SAMPLE_2="$(get_rx_queue_nonzero)"

if [ "${SAMPLE_1}" = "1" ] || [ "${SAMPLE_2}" = "1" ]; then
    echo "[irl-stream-active-check] Eingehender SRTLA-Traffic erkannt (RX-Queue nicht leer) - Connectivity-Check wird uebersprungen."
    exit 1
fi

echo "[irl-stream-active-check] Kein aktiver Stream erkannt - Connectivity-Check darf laufen."
exit 0
