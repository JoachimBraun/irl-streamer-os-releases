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

# --- SRT-Bibliothek nur EINMAL laden (Review 11.11.2026, Block 3) ---
# Absturz bei JEDEM OBS-Ende (per Core-Datei auf der Test-VM belegt): das offizielle
# OBS-.deb fuer Ubuntu 26.04 laedt die SRT-Bibliothek zweimal. obs-ffmpeg.so verlangt
# libsrt.so.1.5 (OpenSSL-Variante), libavformat (ffmpeg, auch fuer die IRL SRT Quelle)
# und obs-outputs.so verlangen libsrt-gnutls.so.1.5. Beide Kopien exportieren dieselben
# C++-Symbole, beim Programmende baut deshalb jede Kopie dasselbe globale Objekt ab
# (srt::PacketFilter-Fabrikliste) -> doppelte Freigabe -> "corrupted size vs. prev_size"
# bzw. SEGV/TRAP, erst NACH dem sauberen Speichern. Ohne Eingriff in OBS: fuer diesen
# Start zeigt der Name libsrt.so.<ver> auf die GnuTLS-Datei, damit nur EINE Kopie im
# Prozess ist (gleiche SRT-Version und C-Schnittstelle; die OBS-eigene SRT-Ausgabe ist
# damit getestet). Passt sich selbst an: fehlt eine der beiden Dateien oder aendert sich
# die Version, entsteht kein Link und OBS startet wie bisher. Nur im privaten
# XDG_RUNTIME_DIR (nie /tmp - dort koennte ein anderer Nutzer Bibliotheken unterschieben).
SRT_LIBDIR="${IRL_SRT_LIBDIR:-/usr/lib/x86_64-linux-gnu}"
if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "${XDG_RUNTIME_DIR}" ] && [ -O "${XDG_RUNTIME_DIR}" ]; then
  SRT_EINHEIT="${XDG_RUNTIME_DIR}/irl-srt-einheit"
  rm -rf "${SRT_EINHEIT}" 2>/dev/null
  if mkdir -m 0700 "${SRT_EINHEIT}" 2>/dev/null; then
    for g in "${SRT_LIBDIR}"/libsrt-gnutls.so.*.*; do
      ver="${g##*/libsrt-gnutls.so.}"
      case "${ver}" in *.*.*|'*.*') continue ;; esac
      if [ -e "${g}" ] && [ -e "${SRT_LIBDIR}/libsrt.so.${ver}" ]; then
        ln -s "${g}" "${SRT_EINHEIT}/libsrt.so.${ver}"
      fi
    done
    if compgen -G "${SRT_EINHEIT}/libsrt.so.*" >/dev/null; then
      export LD_LIBRARY_PATH="${SRT_EINHEIT}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
    fi
  fi
fi

# --- Nach Absturz waehrend einer Sendung automatisch weitersenden (Nutzerentscheidung 12.11.2026) ---
# Nur wenn BEIDES zutrifft:
#  1. systemd startet OBS gerade automatisch nach einem Absturz neu (Restart=on-failure,
#     NRestarts >= 1). Nie beim Login, nach einem Neustart/Stromausfall des Geraets oder
#     per Dashboard-Knopf "OBS starten" (dort ist NRestarts 0) - sonst ginge das Geraet
#     z.B. Tage spaeter beim Einschalten ungefragt live.
#  2. Das OBS-Log der abgestuerzten Sitzung (juenger als 2 min) zeigt eine laufende Sendung:
#     die letzte "Streaming Start/Stop"-Zeile ist ein Start. OBS schreibt das Log zeilenweise,
#     es ist also bis zum Absturz vollstaendig; ein normales Senden-Beenden steht als Stop drin.
# Wiederholte Abstuerze begrenzt die Unit (hoechstens 3 Starts in 10 min).
OBS_EXTRA_ARGS=()
n_restarts="$(systemctl --user show -p NRestarts --value irl-streamer-obs.service 2>/dev/null)"
case "${n_restarts}" in ''|*[!0-9]*) n_restarts=0 ;; esac
if [ "${n_restarts}" -ge 1 ]; then
  last_log="$(find "${HOME}/.config/obs-studio/logs" -maxdepth 1 -name '*.txt' -mmin -2 -printf '%T@ %p\n' 2>/dev/null \
    | sort -n | tail -1 | cut -d' ' -f2-)"
  if [ -n "${last_log}" ] && grep -E '^[0-9:.]+: ==== Streaming (Start|Stop) ' "${last_log}" 2>/dev/null \
       | tail -1 | grep -q 'Streaming Start'; then
    OBS_EXTRA_ARGS+=(--startstreaming)
    logger -t irl-streamer-obs "OBS nach Absturz neu gestartet (Neustart ${n_restarts}) - Sendung lief, sende automatisch weiter"
  fi
fi

export QT_QPA_PLATFORM=xcb
exec obs --collection "IRL-Streamer" --profile "IRL-Streamer" --scene "LIVE" "${OBS_EXTRA_ARGS[@]}"
