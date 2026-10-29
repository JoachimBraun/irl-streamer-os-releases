#!/usr/bin/env bash
# Lizenz-/Testphasen-Sperre pruefen, BEVOR OBS ueberhaupt startet - siehe
# provision/licensing/license-guard.sh. Reine Datei-Existenz-Pruefung
# (schnell, kein Netzwerk), die eigentliche Signaturpruefung + Sperr-
# Entscheidung trifft der taegliche systemd-Timer (irl-sysmaint-check.service).
#
# BUGFIX (05.09., live gefunden - PermissionError beim OBS-Autostart):
# license-guard.sh ruft license-locate.py auf, das den absichtlich
# root-only geschuetzten Lizenz-Tresor (/opt/.intel-mediasdk-cache,
# chmod 700 root:root, siehe license-vault-init.sh) lesen muss. Dieser
# Wrapper hier laeuft aber als normaler User (Autostart-Desktop-Datei) -
# ohne sudo crasht der Resolver mit Errno 13 und blockiert damit JEDEN
# OBS-Start komplett. sudo per dediziertem NOPASSWD-Eintrag
# (/etc/sudoers.d/irl-streamer-license-guard, siehe weiter unten in
# diesem Skript) behebt das, ohne den Tresor selbst lesbar machen zu
# muessen.
# Review 29.10.2026 (B7): nur Exit 10 (= wirklich gesperrt) verhindert den Start.
# Jeder andere Fehler (fehlender sudo-Eintrag, Tresor nicht lesbar) wird geloggt,
# OBS startet trotzdem - die Sperre selbst setzt der Lizenz-Timer durch.
sudo -n bash /opt/irl-streamer-os/provision/licensing/license-guard.sh
GUARD_RC=$?
if [ "${GUARD_RC}" -eq 10 ]; then
  exit 0
fi
if [ "${GUARD_RC}" -ne 0 ]; then
  logger -t irl-streamer-obs "Lizenz-Pruefung beim OBS-Start technisch fehlgeschlagen (rc=${GUARD_RC}) - OBS startet trotzdem"
fi

# "Abgesicherter Modus"-Dialog IMMER unterdruecken (Nutzerwunsch 03.09.):
# OBS erkennt einen unsauberen vorherigen Exit ueber eine reine Sentinel-
# Datei (~/.config/obs-studio/.sentinel/run_<uuid>), die beim Start
# angelegt und beim SAUBEREN Beenden wieder geloescht wird - liegt sie
# noch vor, fragt OBS beim naechsten Start nach Normal-/Abgesichertem
# Modus. Das frueher existierende "--disable-shutdown-check"-Flag wurde
# von OBS-Upstream in 32.0.0 komplett entfernt (kein Ersatz-Flag mehr
# verfuegbar, siehe "obs --help") - der zuverlaessige, von der OBS-
# Community selbst empfohlene Weg ist stattdessen, die Sentinel-Datei(en)
# VOR jedem Start selbst zu entfernen. Das ist eine reine Kosmetik-
# Massnahme (unterdrueckt nur den Dialog) - die eigentliche Ursache fuer
# unsaubere Shutdowns wird weiterhin durch den separaten Shutdown-
# Inhibitor-Mechanismus bekaempft (siehe irl-streamer-shutdown-inhibitor.py),
# diese Zeile hier ist bewusst die zusaetzliche Ausfallsicherung fuer
# alle Faelle, die jener Mechanismus (noch) nicht abdeckt.
rm -f "${HOME}/.config/obs-studio/.sentinel/run_"* 2>/dev/null

# IRL SRT Quelle: ist das Plugin (z.B. nach einem OBS-/Ubuntu-Update) nicht
# ladbar, die Szene vor dem Start auf die Standard-Medienquelle umstellen -
# und zurueck, sobald es wieder geht. Nie fatal fuer den OBS-Start.
# Gilt fuer ALLE Szenen-Sammlungen - auch selbst angelegte SRT-Medienquellen
# werden so immer zur IRL SRT Quelle.
python3 /opt/irl-streamer-os/provision/lib/irl-srt-source-select.py \
  "${HOME}/.config/obs-studio/basic/scenes/"*.json >/dev/null 2>&1 || true

export QT_QPA_PLATFORM=xcb
exec obs --collection "IRL-Streamer" --profile "IRL-Streamer" --scene "LIVE"
