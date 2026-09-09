#!/usr/bin/env bash
# IRL Streamer OS - Erstboot-Provisioning (Phase 1)
#
# Laeuft einmalig per systemd-oneshot (irl-streamer-provision.service) beim
# ersten echten Boot, NICHT im Curtin-Chroot waehrend der Installation - das
# ist bewusst so, weil hier auf einen fertig existierenden Benutzer-Home,
# gdm3 und einen laufenden Docker-Daemon aufgebaut wird.
#
# Idempotent: kann bei Bedarf manuell erneut ausgefuehrt werden (z.B. beim
# Live-Testen auf der Unraid-Test-VM), ohne kaputtzugehen - alle Schritte
# ueberschreiben ihre Zieldateien bzw. sind von Haus aus wiederholbar
# (docker compose up, apt-get install, mkdir -p, ...).

set -euo pipefail

TARGET_USER="streamer"
HOME_DIR="/home/${TARGET_USER}"
PROJECT_DIR="/opt/irl-streamer-os"
STATE_DIR="${PROJECT_DIR}/state"
LOG_PREFIX="[irl-streamer-provision]"

log() { echo "${LOG_PREFIX} $*"; }

# Fortschritts-Marker fuer den grafischen Zenity-Fortschrittsbalken
# (siehe IRL-Streamer-OS-einrichten.desktop / provision-wrapper.sh). Gibt
# eine Zeile im festen Format "PROGRESS:<prozent>:<beschreibung>" aus, die
# der Wrapper per Pipe abfaengt und an "zenity --progress" durchreicht.
# Laeuft provision.sh mal OHNE den Wrapper (z.B. manuell im Terminal beim
# Debuggen), sind diese Zeilen einfach normale, harmlose stdout-Ausgaben -
# kein Sonderfall noetig.
TOTAL_PROGRESS_STEPS=12
progress() {
  local step="$1" desc="$2"
  local percent=$(( step * 100 / TOTAL_PROGRESS_STEPS ))
  echo "PROGRESS:${percent}:${desc}"
}

# Generiert ein zufaelliges, gut lesbares Passwort OHNE zweideutige Zeichen
# (Nutzerwunsch 2026-08-31): 0/O, 1/l/I und aehnlich verwechselbare Zeichen
# faellen weg - relevant, weil Passwoerter oft von Hand abgetippt werden
# (z.B. Login am OBS-Rechner ohne Copy-Paste-Zugriff auf die
# Zugangsdaten.txt). Alphabet bewusst gross gehalten (52 Zeichen) trotz der
# Streichungen, damit die Gesamtentropie bei Laenge 20 weiterhin hoch bleibt
# (~114 Bit, deutlich mehr als openssl rand -base64 18 mit ~108 Bit brutto,
# aber dort ohne Zeichenausschluss). NICHT fuer WireGuard-Schluessel
# verwenden - das sind kryptografische 256-Bit-Keys (wg genkey), die ihr
# volles Base64-Alphabet fuer die Sicherheit brauchen; dort hilft nur
# konsequentes Copy-Paste statt Abtippen.
generate_readable_password() {
  local length="${1:-20}"
  # Ausgeschlossen: 0 O o 1 l I (Ziffer/Grossbuchstabe/Kleinbuchstabe je
  # verwechselbares Zeichen), zusaetzlich 5/S und 2/Z als grenzwertige Faelle
  # in manchen Schriftarten bewusst ebenfalls raus.
  local charset="ABCDEFGHJKLMNPQRTUVWXYabcdefghijkmnpqrtuvwxy346789"
  # WICHTIG (Nutzerfehler 2026-08-31, live reproduziert per bash -x-Trace):
  # "tr ... | head -c N" beendet head, sobald N Bytes gelesen sind - tr
  # bekommt dabei ein SIGPIPE, weil seine Ausgabe-Pipe geschlossen wird, und
  # beendet sich mit einem Fehler-Exit-Code (kein Bug bei tr, ganz normales
  # Unix-Pipe-Verhalten). Mit "set -o pipefail" (siehe Skriptkopf) wertet
  # Bash die GESAMTE Pipe als fehlgeschlagen, "set -e" beendet daraufhin
  # STILL das komplette provision.sh, ohne jede sichtbare Fehlermeldung -
  # live reproduziert: das Skript brach dadurch nach dem Guacamole-
  # Passwort, aber VOR dem RDP-Passwort/Desktop-Icons ab. "|| true" faengt
  # genau dieses erwartete SIGPIPE ab, ohne echte tr-Fehler zu verschlucken
  # (bei einem echten tr-Fehler waere die Ausgabe schlicht leer/zu kurz,
  # das faellt beim Verwenden des Passworts sofort auf).
  LC_ALL=C tr -dc "${charset}" < /dev/urandom | head -c "${length}" || true
}

mkdir -p "${STATE_DIR}"

# --- -1. Eindeutigen Hostnamen setzen (Basisname + letzte 4 Hex-Stellen der
#         MAC-Adresse) - Autoinstall selbst vergibt statisch "irl-streamer-os"
#         fuer JEDE Installation; sobald mehrere Geraete gleichzeitig im
#         selben Netz installiert werden (z.B. Test-VM + echter Mini-PC
#         parallel, 2026-08-24), kollidieren DHCP-Lease-Liste und mDNS
#         (".local") sonst. Ueber die primaere Default-Route-Schnittstelle
#         ermittelt, mit Fallback auf das erste Nicht-Loopback-Interface,
#         falls beim allerersten Boot noch keine Default-Route existiert.
IFACE="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
if [ -z "${IFACE}" ]; then
  IFACE="$(ls /sys/class/net | grep -v '^lo$' | head -1)"
fi
MAC_SUFFIX="$(cat "/sys/class/net/${IFACE}/address" 2>/dev/null | tr -d ':' | tail -c 5)"
if [ -n "${MAC_SUFFIX}" ]; then
  NEW_HOSTNAME="irl-streamer-os-${MAC_SUFFIX}"
  log "Setze eindeutigen Hostnamen: ${NEW_HOSTNAME} (aus MAC von ${IFACE})"
  hostnamectl set-hostname "${NEW_HOSTNAME}"
  if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t${NEW_HOSTNAME}/" /etc/hosts
  else
    echo -e "127.0.1.1\t${NEW_HOSTNAME}" >> /etc/hosts
  fi
else
  log "WARNUNG: konnte keine MAC-Adresse ermitteln - Hostname bleibt beim Autoinstall-Standard"
fi

# --- 0. Vollstaendiges Upgrade (nicht nur die Sicherheitsupdates, die
#        Autoinstall waehrend der Installation selbst schon zieht) - auf
#        Nutzerwunsch, damit ein ISO, das evtl. erst Wochen/Monate nach dem
#        Bauen tatsaechlich installiert wird, trotzdem mit aktuellem
#        Paketstand startet (z.B. relevant fuer den OBS/Twitch-Bug, siehe
#        Chatverlauf 2026-08-24 - koennte durch ein OBS-Update behoben sein).
log "Fuehre vollstaendiges apt update+upgrade aus"
progress 1 "System wird aktualisiert..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y -o Dpkg::Options::="--force-confold" upgrade

# Basis-Werkzeuge, die dieses Skript selbst braucht - NICHT einfach als
# vorhanden annehmen: live verifiziert (2026-08-24), das offizielle Ubuntu-
# Desktop-ISO (manuelle Installation, anders als unser fruehereres Server-
# Autoinstall mit eigener Paketliste) bringt z.B. kein "curl" mit. Macht
# dieses Skript unabhaengig davon, wie das Grundsystem installiert wurde.
#
# wireguard-tools (liefert wg/wg-quick) gehoert HIER dazu, nicht nur bedingt
# im Fernzugriff-Skript (irl-streamer-fernzugriff-einrichten.sh) - Bug
# gefunden 03.09. bei einer komplett frischen Installation: der automatische
# CGNAT-Fallback (irl-connectivity-report-client.sh, laeuft stuendlich per
# systemd-Timer OHNE dass der Nutzer je "Fernzugriff einrichten" klicken
# muss) versuchte, den wg-relay-Tunnel per "systemctl restart
# wg-quick@wg-relay" zu starten - das Skript existiert aber erst, NACHDEM
# das wireguard-tools-Paket (liefert die wg-quick@.service-Unit) installiert
# wurde. Auf einer aelteren Installation, auf der der Nutzer den manuellen
# Fernzugriff schon mal eingerichtet hatte, fiel das nie auf (wireguard war
# dort laengst installiert) - erst bei einer komplett frischen ISO wurde der
# Relay-Tunnel dadurch NIE aufgebaut (0 WireGuard-Handshakes serverseitig,
# Caddy meldete "no route to host" fuer die Kunden-Subdomain).
log "Installiere Basis-Werkzeuge (curl, jq, git, openssh-client, whiptail, gnupg, net-tools, wireguard-tools)"
apt-get install -y curl jq git openssh-client whiptail gnupg net-tools wireguard-tools

# --- 0b. Lizenz-/Testphasen-Verwaltung starten ------------------------------
# Meldet dieses Geraet EINMALIG beim Lizenzserver an und startet damit die
# 30-Tage-Testphase - so frueh wie moeglich im Skript platziert (nur nach den
# Basis-Werkzeugen oben, curl/python3 werden gebraucht), damit die Testzeit
# nicht durch eine lange OBS-/Docker-Installation "verschwendet" wird, bevor
# der Nutzer das System ueberhaupt zum ersten Mal richtig nutzen kann.
#
# python3-cryptography wird HIER installiert (nicht erst spaeter), weil
# license-check.py (taeglicher Warn-/Sperr-Timer, siehe systemd-Unit weiter
# unten) diese Bibliothek fuer die Ed25519-Signaturpruefung braucht.
#
# Netzwerkausfall bei der Ersteinrichtung ist kein Show-Stopper: schlaegt der
# Trial-Start fehl (siehe license-client.sh trial-start), laeuft die
# Provisionierung trotzdem weiter - der taegliche Timer versucht es erneut,
# bis zum ersten erfolgreichen Kontakt gibt es lediglich noch KEINE aktive
# Sperr-/Warnlogik (kein Lizenzstatus vorhanden = wird von den Check-Stellen
# als "noch kein Trial gestartet", nicht als "abgelaufen" behandelt).
log "Installiere python3-cryptography (fuer Lizenz-Signaturpruefung)"
apt-get install -y python3-cryptography

# Lizenz-Tresor initialisieren (Haertung 2026-09-05, siehe
# license-vault-init.sh) - MUSS vor dem allerersten trial-start laufen,
# damit license-client.sh von Anfang an ueber den Tresor-Resolver auf
# die Lizenzdatei zugreift, statt sie noch kurz am alten, offensichtlichen
# Pfad (state/license.json) anzulegen und erst danach zu verschieben.
# Idempotent (siehe dortiger Check) - ein erneuter provision.sh-Lauf auf
# einem bereits initialisierten Geraet tut hier nichts.
log "Initialisiere Lizenz-Tresor"
bash "${PROJECT_DIR}/provision/licensing/license-vault-init.sh"

log "Starte Testphase beim Lizenzserver (falls noch nicht geschehen)"
progress 2 "Lizenz-/Testphase wird eingerichtet..."
bash "${PROJECT_DIR}/provision/licensing/license-client.sh" trial-start \
  || log "WARNUNG: Testphase konnte nicht gestartet werden (siehe Meldung oben) - taeglicher Timer versucht es spaeter erneut."

# SSH-Host-Keys neu erzeugen, falls sie fehlen - relevant fuer das Live-ISO
# aus einem geklonten Golden-Image (Phase B, 2026-08-24): dort werden die
# Host-Keys der Golden-Maschine bewusst vor dem Capture geloescht, damit
# nicht jede daraus deployte Maschine dieselben (unsicheren, weil geklonten)
# SSH-Host-Keys teilt. Bei einer normalen Neuinstallation per Ubuntu-Installer
# erzeugt Ubuntu die Keys zwar schon selbst frisch, dieser Schritt schadet
# dort aber nicht (idempotent, ueberspringt sich selbst wenn Keys bereits da
# sind) und macht das Skript unabhaengig davon, ob ein zuverlaessiger
# automatischer Mechanismus dafuer vorhanden ist.
if [ ! -s /etc/ssh/ssh_host_rsa_key ]; then
  log "Erzeuge fehlende SSH-Host-Keys neu"
  ssh-keygen -A
fi

# Docker CE + Chrome: bei der urspruenglichen Server-Autoinstall liefen diese
# Repo-/Paket-Installationen ueber die autoinstall-user-data (apt.sources +
# packages), die bei einer manuellen Desktop-Installation (aktueller Weg,
# 2026-08-24) gar nicht existiert - live gefunden: ohne diesen Block bleibt
# "docker" schlicht nicht installiert. Volle Schluessel direkt von der
# offiziellen Quelle (nicht per Keyserver-keyid) - siehe Chatverlauf zum
# veralteten-Schluessel-Crash beim frueheren Autoinstall-Ansatz, derselbe
# Grund gilt hier genauso.
# Idempotenz-Checks (command -v) UND </dev/null bei gpg: live gefunden
# (2026-08-24), ein erneuter Skriptlauf brach hier mit "gpg: cannot open
# '/dev/tty'" ab, obwohl --dearmor rein nicht-interaktiv ist - vermutlich
# ein gpg-agent-Eigenheit je nach SSH-Sitzungskontext. Explizites
# stdin-/dev/null vermeidet das zuverlaessig, die command-v-Pruefung spart
# ausserdem den unnoetigen Wiederholungsaufwand.
# Datei statt direkter Pipe + Wiederholungsversuche: live beobachtet
# (2026-08-24, Mini-PC), ein kurzer Netzwerk-Aussetzer beim Download liess
# gpg mit "Keine gueltigen OpenPGP-Daten gefunden" abbrechen, weil eine
# unterbrochene Pipe unbemerkt nur Teildaten durchreicht. Mit Zwischendatei
# ist die Downloadgroesse pruefbar, bevor gpg ueberhaupt startet.
fetch_key_with_retry() {
  local url="$1" out="$2" attempt
  for attempt in 1 2 3; do
    if curl -fsSL "${url}" -o "${out}.tmp" && [ -s "${out}.tmp" ]; then
      mv "${out}.tmp" "${out}"
      return 0
    fi
    log "Download von ${url} fehlgeschlagen (Versuch ${attempt}/3), versuche erneut"
    sleep 2
  done
  return 1
}

install -m 0755 -d /etc/apt/keyrings

if ! command -v docker >/dev/null 2>&1; then
  log "Richte Docker-CE-Repo ein und installiere Docker"
  fetch_key_with_retry https://download.docker.com/linux/ubuntu/gpg /tmp/docker-key.pub
  gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg < /tmp/docker-key.pub
  rm -f /tmp/docker-key.pub
  chmod a+r /etc/apt/keyrings/docker.gpg
  UBUNTU_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
else
  log "Docker bereits installiert - ueberspringe Repo-Einrichtung"
fi

if ! command -v google-chrome-stable >/dev/null 2>&1; then
  log "Richte Chrome-Repo ein und installiere Google Chrome"
  fetch_key_with_retry https://dl.google.com/linux/linux_signing_key.pub /tmp/chrome-key.pub
  gpg --batch --yes --dearmor -o /etc/apt/keyrings/google-chrome.gpg < /tmp/chrome-key.pub
  rm -f /tmp/chrome-key.pub
  chmod a+r /etc/apt/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update
  apt-get install -y google-chrome-stable
else
  log "Chrome bereits installiert - ueberspringe Repo-Einrichtung"
fi

# Web Bluetooth auf Linux dauerhaft aktivieren (Nutzerwunsch 2026-08-31,
# live geloest): auf Linux ist Web Bluetooth in Chrome NICHT standardmaessig
# aktiv, unabhaengig von HTTPS - "navigator.bluetooth" bleibt trotz
# korrektem HTTPS-Kontext false/undefined, bis das Flag
# chrome://flags/#enable-experimental-web-platform-features manuell auf
# "Enabled" gesetzt wird (offiziell in der Chrome-Entwicklerdoku als
# Linux-Sonderfall dokumentiert). Das ist kein Bug in diesem Projekt,
# sondern generelles Chrome-auf-Linux-Verhalten. Statt den Nutzer bei jeder
# Neuinstallation manuell durch chrome://flags klicken zu lassen: das echte
# Chrome-Binary durch einen Wrapper ersetzen, der das Flag automatisch
# mitgibt - wirkt dadurch fuer JEDEN Aufrufweg (Desktop-Icon, xdg-open,
# direkter Terminalaufruf), nicht nur fuer eine einzelne Verknuepfung.
# Idempotent (Check auf .real-Suffix), schadet bei erneutem Lauf nicht.
CHROME_BIN="/usr/bin/google-chrome-stable"
CHROME_REAL_BIN="/usr/bin/google-chrome-stable.real"
if [ -f "${CHROME_BIN}" ] && [ ! -f "${CHROME_REAL_BIN}" ]; then
  log "Aktiviere Web Bluetooth dauerhaft (Chrome-Wrapper mit experimental-web-platform-features)"
  mv "${CHROME_BIN}" "${CHROME_REAL_BIN}"
  cat > "${CHROME_BIN}" <<'EOF'
#!/usr/bin/env bash
exec /usr/bin/google-chrome-stable.real --enable-experimental-web-platform-features "$@"
EOF
  chmod +x "${CHROME_BIN}"
fi

# Chrome als System-Standardbrowser setzen (xdg-settings, wirkt fuer
# xdg-open/alle Desktop-Icons in diesem Projekt) UND Chromes eigene
# "Als Standardbrowser festlegen?"-Nachfrage beim ersten Start unterdruecken.
#
# BUGFIX (05.09., live gefunden): DefaultBrowserSettingEnabled=true war
# GENAU FALSCH HERUM gesetzt - laut offizieller Chrome-Enterprise-Doku
# (chromeenterprise.google/policies/?policy=DefaultBrowserSettingEnabled)
# bedeutet "true": Chrome prueft AKTIV bei jedem Start, ob es Standard ist,
# und registriert sich ggf. automatisch NEU - inklusive der sichtbaren
# Nachfrage, wenn eine automatische Registrierung (wie hier unter Linux
# ueber xdg-settings) nicht ohne Nutzerinteraktion moeglich ist. "false"
# ist der dokumentierte Weg, die gesamte Standardbrowser-Pruefung UND
# jede Nachfrage danach komplett abzuschalten.
sudo -u "${TARGET_USER}" xdg-settings set default-web-browser google-chrome.desktop 2>/dev/null || true
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/irl-streamer-os-defaults.json <<'EOF'
{
  "DefaultBrowserSettingEnabled": false
}
EOF


# OBS Studio: direktes .deb vom offiziellen GitHub-Release statt PPA.
# Nutzerentscheidung 03.09.: das PPA (ppa:obsproject/obs-studio) fuehrt
# fuer den Ubuntu-Codename "resolute" (26.04) nur 32.2.0 - Ubuntus/das
# PPA-Buildsystem hinkt dem eigentlichen OBS-Release-Zyklus nach, obwohl
# OBS-Upstream schon bei 32.2.2 ist. Live gefunden (03.09.): 32.2.0 hat
# einen reproduzierbaren Absturz (Segfault im "hotkey"-Thread von
# libobs.so.30) beim SIGTERM-basierten Herunterfahren, der zum
# "Abgesicherter Modus"-Dialog beim naechsten Start fuehrt. Der offizielle
# 32.2-Changelog listet explizit "Fixed some erroneous crashes during
# shutdown" - live auf 192.168.10.223 bestaetigt: nach Umstieg auf
# 32.2.2 kein "Crash or unclean shutdown detected" mehr im Log.
#
# Direktes .deb statt PPA, weil GitHub-Releases bereits fertige
# Ubuntu-26.04-Pakete mit der neuesten Version anbieten (PPA-Build-
# Verzoegerung umgangen) - mit SHA256-Verifikation gegen den offiziellen,
# im Code fest hinterlegten Wert (aus der GitHub-Release-Seite, nicht
# vom Server zur Laufzeit abgefragt - Verifikation waere sonst wertlos).
# Bei Fehlschlag (Download/Checksumme/Installation) Fallback aufs PPA
# (siehe unten) statt komplett zu scheitern - "set -e" wuerde sonst die
# GESAMTE Provisionierung abbrechen (analoges Muster zum bisherigen
# GPG-Key-Retry unten).
log "Installiere OBS Studio direkt vom offiziellen GitHub-Release (32.2.2)"
OBS_DEB_URL="https://github.com/obsproject/obs-studio/releases/download/32.2.2/OBS-Studio-32.2.2-Ubuntu-26.04-x86_64.deb"
OBS_DEB_SHA256="f256927aeba7b8d2ce64815402e723d1dd8332d1e6535939b03572f2adcc2849"
OBS_DEB_PATH="/tmp/obs-studio-32.2.2.deb"
OBS_INSTALLED_VIA_DEB=false
if curl -fsSL "${OBS_DEB_URL}" -o "${OBS_DEB_PATH}" 2>/dev/null; then
  ACTUAL_SHA256="$(sha256sum "${OBS_DEB_PATH}" | awk '{print $1}')"
  if [ "${ACTUAL_SHA256}" = "${OBS_DEB_SHA256}" ]; then
    if apt-get install -y "${OBS_DEB_PATH}"; then
      OBS_INSTALLED_VIA_DEB=true
    else
      log "WARNUNG: Installation des OBS-.deb fehlgeschlagen - falle zurueck auf PPA"
    fi
  else
    log "WARNUNG: SHA256-Pruefsumme des OBS-.deb stimmt nicht (erwartet ${OBS_DEB_SHA256}, erhalten ${ACTUAL_SHA256}) - falle zurueck auf PPA"
  fi
else
  log "WARNUNG: OBS-.deb-Download fehlgeschlagen - falle zurueck auf PPA"
fi
rm -f "${OBS_DEB_PATH}"

if [ "${OBS_INSTALLED_VIA_DEB}" != true ]; then
  log "Richte OBS-PPA ein und installiere/aktualisiere OBS Studio (Fallback)"
  OBS_KEYRING="/usr/share/keyrings/obs-studio-archive-keyring.gpg"
  OBS_KEY_FPR="BC7345F522079769F5BBE987EFC71127F425E228"
  if [ ! -s "${OBS_KEYRING}" ]; then
    for i in $(seq 1 5); do
      if curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${OBS_KEY_FPR}" 2>/dev/null \
           | gpg --dearmor > "${OBS_KEYRING}.tmp" 2>/dev/null \
         && [ -s "${OBS_KEYRING}.tmp" ]; then
        mv "${OBS_KEYRING}.tmp" "${OBS_KEYRING}"
        break
      fi
      rm -f "${OBS_KEYRING}.tmp"
      log "OBS-PPA-Schluessel noch nicht abrufbar (Versuch ${i}/5), warte 5s..."
      sleep 5
    done
  fi
  if [ ! -s "${OBS_KEYRING}" ]; then
    log "WARNUNG: OBS-PPA-Schluessel nicht abrufbar - OBS-Studio-Installation wird uebersprungen, Rest laeuft weiter. Dieses Skript spaeter erneut ausfuehren, um OBS nachzuinstallieren."
  else
    cat > /etc/apt/sources.list.d/obs-studio.list <<EOF
deb [signed-by=${OBS_KEYRING}] https://ppa.launchpadcontent.net/obsproject/obs-studio/ubuntu $(lsb_release -sc) main
EOF
    apt-get update
    apt-get install -y obs-studio
  fi
fi

# --- GPU-Erkennung + Hardware-Encoding-Backend ------------------------------
# Live festgestellt (2026-08-24, Mini-PC/Intel Alder Lake-N): libva* (das
# VAAPI-Laufzeit-Framework) ist zwar Teil des Ubuntu-Grundsystems, liefert
# aber OHNE ein tatsaechliches Treiber-Backend gar keinen einzigen Encoder -
# OBS zeigt dann fuer Quick Sync/VAAPI ueberhaupt keine Auswahl an, nicht
# weil die GPU fehlt, sondern weil das Backend fehlt. Erkennung ueber die
# PCI-Vendor-ID (0x8086=Intel, 0x1002=AMD, 0x10de=Nvidia) statt Modellnamen-
# Matching, damit das auf jedem kuenftigen Mini-PC automatisch das Richtige
# installiert - genau das vom Nutzer gewuenschte "eine ISO fuer verschiedene
# Hardware", ohne dass ein falscher Treiber (z.B. Nvidia auf einer AMD-Kiste)
# erzwungen wird. apt-get install ist von Natur aus idempotent (bereits
# installiert = No-op), daher kein zusaetzlicher command-v-Guard noetig.
log "Erkenne verbaute GPU(s) und installiere passendes Hardware-Encoding-Backend"
GPU_VENDOR_IDS="$(lspci -nnmm 2>/dev/null | awk -F'"' '$2 ~ /VGA compatible controller|3D controller|Display controller/ {print $4}' | grep -oP '(?<=\[)[0-9a-f]{4}(?=\])' | sort -u || true)"
for VID in ${GPU_VENDOR_IDS}; do
  case "${VID}" in
    8086)
      log "Intel-GPU erkannt (${VID}) - installiere VAAPI-Backend fuer Quick Sync"
      apt-get install -y intel-media-va-driver-non-free vainfo
      ;;
    1002)
      log "AMD-GPU erkannt (${VID}) - installiere Mesa-VAAPI-Backend"
      apt-get install -y mesa-va-drivers vainfo
      ;;
    10de)
      log "Nvidia-GPU erkannt (${VID}) - installiere proprietaeren Treiber (fuer NVENC) via ubuntu-drivers"
      apt-get install -y ubuntu-drivers-common
      ubuntu-drivers autoinstall
      log "HINWEIS: Falls der Nvidia-Treiber nach dem naechsten Neustart nicht laedt, ist vermutlich Secure Boot im BIOS aktiv (kein automatisches MOK-Enrollment moeglich) - dort deaktivieren."
      ;;
    *)
      log "Unbekannter GPU-Vendor (PCI-ID ${VID}) - kein Hardware-Encoding-Backend installiert, OBS faellt auf Software-x264 zurueck"
      ;;
  esac
done

# OBS-Encoder-ID fuer das erkannte Hardware-Backend merken (unten beim
# Schreiben der basic.ini genutzt) - bei mehreren GPUs (z.B. Intel-iGPU +
# Nvidia-dGPU) hat die dedizierte Karte Vorrang vor der integrierten.
# "ffmpeg_vaapi_tex" ist OBS' generische Linux-VAAPI-Encoder-ID, gilt fuer
# Intel UND AMD gleichermassen (beide laufen ueber denselben Kernel-DRM/
# VAAPI-Pfad) - "jim_nvenc" ist OBS' Nvidia-eigene NVENC-Encoder-ID (nutzt
# die proprietaere NVENC-API statt VAAPI). Bleibt leer (= Software-x264),
# wenn kein bekannter Vendor gefunden wurde.
OBS_HW_ENCODER=""
if echo "${GPU_VENDOR_IDS}" | grep -qx '10de'; then
  OBS_HW_ENCODER="jim_nvenc"
elif echo "${GPU_VENDOR_IDS}" | grep -qx '1002'; then
  OBS_HW_ENCODER="ffmpeg_vaapi_tex"
elif echo "${GPU_VENDOR_IDS}" | grep -qx '8086'; then
  OBS_HW_ENCODER="ffmpeg_vaapi_tex"
fi

# --- 0a. Boot-Splash (Plymouth-Theme mit eigenem IRL-Motiv) -----------------
# Ersetzt das Standard-Ubuntu-Bootlogo durch ein eigenes Vollbild-Theme
# (Camper/Berge/Lagerfeuer-Motiv mit "IRL Streamer OS"-Logo, Nutzerwunsch
# 2026-08-31). Greift erst ab dem NAECHSTEN Boot nach diesem Provisioning-Lauf
# (also dem ersten echten Systemstart) - der Ubuntu-Installer-Bootscreen davor
# bleibt bewusst unangetastet, weil der aus der Live-Squashfs des
# Basis-ISOs kommt und dieses Projekt bewusst KEINE Squashfs-Aenderungen
# macht (siehe iso-build/build-desktop-autoinstall-iso.sh).
#
# Bild liegt bereits fertig auf 1920x1080 randlos zugeschnitten vor
# (assets/plymouth/irl-streamer-os/background.png). Idempotent: einfaches
# Ueberschreiben-und-neu-Aktivieren, kein Schaden bei erneutem Lauf.
THEME_SRC="${PROJECT_DIR}/provision/assets/plymouth/irl-streamer-os"
THEME_DST="/usr/share/plymouth/themes/irl-streamer-os"
if [ -d "${THEME_SRC}" ]; then
  log "Installiere Plymouth-Boot-Splash-Theme 'IRL Streamer OS'"
  mkdir -p "${THEME_DST}"
  cp "${THEME_SRC}/irl-streamer-os.plymouth" "${THEME_DST}/"
  cp "${THEME_SRC}/irl-streamer-os.script" "${THEME_DST}/"
  cp "${THEME_SRC}/background.png" "${THEME_DST}/"

  if command -v update-alternatives >/dev/null 2>&1; then
    update-alternatives --install /usr/share/plymouth/themes/default.plymouth \
      default.plymouth "${THEME_DST}/irl-streamer-os.plymouth" 100
    update-alternatives --set default.plymouth "${THEME_DST}/irl-streamer-os.plymouth"
  fi

  if command -v update-initramfs >/dev/null 2>&1; then
    log "Baue initramfs neu, damit das Theme beim naechsten Boot greift"
    update-initramfs -u
  else
    log "WARNUNG: update-initramfs nicht gefunden - Boot-Splash-Theme greift erst nach manuellem 'update-initramfs -u'"
  fi
else
  log "WARNUNG: ${THEME_SRC} nicht gefunden - Boot-Splash-Theme wird uebersprungen"
fi

# --- 1. Autologin (gdm3) in die GNOME-Wayland-Session -----------------------
# Live verifiziert (2026-08-24, Ubuntu 26.04): /usr/share/xsessions/ existiert
# GAR NICHT mehr - Ubuntu hat die Xorg-Session komplett fallen gelassen (nur
# noch /usr/share/wayland-sessions/ubuntu.desktop). Der urspruengliche Plan
# ("WaylandEnable=false erzwingen", siehe Git-Historie) ist damit obsolet -
# wir akzeptieren Wayland jetzt aktiv und ersetzen x11vnc/xrandr unten durch
# GNOMEs eigenen Wayland-nativen RDP-Server (gnome-remote-desktop/grdctl).
log "Richte gdm3-Autologin fuer ${TARGET_USER} ein (GNOME/Wayland, Standardsession)"
progress 3 "Boot-Splash und Autologin werden eingerichtet..."
mkdir -p /etc/gdm3
cat > /etc/gdm3/custom.conf <<EOF
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=${TARGET_USER}
EOF

# AccountsService-Fallback fuer den Fall einer manuellen An-/Abmeldung ueber
# den Greeter (Autologin oben ist davon nicht betroffen) - "ubuntu" ist die
# einzige verfuegbare Session (/usr/share/wayland-sessions/ubuntu.desktop).
mkdir -p /var/lib/AccountsService/users
cat > "/var/lib/AccountsService/users/${TARGET_USER}" <<EOF
[User]
Session=ubuntu
XSession=ubuntu
SystemAccount=false
EOF

# --- 1b. Bildschirmsperre/Standby/Screensaver dauerhaft deaktivieren -------
# Appliance-Anforderung (2026-08-24): der PC laeuft dauerhaft, niemand
# sitzt regelmaessig davor um sich einzuloggen oder eine Sperre zu
# entsperren - Autologin oben allein reicht nicht, GNOME sperrt den
# Bildschirm/faehrt den Monitor runter trotzdem nach ein paar Minuten
# Inaktivitaet. Ueber ein systemweites dconf-Profil ("local", per
# /etc/dconf/profile/user Standard fuer alle Nutzer) statt per gsettings im
# Autostart-Skript gesetzt - wirkt sofort bei jedem Login, kein Warten auf
# einen Autostart-Eintrag noetig, und die "locks"-Datei verhindert, dass die
# Einstellung versehentlich (z.B. ueber die GNOME-Einstellungen-App) wieder
# aktiviert wird.
mkdir -p /etc/dconf/profile
cat > /etc/dconf/profile/user <<'EOF'
user-db:user
system-db:local
EOF

mkdir -p /etc/dconf/db/local.d /etc/dconf/db/local.d/locks
cat > /etc/dconf/db/local.d/00-irl-streamer-os-no-lock <<'EOF'
[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false
ubuntu-lock-on-suspend=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
idle-dim=false
lid-close-ac-action='nothing'
lid-close-battery-action='nothing'

[org/gnome/desktop/lockdown]
disable-lock-screen=true
EOF

cat > /etc/dconf/db/local.d/locks/00-irl-streamer-os-no-lock <<'EOF'
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/desktop/screensaver/ubuntu-lock-on-suspend
/org/gnome/desktop/session/idle-delay
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
/org/gnome/settings-daemon/plugins/power/lid-close-ac-action
/org/gnome/settings-daemon/plugins/power/lid-close-battery-action
EOF

# --- Darkmode als Standard + Ersteinrichtungsassistent ueberspringen -------
# (Nutzerwunsch 2026-09-05): der GNOME-Ersteinrichtungsassistent
# (gnome-initial-setup, fragt u.a. Online-Konten, Standort, Ubuntu Pro,
# Hell-/Dunkelmodus ab) erscheint sonst ZWEIMAL - einmal direkt nach der
# reinen Ubuntu-Grundinstallation beim allerersten grafischen Login, und ein
# zweites Mal nach dem provision.sh-Lauf + Neustart (neuer Login-Zyklus
# erkennt den Assistenten erneut als "noch nicht durchlaufen", wenn der
# Marker fehlt). gnome-initial-setup markiert sich selbst als erledigt ueber
# eine reine Existenz-Datei pro Benutzer - die same Datei vorab anzulegen
# ist der von GNOME selbst vorgesehene Weg, den Assistenten zu ueberspringen
# (kein Downgrade/Entfernen des Pakets noetig, betrifft nur den
# Erstlauf-Check). Der Dark-Style wird zusaetzlich per dconf gesetzt UND
# gelockt, damit er nicht nur "Standard beim ersten Start" ist, sondern
# dauerhaft bestehen bleibt, egal was der (uebersprungene) Assistent sonst
# gesetzt haette.
mkdir -p "${HOME_DIR}/.config"
touch "${HOME_DIR}/.config/gnome-initial-setup-done"
chown streamer:streamer "${HOME_DIR}/.config/gnome-initial-setup-done"

# BUGFIX (05.09., live gefunden - Assistent erschien trotz obigem Marker
# WEITERHIN): es gibt einen ZWEITEN, unabhaengigen systemd-User-Service
# (gnome-initial-setup-upgrade-login.service), der GENAU DANN laeuft, wenn
# gnome-initial-setup-done EXISTIERT (!) UND ein separater Marker
# (gnome-initial-setup/upgrade-26.04-done) FEHLT - zeigt eine eigene
# "Insights"/Datenschutz-Seite (GisUbuntuInsightsPage), unabhaengig vom
# oben behandelten Erstlauf-Assistenten. Live per journalctl --user
# bestaetigt: "gnome-initial-setup-first-login.service ... skipped" (der
# erste Marker wirkt), aber direkt danach "Starting
# gnome-initial-setup-upgrade-login.service" (der zweite Assistent laeuft
# trotzdem). Beide Marker-Dateien muessen VORHANDEN sein, damit gar kein
# gnome-initial-setup-Assistent mehr erscheint - siehe auch
# /usr/lib/systemd/user/gnome-initial-setup-upgrade-login.service fuer die
# genaue Condition-Logik.
mkdir -p "${HOME_DIR}/.config/gnome-initial-setup"
touch "${HOME_DIR}/.config/gnome-initial-setup/upgrade-26.04-done"
chown -R streamer:streamer "${HOME_DIR}/.config/gnome-initial-setup"

cat > /etc/dconf/db/local.d/02-irl-streamer-os-darkmode <<'EOF'
[org/gnome/desktop/interface]
color-scheme='prefer-dark'
gtk-theme='Yaru-dark'
icon-theme='Yaru-dark'
EOF

cat > /etc/dconf/db/local.d/locks/02-irl-streamer-os-darkmode <<'EOF'
/org/gnome/desktop/interface/color-scheme
EOF

# Ubuntu-Pro-Werbe-Popup unterdruecken (erscheint separat vom
# gnome-initial-setup-Assistenten, meldet sich z.B. per
# "ubuntu-advantage-tools" motd/GUI-Hinweis auf freie Ubuntu-Pro-Testphase).
# systemctl mask verhindert zuverlaessig jeden GUI-Aufruf, ohne das Paket
# selbst zu deinstallieren (koennte von anderen apt-Abhaengigkeiten
# gebraucht werden).
systemctl mask ubuntu-advantage-notification.timer 2>/dev/null || true
systemctl mask esm-cache.service 2>/dev/null || true

# Zusaetzliche Absicherung auf Systemebene, unabhaengig von GNOME (Nutzer-
# wunsch 2026-08-25, "darf niemals schlafen"): systemd-Sleep-Targets
# komplett maskieren, statt sich nur auf die dconf-Einstellungen oben zu
# verlassen - dadurch wird Suspend/Hibernate/Hybrid-Sleep strukturell
# unmoeglich, egal WAS es ausloesen will (GNOME selbst, ein ACPI-Ereignis,
# ein direkter D-Bus-Aufruf einer anderen Anwendung). "mask" statt nur
# "disable", weil mask den Unit-Namen auf /dev/null verlinkt - selbst ein
# expliziter "systemctl suspend"-Aufruf schlaegt dann fehl, statt nur
# automatische Ausloeser zu verhindern.
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target 2>/dev/null || true

# Hintergrund-Update-/Benachrichtigungsdienste abschalten + Login-Keyring-
# Sperre beheben (Nutzerwunsch 2026-08-25, "Authentication required"-
# Dialoge live beobachtet): passt nicht zu einer Appliance, die spaeter
# eigenstaendig und unbeaufsichtigt laufen soll - Updates managt der Nutzer
# bewusst selbst, keine spontanen Hintergrund-Prompts.
systemctl mask apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask fwupd-refresh.timer 2>/dev/null || true
systemctl mask update-notifier-download.timer update-notifier-motd.timer 2>/dev/null || true
systemctl mask packagekit.service 2>/dev/null || true
rm -f /etc/xdg/autostart/update-notifier.desktop
pkill -u "${TARGET_USER}" -f update-notifier 2>/dev/null || true
pkill -u "${TARGET_USER}" -f snapd-desktop-integration 2>/dev/null || true
snap remove snapd-desktop-integration 2>/dev/null || true

# WICHTIG - Keyring NIEMALS loeschen (live gelernt, 2026-08-25): ein
# frueherer Stand dieses Skripts loeschte hier login.keyring, um den
# "Choose password for new keyring"-Dialog unter GDM-Autologin zu
# vermeiden. Folgeschaden live erlebt: GNOME Remote Desktop speichert
# seine RDP-Zugangsdaten IM Keyring - nach dem Loeschen lehnte grd jeden
# Guacamole-Client mit "Credentials are not set" ab (RDP komplett tot),
# bis Keyring + Credentials manuell neu angelegt waren. Der Dialog beim
# allerersten Boot ist der harmlosere Zustand: einmal mit LEEREM Passwort
# bestaetigen (in der Kundenanleitung dokumentiert), danach erscheint er
# nie wieder und alles funktioniert dauerhaft.

# Energiesparplan auf "Leistung" statt Standard "Ausgeglichen" (Nutzer-
# wunsch 2026-08-25) - passt zu einer Streaming-Appliance, die nie drosseln
# soll. power-profiles-daemon merkt sich den gewaehlten Modus NICHT ueber
# einen Neustart hinweg (kein dconf/gsettings-Wert, eigener D-Bus-Dienst
# ohne persistente Config) - deshalb zusaetzlich zum sofortigen Setzen ein
# Oneshot-Systemdienst, der das bei jedem Boot erneut anwendet.
command -v powerprofilesctl >/dev/null 2>&1 && powerprofilesctl set performance 2>/dev/null || true
cat > /etc/systemd/system/irl-streamer-performance-profile.service <<'EOF'
[Unit]
Description=IRL Streamer OS - Energiesparplan auf Leistung setzen
After=power-profiles-daemon.service
Requires=power-profiles-daemon.service

[Service]
Type=oneshot
ExecStart=/usr/bin/powerprofilesctl set performance

[Install]
WantedBy=multi-user.target
EOF
systemctl enable irl-streamer-performance-profile.service 2>/dev/null || true

# Dauerhafter Idle-Inhibitor (Nutzermeldung 2026-08-25, live auf einer
# echten Frischinstallation reproduziert): trotz idle-delay=0 und
# idle-activation-enabled=false (beide korrekt gesetzt UND per dconf-Lock
# unveraenderbar, live gegengeprueft) ging der Bildschirm nach einiger Zeit
# trotzdem in Standby und zeigte den GNOME-Sperrbildschirm ("Durch
# Mausklick oder Tastendruck entsperren") - das Blanking laeuft in dieser
# GNOME-Shell-Version offenbar ueber einen eigenen Compositor-Pfad, der
# nicht ausschliesslich von den og. dconf-Werten gesteuert wird. Der
# robuste, auch in Kiosk-/Digital-Signage-Systemen uebliche Weg dagegen:
# ein dauerhaft aktiver systemd-inhibit-Lock im "block"-Modus (staerker
# als GNOMEs eigene "delay"-Inhibitoren, siehe "systemd-inhibit --list")
# statt sich nur auf Einstellungswerte zu verlassen.
STREAMER_UID_EARLY="$(id -u "${TARGET_USER}")"
mkdir -p "${HOME_DIR}/.config/systemd/user"
cat > "${HOME_DIR}/.config/systemd/user/irl-streamer-no-idle.service" <<'EOF'
[Unit]
Description=IRL Streamer OS - Bildschirm-Standby/Sperre/Ruhezustand dauerhaft verhindern

[Service]
ExecStart=/usr/bin/systemd-inhibit --what=idle:sleep:handle-lid-switch --who=IRL-Streamer-OS --why=Always-on-Streaming-Appliance --mode=block sleep infinity
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
chown -R "${TARGET_USER}:${TARGET_USER}" "${HOME_DIR}/.config/systemd"
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID_EARLY}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID_EARLY}/bus" \
  systemctl --user daemon-reload
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID_EARLY}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID_EARLY}/bus" \
  systemctl --user enable --now irl-streamer-no-idle.service

# "Programm reagiert nicht"-Dialog von Mutter seltener ausloesen (Nutzer-
# meldung, 2026-08-25): Standard-Timeout ist 5s - reicht bei OBS mit reiner
# Software-Kodierung (keine Hardware-GPU in dieser Test-VM, siehe
# GPU-Erkennung weiter unten) plus gleichzeitig laufenden Docker-Builds
# gelegentlich nicht aus, obwohl OBS nicht wirklich haengt. Grosszuegig auf
# 30s statt komplett deaktiviert, damit ein echtes Haengenbleiben trotzdem
# irgendwann angezeigt wird.
cat > /etc/dconf/db/local.d/01-irl-streamer-os-mutter <<'EOF'
[org/gnome/mutter]
check-alive-timeout=uint32 30000
EOF

# Deutsches Tastaturlayout auch fuer GNOME selbst setzen (live gefunden,
# 2026-08-25 auf echter Mini-PC-Hardware): Autoinstall setzt zwar
# System-/X11-Layout korrekt auf "de" (siehe /etc/default/keyboard,
# "localectl status"), aber GNOMEs EIGENE Eingabequellen-Liste
# ("org.gnome.desktop.input-sources") bleibt dabei leer - die wird normaler-
# weise vom interaktiven "GNOME Initial Setup"-Assistenten beim ersten Login
# gesetzt, den es bei Autoinstall gar nicht gibt. Leere Liste faellt in der
# Praxis auf Englisch zurueck, obwohl das System-Layout korrekt "de" zeigt.
cat > /etc/dconf/db/local.d/02-irl-streamer-os-keyboard <<'EOF'
[org/gnome/desktop/input-sources]
sources=[('xkb', 'de')]
EOF

dconf update

# Desktop-Hintergrund: Anthrazit-Flaeche mit dem IRL-Streamer-OS-Logo gross
# mittig (Nutzerwunsch 2026-08-31) - dasselbe Logo, das auch im
# Diagnose-Dashboard/Desktop-Icon verwendet wird (siehe
# docker/irl-diagnostics-src/static/icon-192.png), hier vorgerendert als
# fertiges 1920x1080-Wallpaper statt es zur Laufzeit zusammenzusetzen
# (einfacher, kein zusaetzliches Bildbearbeitungs-Tooling auf dem
# Zielsystem noetig). Ueber dconf-Default gesetzt (wie Tastaturlayout oben)
# statt nur per gsettings zum Provisioning-Zeitpunkt - dconf-Default greift
# zuverlaessig auch bei einem spaeteren Zuruecksetzen auf Standardwerte.
WALLPAPER_SRC="${PROJECT_DIR}/provision/assets/desktop/wallpaper.png"
WALLPAPER_DST="/usr/share/backgrounds/irl-streamer-os-wallpaper.png"
if [ -f "${WALLPAPER_SRC}" ]; then
  log "Setze Desktop-Hintergrund (Anthrazit + Logo)"
  mkdir -p "$(dirname "${WALLPAPER_DST}")"
  cp "${WALLPAPER_SRC}" "${WALLPAPER_DST}"
  cat > /etc/dconf/db/local.d/03-irl-streamer-os-background <<EOF
[org/gnome/desktop/background]
picture-uri='file://${WALLPAPER_DST}'
picture-uri-dark='file://${WALLPAPER_DST}'
picture-options='zoom'
primary-color='#181a1e'
secondary-color='#181a1e'

[org/gnome/desktop/screensaver]
picture-uri='file://${WALLPAPER_DST}'
picture-options='zoom'
EOF
  dconf update
else
  log "WARNUNG: ${WALLPAPER_SRC} nicht gefunden - Desktop-Hintergrund wird uebersprungen"
fi

# Zusaetzlicher Kick-Fix (live gefunden, 2026-08-25, auf einer erneuten
# Frischinstallation MIT obigem dconf-Default bereits korrekt gesetzt): das
# Layout stand in GNOME-Einstellungen sichtbar korrekt auf "Deutsch", tippte
# aber trotzdem US-Layout (y/z vertauscht) - erst ein manueller Wechsel auf
# eine andere Sprache und zurueck hat es tatsaechlich aktiviert. Offenbar
# uebernimmt mutter/GNOME Shell bei einer ganz frischen Sitzung den
# dconf-Default nicht zuverlaessig in den tatsaechlichen Compositor-Zustand -
# ein "echter" Quellenwechsel (den GNOME Shell aktiv verarbeitet, anders als
# eine reine dconf-Werteanzeige) behebt das. Simuliert per Autostart-Skript
# beim naechsten Login: kurz auf eine zweite Quelle wechseln, dann zurueck
# auf die eigentliche Konfiguration - danach loescht sich der Eintrag selbst
# (gleiches Einmal-Muster wie beim Icon-Vertrauens-Fix).
mkdir -p "${HOME_DIR}/.config/autostart"
cat > "${HOME_DIR}/.config/autostart/irl-streamer-keyboard-fix.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=IRL Streamer OS - Tastaturlayout aktivieren
Exec=bash -c "sleep 2; gsettings set org.gnome.desktop.input-sources sources \"[('xkb', 'us'), ('xkb', 'de')]\"; sleep 1; gsettings set org.gnome.desktop.input-sources current 1; sleep 1; gsettings set org.gnome.desktop.input-sources sources \"[('xkb', 'de')]\"; rm -f /home/streamer/.config/autostart/irl-streamer-keyboard-fix.desktop"
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
chown "${TARGET_USER}:${TARGET_USER}" "${HOME_DIR}/.config/autostart/irl-streamer-keyboard-fix.desktop"

# --- 2. OBS-Websocket-Passwort (einmalig generieren, danach stabil halten) -
WS_PASS_FILE="${STATE_DIR}/obs-websocket-password.txt"
if [ ! -s "${WS_PASS_FILE}" ]; then
  generate_readable_password 20 > "${WS_PASS_FILE}"
fi
OBS_WS_PASSWORD="$(cat "${WS_PASS_FILE}")"

DASH_PASS_FILE="${STATE_DIR}/dashboard-password.txt"
if [ ! -s "${DASH_PASS_FILE}" ]; then
  generate_readable_password 20 > "${DASH_PASS_FILE}"
fi
DASHBOARD_PASSWORD="$(cat "${DASH_PASS_FILE}")"

# --- 3. OBS: Szenen-Collection (LIVE/LOW/BRB) + Websocket-Konfiguration ----
OBS_CONF_DIR="${HOME_DIR}/.config/obs-studio"
log "Lege OBS-Profil/Szenen unter ${OBS_CONF_DIR} an"
progress 4 "OBS Studio wird konfiguriert..."
mkdir -p "${OBS_CONF_DIR}/basic/scenes"
mkdir -p "${OBS_CONF_DIR}/basic/profiles/IRL-Streamer"
mkdir -p "${OBS_CONF_DIR}/plugin_config/obs-websocket"

# Szenen-Collection aus der Vorlage uebernehmen (LIVE/LOW/BRB, siehe
# provision/obs-scenes.json) - Name der Collection wird in global.ini als
# Standard gesetzt, damit OBS sie beim ersten Start automatisch oeffnet.
cp "${PROJECT_DIR}/provision/obs-scenes.json" "${OBS_CONF_DIR}/basic/scenes/IRL-Streamer.json"

cat > "${OBS_CONF_DIR}/basic/profiles/IRL-Streamer/basic.ini" <<EOF
[General]
Name=IRL-Streamer

[Video]
BaseCX=1920
BaseCY=1080
OutputCX=1920
OutputCY=1080
FPSType=0
FPSCommon=30
EOF

# Hardware-Encoder als Streaming-Standard vorbelegen, statt sich auf den
# Nutzer zu verlassen, das manuell im Erweiterten Ausgabemodus umzustellen -
# live verifiziert (2026-08-24, Mini-PC): der "Einfache" Ausgabemodus zeigt
# unter Linux ueberhaupt keine VAAPI-Option an (bekannte OBS-Linux-
# Einschraenkung, nur Software-x264 dort waehlbar), nur der "Erweiterte"
# Modus bietet den erkannten Hardware-Encoder an. Da Hardware-Encoding auf
# schwacher Mini-PC-Hardware (z.B. N100) notwendig statt optional ist, wird
# hier direkt in den Erweiterten Modus mit vorgewaehltem Hardware-Encoder
# geschaltet, sobald oben ein passender GPU-Vendor erkannt wurde - ohne
# eigenes GPU-Backend (OBS_HW_ENCODER leer) bleibt es beim OBS-Standard
# (Einfacher Modus, Software-x264). Der konkrete VAAPI-Geraetepfad (z.B.
# /dev/dri/by-path/...) wird bewusst NICHT hier gesetzt - OBS erkennt und
# schreibt den passenden Pfad selbst automatisch beim ersten echten Start,
# das haendisch vorzugeben waere hardwarespezifisch (PCI-Bus-Adresse) und
# wuerde die Wiederverwendbarkeit auf anderer Hardware kaputt machen.
if [ -n "${OBS_HW_ENCODER}" ]; then
  cat >> "${OBS_CONF_DIR}/basic/profiles/IRL-Streamer/basic.ini" <<EOF

[Output]
Mode=Advanced

[AdvOut]
ApplyServiceSettings=true
Encoder=${OBS_HW_ENCODER}
EOF
fi

cat > "${OBS_CONF_DIR}/global.ini" <<EOF
[Basic]
Profile=IRL-Streamer
ProfileDir=IRL-Streamer
SceneCollection=IRL-Streamer
SceneCollectionFile=IRL-Streamer

[General]
FirstRun=false
# Browser-Quellen (Twitch-Chat-Overlay) nutzen sonst CEF mit GPU-
# Beschleunigung (Zink/EGL) - auf Systemen ohne richtige GPU-Treiber
# (live gefunden 2026-08-25 auf der Test-VM: nur virtuelle QXL-Grafik,
# "Unbekannter GPU-Vendor") stuerzt das mit "MESA: ZINK: failed to choose
# pdev" -> Segfault in libcef.so ab und reisst OBS mit. Software-Rendering
# ist etwas langsamer, aber stabil - sinnvoller Standard fuer eine
# unbeaufsichtigte Appliance, die auch auf Mini-PCs mit schwacher/keiner
# dedizierten GPU laufen soll.
BrowserHWAccel=false
EOF

# obs-websocket (seit OBS 28 eingebaut) - Server aktiv, Passwort wie oben
# generiert. Die Diagnose-Dashboard-Config muss auf denselben Wert zeigen
# (siehe docker/belabox unten bzw. main.py-Konfiguration, vom Nutzer beim
# Ersteinrichten im Dashboard einzutragen - Phase 2 automatisiert das).
cat > "${OBS_CONF_DIR}/plugin_config/obs-websocket/config.json" <<EOF
{
  "alerts_enabled": false,
  "auth_required": true,
  "first_load": false,
  "server_enabled": true,
  "server_password": "${OBS_WS_PASSWORD}",
  "server_port": 4455
}
EOF

# --- 3b. Aufloesung 1920x1080 setzen (Wayland-nativ per gdctl) -------------
# xrandr geht unter Wayland nicht mehr (siehe x11vnc-Abschnitt weiter unten).
# gdctl (GNOME 50+, ersetzt gnome-monitor-config) ist das Wayland-native
# Pendant. "--persistent" schreibt live beobachtet (2026-08-24, VM-Test)
# KEINE dauerhafte ~/.config/monitors.xml - deshalb stattdessen bei JEDEM
# grafischen Login per Autostart neu gesetzt, robuster als sich auf einen
# unklaren Persistenz-Mechanismus zu verlassen. Erkennt den Monitor-Namen
# automatisch (z.B. "Virtual-1" in der Test-VM, "HDMI-1" bei echter Hardware
# mit angeschlossenem Bildschirm) statt ihn hart zu verdrahten.
mkdir -p "${HOME_DIR}/.local/bin"
RESOLUTION_SCRIPT="${HOME_DIR}/.local/bin/irl-streamer-set-resolution.sh"
cat > "${RESOLUTION_SCRIPT}" <<'EOF'
#!/usr/bin/env bash
# Wiederholungsschleife statt Einmalversuch - live gefunden (2026-08-25 auf
# der Test-VM): direkt nach dem Login ist Mutters Wayland-Monitor-Management
# manchmal noch nicht bereit, "gdctl" schlaegt dann fehl, was das Skript
# bisher per "2>/dev/null || true" stillschweigend verschluckt hat (manuell
# Sekunden spaeter ausgefuehrt hat derselbe Befehl anstandslos funktioniert).
# Eine Schleife ist robuster als eine geratene feste Verzoegerung, weil
# unklar ist wie lange eine schwaechere Mini-PC-GPU dafuer braucht.
for i in $(seq 1 15); do
  CONNECTOR="$(gdctl show 2>/dev/null | grep -oP '(?<=Monitor )\S+' | head -1)"
  if [ -n "${CONNECTOR}" ] && gdctl show --verbose 2>/dev/null | grep -q '1920x1080@60\.000'; then
    if gdctl set --logical-monitor --monitor "${CONNECTOR}" --mode 1920x1080@60.000 --primary 2>/dev/null; then
      exit 0
    fi
  fi
  sleep 1
done
EOF
chmod +x "${RESOLUTION_SCRIPT}"

mkdir -p "${HOME_DIR}/.config/autostart"
cat > "${HOME_DIR}/.config/autostart/irl-streamer-resolution.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=IRL Streamer Aufloesung
Exec=${RESOLUTION_SCRIPT}
X-GNOME-Autostart-enabled=true
EOF

# --- 4. OBS-Autostart beim grafischen Login (nicht: automatisch Stream
#        starten - nur automatisches OEFFNEN) -------------------------------
LAUNCH_SCRIPT="${HOME_DIR}/.local/bin/irl-streamer-obs-launch.sh"
# QT_QPA_PLATFORM=xcb (unten im Wrapper): live beobachtet (2026-08-24,
# Mini-PC), das Vorschaufenster flackert beim OBS-Start dauerhaft, bis der
# Nutzer manuell ein Dock in der Groesse veraendert (erzwingt ein Qt-
# Relayout). Bekannter Qt-auf-nativem-Wayland-Bug (OBS ist Qt6-basiert,
# laeuft hier per default nativ ueber "Using EGL/Wayland" statt XWayland) -
# das GUI-Fenstersystem per QT_QPA_PLATFORM=xcb auf Xwayland umzuleiten ist
# der uebliche Community-Workaround dafuer. Betrifft nur die Qt-Fenster-
# verwaltung, NICHT die eigentliche VAAPI-Videoausgabe/-kodierung.
cat > "${LAUNCH_SCRIPT}" <<'EOF'
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
if ! sudo -n bash /opt/irl-streamer-os/provision/licensing/license-guard.sh; then
  exit 0
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

export QT_QPA_PLATFORM=xcb
exec obs --collection "IRL-Streamer" --profile "IRL-Streamer" --scene "LIVE"
EOF
chmod +x "${LAUNCH_SCRIPT}"

# systemd --user-Unit statt reinem .desktop-Autostart-Eintrag (Umbau
# 07.09.2026 fuer den neuen OBS-Notfallknopf im Diagnose-Dashboard): der
# Knopf muss OBS gezielt starten/stoppen koennen, nicht nur beim Login
# automatisch oeffnen - eine .desktop-Datei bietet dafuer keinen sauberen
# Ansprechpunkt (kein "ist gerade an/aus"-Status, kein gezieltes Beenden
# ohne pgrep+kill-Gebastel). Startet weiterhin automatisch beim
# grafischen Login (WantedBy=graphical-session.target), ist aber
# zusaetzlich per 'systemctl --user start/stop irl-streamer-obs.service'
# von aussen steuerbar - genau das nutzt der neue Host-Control-Watcher
# (siehe irl-streamer-host-control.service weiter unten).
cp "${PROJECT_DIR}/provision/assets/irl-streamer-obs.service" \
  "${HOME_DIR}/.config/systemd/user/irl-streamer-obs.service"
chown "${TARGET_USER}:${TARGET_USER}" "${HOME_DIR}/.config/systemd/user/irl-streamer-obs.service"
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID_EARLY}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID_EARLY}/bus" \
  systemctl --user daemon-reload
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID_EARLY}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID_EARLY}/bus" \
  systemctl --user enable irl-streamer-obs.service

# Host-Control-Watcher (Nutzerwunsch 07.09.2026: OBS-Notfallknopf + PC-
# Neustart-Knopf im Diagnose-Dashboard) - laeuft als root-System-Dienst
# DIREKT auf dem Host (NICHT im Docker-Container, der hat keinen Zugriff
# auf Host-Prozesse/den echten Reboot-Befehl), beobachtet einen
# gemeinsamen Ordner, den docker-compose.yml read-write in den
# irl-diagnostics-Container mountet (siehe dortiger Kommentar).
mkdir -p "${PROJECT_DIR}/provision/assets"
mkdir -p /var/lib/irl-streamer-host-control
cp "${PROJECT_DIR}/provision/assets/irl-streamer-host-control.py" \
  /usr/local/bin/irl-streamer-host-control.py
chmod +x /usr/local/bin/irl-streamer-host-control.py
sed "s#/opt/irl-streamer-os/provision/assets/irl-streamer-host-control.py#/usr/local/bin/irl-streamer-host-control.py#" \
  "${PROJECT_DIR}/provision/assets/irl-streamer-host-control.service" \
  > /etc/systemd/system/irl-streamer-host-control.service
systemctl daemon-reload
systemctl enable --now irl-streamer-host-control.service

# --- 4b. OBS/Chrome beim Herunterfahren/Neustart sauber beenden
#         (Nutzerwunsch 03.09.): Live beobachtetes Problem - beim
#         Poweroff/Reboot wurden OBS und Chrome mitsamt der grafischen
#         Sitzung abgewuergt, ohne Zeit fuer ihr normales Herunterfahren
#         zu bekommen. OBS zeigte danach beim naechsten Start den
#         "Abgesicherter Modus"-Dialog, Chrome fragte nach "Seiten
#         wiederherstellen?" - beides erkennt einen unsauberen Exit
#         selbst (fehlender clean-shutdown-Marker), das laesst sich
#         nicht wegkonfigurieren, nur durch tatsaechlich sauberes
#         Beenden vor dem Systemende vermeiden.
#
# VIER FEHLGESCHLAGENE ANSAETZE VOR DIESER LOESUNG (03.09., live auf
# 192.168.10.223 durchgetestet, hier dokumentiert damit niemand sie
# nochmal probiert):
#   1. systemd-System-Service mit "Before=shutdown.target reboot.target
#      halt.target" - wurde beim ECHTEN Shutdown/Reboot komplett
#      uebersprungen (journalctl zeigte "Reached target shutdown.target"
#      ohne jeden "Stopping"-Eintrag), weil "Before=" mit
#      "DefaultDependencies=no" NICHT automatisch die sonst implizite
#      "Conflicts=shutdown.target"-Bindung mitbringt.
#   2. Dieselbe Unit + "Conflicts=shutdown.target" ergaenzt - wurde jetzt
#      nachweislich gestoppt, aber OBS blieb trotzdem "unclean" (X11-Log:
#      "The X11 connection broke (error 1)"): GNOME/GDM beendet die
#      grafische Sitzung PARALLEL zum System-Shutdown, nicht danach - ein
#      reiner Reihenfolge-Trick zwischen zwei System-Units kann diesen
#      Wettlauf nicht gewinnen.
#   3. Zusaetzlich "Before=gdm.service" ergaenzt, in der Annahme das
#      wuerde unseren Service vor gdm zum Stoppen bringen - GENAU FALSCH
#      GEPOLT: systemd stoppt Units beim Herunterfahren IMMER in
#      UMGEKEHRTER Reihenfolge zu ihrer Start-Abhaengigkeit, "Before="
#      bedeutet also beim Stoppen "NACH" gdm, nicht davor.
#   4. Ein logind-"delay"-Inhibitor als systemd-USER-Service (haelt eine
#      Inhibit-Sperre, die logind zwingt, mit dem GESAMTEN Herunterfahren
#      zu warten). Der Inhibitor-Mechanismus selbst funktionierte
#      nachweislich (systemd-inhibit --list zeigte die aktive Sperre) -
#      ABER: der Inhibitor-PROZESS lief selbst als User-Service unter
#      default.target, und genau DIESES Target wird beim Session-Ende
#      durch gnome-session-manager@ubuntu.service SELBST (nicht durch
#      logind/unseren Inhibitor) abgewuergt - "systemd --user" stoppte
#      unseren Inhibitor-Prozess dadurch bereits 21 Millisekunden nach
#      Beginn seines eigenen Stop-Vorgangs, lange bevor sein Warten auf
#      OBS/Chrome ueberhaupt zum Tragen kommen konnte. Ein User-Service
#      kann sich also nicht selbst vor dem Sterben der eigenen Session
#      schuetzen, egal welche systemd-Dependencies er bekommt.
#
# FUNKTIONIERENDE LOESUNG: derselbe logind-"delay"-Inhibitor-Ansatz wie
# in Versuch 4, aber als systemd-SYSTEM-Service (nicht User-Service)
# ausgefuehrt - ein System-Service lebt komplett ausserhalb der
# Benutzersitzung und kann daher prinzipbedingt NICHT vom Sterben dieser
# Sitzung mitgerissen werden. Er greift trotzdem auf die grafischen
# Anwendungsprozesse zu (die laufen ja weiterhin unter dem streamer-User,
# "kill -TERM <pid>" braucht dafuer keine eigene Session).
cp "${PROJECT_DIR}/provision/assets/irl-streamer-shutdown-inhibitor.py" \
  "${HOME_DIR}/.local/bin/irl-streamer-shutdown-inhibitor.py"
chmod +x "${HOME_DIR}/.local/bin/irl-streamer-shutdown-inhibitor.py"
chown "${TARGET_USER}:${TARGET_USER}" "${HOME_DIR}/.local/bin/irl-streamer-shutdown-inhibitor.py"

cat > /etc/systemd/system/irl-streamer-shutdown-inhibitor.service <<EOF
[Unit]
Description=IRL Streamer OS - haelt Shutdown auf, bis OBS/Chrome sauber beendet sind
After=multi-user.target docker.service
Wants=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${HOME_DIR}/.local/bin/irl-streamer-shutdown-inhibitor.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now irl-streamer-shutdown-inhibitor.service 2>/dev/null || true

# KRITISCHER BUGFIX #5 (03.09., live gefunden NACH dem User-Service-
# Inhibitor-Fehlversuch #4 oben): unabhaengig vom eigentlichen Inhibitor-
# Mechanismus zeigte sich noch ein ZWEITES, separates Problem: Ubuntus
# systemweite Vorlage fuer ALLE per GNOME-Session gestarteten
# Anwendungen (/usr/lib/systemd/user/app-gnome-.scope.d/override.conf)
# setzt "TimeoutStopSec=5s" fuer JEDEN App-Scope (app-gnome-<name>-
# <pid>.scope). Sobald GNOME/systemd diesen Scope beim Session-Ende zu
# stoppen beginnt, hat OBS nur 5 Sekunden, bevor es per SIGKILL
# zwangsbeendet wird - selbst mit einem korrekt funktionierenden
# Inhibitor wuerde diese kuerzere, unabhaengige Timeout-Uhr das saubere
# Beenden noch unterlaufen koennen. Fix: eigener Drop-in mit hoeherer
# Prioritaet (~/.config/ ueberschreibt /usr/lib/) verlaengert den
# Timeout auf 20s - live gegengeprueft, wirkt sofort per daemon-reload
# auch auf bereits laufende Scopes, kein OBS-Neustart noetig.
mkdir -p "${HOME_DIR}/.config/systemd/user/app-gnome-.scope.d"
cat > "${HOME_DIR}/.config/systemd/user/app-gnome-.scope.d/override.conf" <<'EOF'
[Scope]
TimeoutStopSec=20s
EOF
chown -R "${TARGET_USER}:${TARGET_USER}" "${HOME_DIR}/.config/systemd/user/app-gnome-.scope.d"
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID_EARLY}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID_EARLY}/bus" \
  systemctl --user daemon-reload

# --- 5. Kuratiertes OBS-Plugin-Set (per GitHub-Release-API, kein fest
#        gepinntes Release - siehe Plan: live auf der Test-VM feinjustierbar)
install_obs_plugin_from_github() {
  local repo="$1" pattern="$2"
  local url
  url="$(curl -fsSL "https://api.github.com/repos/${repo}/releases/latest" \
    | jq -r --arg pat "${pattern}" '.assets[] | select(.name | test($pat)) | .browser_download_url' \
    | head -n1)"
  if [ -z "${url}" ] || [ "${url}" = "null" ]; then
    log "WARNUNG: kein passendes Release-Asset fuer ${repo} gefunden (Muster: ${pattern}) - ueberspringe"
    return 0
  fi
  log "Installiere OBS-Plugin aus ${repo}: ${url##*/}"
  local tmpdeb
  tmpdeb="$(mktemp --suffix=.deb)"
  curl -fsSL "${url}" -o "${tmpdeb}"
  apt-get install -y "${tmpdeb}" || log "WARNUNG: Installation von ${repo} fehlgeschlagen, mache weiter"
  rm -f "${tmpdeb}"
}

log "Installiere kuratiertes OBS-Plugin-Set"
progress 5 "OBS-Plugins werden installiert..."
# Ubuntu-Version dynamisch aus /etc/os-release lesen statt fest zu verdrahten -
# live gefunden (2026-08-25): war noch auf "ubuntu24.04" gepinnt, obwohl
# WarmUpTill/SceneSwitcher laengst einen "ubuntu26.04"-Build veroeffentlicht
# hat. Der alte 24.04-Build ist gegen die inzwischen umbenannten "t64"-Qt6-
# Pakete gelinkt, die es unter 26.04 nicht mehr gibt (apt: unerfuellte
# Abhaengigkeiten libqt6gui6t64/libqt6widgets6t64).
UBUNTU_VERSION_ID="$(. /etc/os-release && echo "${VERSION_ID}")"
install_obs_plugin_from_github "WarmUpTill/SceneSwitcher" "ubuntu${UBUNTU_VERSION_ID}-linux-gnu\\.deb\$"
install_obs_plugin_from_github "exeldro/obs-move-transition" 'x86_64-linux-gnu\.deb$'
install_obs_plugin_from_github "exeldro/obs-source-record" 'x86_64-linux-gnu\.deb$'
install_obs_plugin_from_github "exeldro/obs-downstream-keyer" 'x86_64-linux-gnu\.deb$'

# --- 6. Docker-Compose-Stack (Belabox-Receiver = SRTLA-Relay+NOALBS, sowie
#        das IRL-Diagnostics-Dashboard) -------------------------------------
log "Rendere Docker-Compose-Konfiguration"
progress 6 "Docker-Dienste werden vorbereitet..."
BELABOX_DIR="${PROJECT_DIR}/docker/belabox"
mkdir -p "${BELABOX_DIR}"

if [ ! -s "${BELABOX_DIR}/config.json" ]; then
  # "|" statt "/" als sed-Trenner - "openssl rand -base64" kann Slashes im
  # Passwort erzeugen, was mit "/" als Trenner zufaellig fehlschlaegt (live
  # gefunden, 2026-08-25: "sed: -e Ausdruck #1, Zeichen 45: Unbekannte Option
  # fuer »s«"). "|" kommt im Base64-Alphabet nicht vor.
  sed \
    -e "s|__OBS_WEBSOCKET_PASSWORD__|${OBS_WS_PASSWORD}|" \
    "${PROJECT_DIR}/docker/belabox/config.json.template" > "${BELABOX_DIR}/config.json"
fi
if [ ! -s "${BELABOX_DIR}/.env" ]; then
  cp "${PROJECT_DIR}/docker/belabox/.env.template" "${BELABOX_DIR}/.env"
fi

# Diagnose-Dashboard-Zugangsdaten (bcrypt) - dasselbe Verfahren wie im
# bestehenden main.py (siehe [[project_irl_streaming_diagnostics]]).
DASH_HASH="$(python3 -c "import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())" "${DASHBOARD_PASSWORD}")"
# SRTLA_STATS_URL explizit auf den lokalen, in DIESER Appliance gebuendelten
# SRTLA-Relay setzen - main.py faellt ohne diese Variable auf einen fest
# einprogrammierten Default (die urspruengliche separate Produktiv-Unraid-
# NOALBS-Instanz) zurueck. Live gefunden (2026-08-25): auf der Test-VM
# (zufaellig im selben /24-Netz) hat das Dashboard dadurch unbemerkt die
# STATS DES ECHTEN PRODUKTIV-RELAYS angezeigt statt der leeren lokalen -
# haette auf einem anderen Netz schlicht gar keine Daten gezeigt.
cat > "${PROJECT_DIR}/docker/irl-diagnostics.env" <<EOF
DASHBOARD_USERNAME=streamer
DASHBOARD_PASSWORD_HASH=${DASH_HASH//\$/\$\$}
NOALBS_MODE=local_docker
BELABOX_CONTAINER_NAME=belabox-receiver
OBS_HOST=localhost
OBS_PORT=4455
OBS_PASSWORD=${OBS_WS_PASSWORD}
SRTLA_STATS_URL=http://localhost:8181/stats
NOALBS_LOW_SCENE=LOW
NOALBS_OFFLINE_SCENE=BRB
EOF

# Guacamole-Admin-Zugangsdaten (Nutzerwunsch 2026-08-31: PostgreSQL-Datenbank
# statt der vorherigen dateibasierten user-mapping.xml, die keine
# Verwaltung ueber die Weboberflaeche erlaubte - siehe docker-compose.yml).
GUAC_PASS_FILE="${STATE_DIR}/guacamole-password.txt"
if [ ! -s "${GUAC_PASS_FILE}" ]; then
  generate_readable_password 20 > "${GUAC_PASS_FILE}"
fi
GUAC_PASSWORD="$(cat "${GUAC_PASS_FILE}")"

# Eigenes SSH-Schluesselpaar NUR fuer Guacamoles SSH-Verbindung - das
# Login-Passwort des Nutzers kennen wir bei manueller Installation nicht
# (und wollen es auch nicht in einer Konfigdatei ablegen). Oeffentlicher
# Teil geht in authorized_keys, privater Teil direkt in die Datenbank-
# Verbindung (Guacamoles SSH-Modul akzeptiert den Schluesselinhalt inline).
SSH_KEY_DIR="${STATE_DIR}/guacamole-ssh-key"
mkdir -p "${SSH_KEY_DIR}"
if [ ! -s "${SSH_KEY_DIR}/id_ed25519" ]; then
  ssh-keygen -t ed25519 -f "${SSH_KEY_DIR}/id_ed25519" -N "" -C "guacamole@irl-streamer-os" -q
fi
mkdir -p "${HOME_DIR}/.ssh"
touch "${HOME_DIR}/.ssh/authorized_keys"
if ! grep -qF "$(cat "${SSH_KEY_DIR}/id_ed25519.pub")" "${HOME_DIR}/.ssh/authorized_keys" 2>/dev/null; then
  cat "${SSH_KEY_DIR}/id_ed25519.pub" >> "${HOME_DIR}/.ssh/authorized_keys"
fi
chmod 700 "${HOME_DIR}/.ssh"
chmod 600 "${HOME_DIR}/.ssh/authorized_keys"

# RDP-Zugangsdaten fuer GNOME Remote Desktop (siehe grdctl-Einrichtung
# weiter unten) - hier schon generiert, damit die Datenbank-Verbindung das
# direkt mit anlegen kann.
RDP_PASS_FILE="${STATE_DIR}/rdp-password.txt"
if [ ! -s "${RDP_PASS_FILE}" ]; then
  generate_readable_password 20 > "${RDP_PASS_FILE}"
fi
RDP_PASSWORD="$(cat "${RDP_PASS_FILE}")"

GUAC_DIR="${PROJECT_DIR}/docker/guacamole"
mkdir -p "${GUAC_DIR}/state"
# PostgreSQL-DB-Passwort (getrennt vom Guacamole-Login-Passwort oben) -
# Postgres liest es ueber POSTGRES_PASSWORD_FILE, Guacamole selbst ueber
# POSTGRESQL_PASSWORD_FILE, siehe docker-compose.yml.
GUAC_DB_PASS_FILE="${GUAC_DIR}/state/db-password.txt"
if [ ! -s "${GUAC_DB_PASS_FILE}" ]; then
  generate_readable_password 24 > "${GUAC_DB_PASS_FILE}"
fi
# WICHTIG: 644, nicht 600 - die Datei wird per Bind-Mount (kein echtes
# Docker-Secret) in die Container guacamole-db (Postgres, meist root) UND
# guacamole (laeuft als uid 1001 "guacamole") eingehaengt. Mit 600/root
# kann der guacamole-Container die Datei nicht lesen -> jeder Login
# schlaegt mit "Unexpected internal error" fehl und sperrt die Client-IP
# nach 5 Fehlversuchen (Bugfund 2026-08-31).
chmod 644 "${GUAC_DB_PASS_FILE}"

log "Starte Docker-Compose-Stack (Belabox-Receiver + IRL-Diagnostics + Caddy-HTTPS-Proxy + Guacamole+PostgreSQL)"
progress 7 "Docker-Container werden gebaut und gestartet (dauert etwas)..."
usermod -aG docker "${TARGET_USER}" || true
# Kein dyndns.caddy mehr anzulegen (Umbau auf reines Relay-only-Modell,
# siehe Caddyfile-Kommentar dort) - es gibt keinen kundenseitig
# konfigurierten Hostnamen mehr, aller externer Zugriff laeuft ueber den
# Relay-Server.
docker compose -f "${PROJECT_DIR}/docker/docker-compose.yml" up -d --build

# --- 6b. Guacamole-Admin-Nutzer + Verbindungen (SSH+RDP) in der Datenbank
#         anlegen (Nutzerwunsch 2026-08-31) -------------------------------
# Das Schema selbst (Tabellen) wird von Postgres automatisch beim ALLER-
# ERSTEN Start ueber docker-entrypoint-initdb.d eingelesen (siehe
# docker/guacamole/postgresql-init/001-create-schema.sql, unveraendert aus
# dem offiziellen guacamole/guacamole-Image extrahiert). Admin-Nutzer und
# die beiden Standard-Verbindungen (SSH, RDP) werden HIER separat per SQL
# angelegt, NICHT als weiteres initdb.d-Skript - die Passwoerter werden ja
# erst zur Laufzeit generiert (siehe oben), muessen also nach dem
# Datenbank-Start eingespielt werden. Idempotent (ON CONFLICT DO NOTHING /
# Existenz-Check), schadet bei erneutem provision.sh-Lauf nicht.
log "Warte auf PostgreSQL-Datenbank und richte Guacamole-Admin-Nutzer/Verbindungen ein..."
progress 8 "Guacamole-Fernzugriff wird eingerichtet..."
for i in $(seq 1 30); do
  if docker exec guacamole-db pg_isready -U guacamole_user -d guacamole_db >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

python3 - "${SSH_KEY_DIR}/id_ed25519" <<PYEOF > /tmp/irl-streamer-guac-init.sql
import hashlib, os, sys, secrets

ssh_key_path = sys.argv[1]
with open(ssh_key_path) as f:
    ssh_private_key = f.read()

def sql_escape(s):
    return s.replace("'", "''")

def guac_password_hash(password: str):
    salt = secrets.token_bytes(32)
    salt_hex_upper = salt.hex().upper()
    digest = hashlib.sha256((password + salt_hex_upper).encode("utf-8")).hexdigest()
    return digest.upper(), salt_hex_upper

guac_password = "${GUAC_PASSWORD}"
rdp_password = "${RDP_PASSWORD}"
password_hash, password_salt = guac_password_hash(guac_password)

sql = f"""
-- Admin-Nutzer "streamer" (idempotent - loescht einen evtl. vorherigen
-- Lauf-Rest mit demselben Namen zuerst, damit ein neu generiertes
-- Passwort auch tatsaechlich greift statt beim alten Hash zu bleiben).
DELETE FROM guacamole_entity WHERE name = 'streamer' AND type = 'USER';

INSERT INTO guacamole_entity (name, type) VALUES ('streamer', 'USER');
INSERT INTO guacamole_user (entity_id, password_hash, password_salt, password_date)
SELECT entity_id, decode('{password_hash}', 'hex'), decode('{password_salt}', 'hex'), CURRENT_TIMESTAMP
FROM guacamole_entity WHERE name = 'streamer' AND type = 'USER';

INSERT INTO guacamole_system_permission (entity_id, permission)
SELECT entity_id, permission::guacamole_system_permission_type
FROM guacamole_entity, (VALUES ('CREATE_CONNECTION'), ('CREATE_CONNECTION_GROUP'), ('CREATE_SHARING_PROFILE'), ('CREATE_USER'), ('CREATE_USER_GROUP'), ('ADMINISTER')) AS perms(permission)
WHERE guacamole_entity.name = 'streamer' AND guacamole_entity.type = 'USER';

-- SSH-Verbindung (idempotent per DELETE+INSERT, gleiche Begruendung wie
-- oben - der SSH-Schluessel kann sich bei erneutem provision.sh-Lauf
-- theoretisch geaendert haben).
DELETE FROM guacamole_connection WHERE connection_name = 'SSH';
INSERT INTO guacamole_connection (connection_name, protocol) VALUES ('SSH', 'ssh');
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'hostname', 'localhost' FROM guacamole_connection WHERE connection_name = 'SSH';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'port', '22' FROM guacamole_connection WHERE connection_name = 'SSH';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'username', 'streamer' FROM guacamole_connection WHERE connection_name = 'SSH';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'private-key', '{sql_escape(ssh_private_key)}' FROM guacamole_connection WHERE connection_name = 'SSH';
INSERT INTO guacamole_connection_permission (entity_id, connection_id, permission)
SELECT entity_id, guacamole_connection.connection_id, 'READ'::guacamole_object_permission_type
FROM guacamole_entity, guacamole_connection
WHERE guacamole_entity.name = 'streamer' AND guacamole_entity.type = 'USER' AND guacamole_connection.connection_name = 'SSH';

-- RDP-Verbindung
DELETE FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection (connection_name, protocol) VALUES ('Desktop (RDP)', 'rdp');
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'hostname', 'localhost' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'port', '3389' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'username', 'streamer' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'password', '{sql_escape(rdp_password)}' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'security', 'any' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'ignore-cert', 'true' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
-- Tastaturlayout + Zeitzone fest auf Deutsch/Berlin (Nutzerwunsch 2026-09-01):
-- ohne server-layout nutzt Guacamole seinen Standard-Keymap (en-us-qwerty),
-- was am RDP-Server ein vertauschtes Y/Z ergibt, weil physische Tastatur
-- (QWERTZ) und vom Server erwartetes Layout (QWERTY) auseinanderlaufen -
-- "de-de-qwertz" ist der offizielle Guacamole-RDP-Parameterwert dafuer
-- (siehe guacamole-server/src/protocols/rdp/keymaps/de_de_qwertz.keymap).
-- "timezone" im IANA-Format wird von guacd automatisch in die Windows-
-- Zeitzone uebersetzt und als TZ-Umgebungsvariable an die RDP-Sitzung
-- durchgereicht (GUACAMOLE-422).
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'server-layout', 'de-de-qwertz' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection_parameter (connection_id, parameter_name, parameter_value)
SELECT connection_id, 'timezone', 'Europe/Berlin' FROM guacamole_connection WHERE connection_name = 'Desktop (RDP)';
INSERT INTO guacamole_connection_permission (entity_id, connection_id, permission)
SELECT entity_id, guacamole_connection.connection_id, 'READ'::guacamole_object_permission_type
FROM guacamole_entity, guacamole_connection
WHERE guacamole_entity.name = 'streamer' AND guacamole_entity.type = 'USER' AND guacamole_connection.connection_name = 'Desktop (RDP)';
"""
print(sql)
PYEOF

if [ -s /tmp/irl-streamer-guac-init.sql ]; then
  docker exec -i guacamole-db psql -U guacamole_user -d guacamole_db < /tmp/irl-streamer-guac-init.sql     && log "Guacamole-Admin-Nutzer 'streamer' + SSH/RDP-Verbindungen angelegt."     || log "WARNUNG: Guacamole-Datenbank-Einrichtung fehlgeschlagen - Verbindungen muessen manuell in der Weboberflaeche angelegt werden."
  rm -f /tmp/irl-streamer-guac-init.sql
fi

# --- 6a. Caddys selbstsigniertes Root-Zertifikat lokal vertrauenswuerdig
#         machen (Nutzerwunsch 2026-08-31) --------------------------------
# Web Bluetooth (DJI-Kamera-Anbindung, siehe static/dji-ble-test.html)
# verlangt zwingend einen "sicheren Kontext" (HTTPS oder localhost). Das
# Dashboard wird sowohl direkt auf DIESEM Mini-PC als auch von Handys/
# Tablets im Netzwerk aus geoeffnet - "localhost" greift nur lokal, daher
# der Caddy-HTTPS-Reverse-Proxy (siehe docker/caddy/) mit selbstsigniertem
# Zertifikat. Fuer DIESEN Mini-PC importieren wir das Root-Zertifikat
# gleich automatisch in Chromes/Ubuntus NSS-Zertifikatsspeicher, damit der
# Nutzer hier keinen manuellen Schritt braucht - auf jedem WEITEREN Geraet
# (Handy/Tablet) bleibt der einmalige manuelle Import noetig (siehe
# INSTALLATION.md), da wir dort keinen Zugriff auf den Zertifikatsspeicher
# haben.
log "Warte auf Caddys selbstsignierte Root-CA und importiere sie lokal..."
progress 9 "HTTPS-Zertifikat wird eingerichtet..."
CADDY_ROOT_CA=""
for i in $(seq 1 30); do
  # BUGFIX (05.09., live gefunden - Chrome zeigt IMMER "unsichere
  # Verbindung" beim Diagnose-Dashboard): root-ca.crt liegt laut Caddyfile
  # unter dem HTTP-Katch-all-Port 5003 (siehe docker/caddy/Caddyfile,
  # ":5003 { handle /root-ca.crt ... }"), NICHT auf dem Standard-Port 80 -
  # dieser curl-Aufruf schlug dadurch bei JEDER Installation fehl (30x2s
  # Timeout), der Import lief nie, das Root-Zertifikat landete nie in
  # Chromes NSS-Datenbank.
  if curl -fsS --max-time 2 http://localhost:5003/root-ca.crt -o /tmp/irl-streamer-root-ca.crt 2>/dev/null       && [ -s /tmp/irl-streamer-root-ca.crt ]; then
    CADDY_ROOT_CA="/tmp/irl-streamer-root-ca.crt"
    break
  fi
  sleep 2
done
if [ -n "${CADDY_ROOT_CA}" ]; then
  if ! command -v certutil >/dev/null 2>&1; then
    apt-get install -y libnss3-tools >/dev/null 2>&1 || true
  fi
  if command -v certutil >/dev/null 2>&1; then
    NSS_DB="${HOME_DIR}/.pki/nssdb"
    sudo -u "${TARGET_USER}" mkdir -p "${NSS_DB}"
    if [ ! -f "${NSS_DB}/cert9.db" ]; then
      sudo -u "${TARGET_USER}" certutil -N -d "sql:${NSS_DB}" --empty-password
    fi
    sudo -u "${TARGET_USER}" certutil -D -n "IRL Streamer OS (lokal)" -d "sql:${NSS_DB}" 2>/dev/null || true
    sudo -u "${TARGET_USER}" certutil -A -n "IRL Streamer OS (lokal)" -t "C,,"       -i "${CADDY_ROOT_CA}" -d "sql:${NSS_DB}"
    log "Root-Zertifikat lokal in Chrome/Chromium importiert - HTTPS-Dashboard zeigt auf diesem Mini-PC direkt das Schloss-Symbol."
  else
    log "WARNUNG: certutil nicht verfuegbar - Root-Zertifikat muss manuell importiert werden (siehe INSTALLATION.md)."
  fi
else
  log "WARNUNG: Caddys Root-Zertifikat konnte nicht abgerufen werden (Timeout) - HTTPS-Zertifikat muss manuell importiert werden."
fi

# Firefox entfernen + Chrome als einzigen Browser sicherstellen (Nutzerwunsch
# 2026-09-05, Fix 2026-09-06): Ubuntu 26.04 Desktop bringt Firefox als Snap
# vorinstalliert mit, zusaetzlich existiert ein leeres, transitionales APT-
# Paket "firefox", dessen postinst-Skript bei JEDER weiteren apt-Operation
# (Trigger-Verarbeitung, z.B. durch die apt-get install-Aufrufe fuer
# OBS/VA-API/Treiber weiter oben in diesem Skript) den Snap automatisch neu
# installiert, wenn er fehlt. Der fruehere Entfernungsversuch stand VOR
# diesen apt-get install-Aufrufen und wurde dadurch live wieder rueckgaengig
# gemacht (Firefox tauchte nach vollstaendigem provision.sh-Lauf erneut auf).
# Fix: dieser Block steht jetzt bewusst als LETZTE Paket-Operation im
# gesamten Skript (nach allen anderen apt-get install-Aufrufen), UND
# entfernt zusaetzlich das transitionale apt-Paket selbst, nicht nur den
# Snap - erst beides zusammen verhindert die automatische Neuinstallation.
if snap list firefox >/dev/null 2>&1; then
  log "Entferne vorinstalliertes Firefox (Snap) - Chrome ist der einzige benoetigte Browser"
  snap remove firefox >/dev/null 2>&1 || true
fi
if dpkg -s firefox >/dev/null 2>&1; then
  log "Entferne transitionales Firefox-APT-Paket (verhindert automatische Snap-Neuinstallation)"
  apt-get purge -y firefox >/dev/null 2>&1 || true
fi

# --- 7. GNOME Remote Desktop (RDP) fuer Guacamole - ersetzt x11vnc ---------
# x11vnc/xrandr sind unter Wayland kategorisch unmoeglich (Ubuntu 26.04 hat
# die Xorg-Session komplett entfernt, live verifiziert 2026-08-24). GNOMEs
# eigener Remote-Desktop-Daemon (Paket "gnome-remote-desktop", Kommandozeile
# "grdctl") bietet einen nativen Wayland-RDP-Server, den Guacamole direkt als
# RDP-Verbindung nutzen kann - inkl. funktionierender Zwischenablage.
#
# NUTZER-SITZUNGS-MODUS statt "--system" (Kurskorrektur, 2026-08-25): der
# "--system"-Modus ist fuer Remote-LOGIN gedacht und erzeugt bei jeder RDP-
# Verbindung eine EIGENE, neue Sitzung - live bestaetigt per "loginctl
# list-sessions" waehrend eine Guacamole-RDP-Verbindung aktiv war (zusaetzlich
# zur physischen Autologin-Sitzung, in der OBS laeuft). Der Nutzer-Modus
# dagegen TEILT die bereits laufende, physische Sitzung (wie eine VNC-
# Bildschirmfreigabe) - live bestaetigt, dass darin dasselbe laufende OBS
# sichtbar ist und nichts neu gestartet wird. Braucht dafuer eine aktive
# Sitzung mit eigenem D-Bus, die provision.sh (laeuft als root vor jedem
# Login) noch nicht hat - deshalb per Autostart-Skript ausgelagert, das bei
# jedem grafischen Login (erneut) konfiguriert, nicht nur einmalig (die
# grdctl-Einstellungen sind zwar an sich dconf-persistent, aber ein
# jedes-Mal-neu-Anwenden ist robuster als sich auf unklare Persistenz zu
# verlassen - gleiches Muster wie beim Aufloesungs-Skript oben).
log "Richte GNOME Remote Desktop (RDP, Nutzer-Sitzungs-Modus) fuer Guacamole ein"
progress 10 "Fernzugriff (RDP) wird eingerichtet..."
# RDP_PASSWORD/RDP_PASS_FILE wurden weiter oben (bei der Guacamole-Datenbank-
# Erzeugung) bereits generiert bzw. eingelesen - hier weiterverwendet.

# --- 7a. Login-Schluesselbund fuer Autologin vorbereiten --------------------
# Bugfund 2026-09-01: Bei Autologin (kein Passwort wird je eingegeben) kann
# PAM den Login-Schluesselbund nie automatisch mit einem Passwort anlegen/
# entsperren. Jeder Versuch, ein Secret zu speichern (v.a. "grdctl rdp
# set-credentials" gleich unten), loeste dadurch einen GUI-Passwort-Dialog
# aus, der bei einem unbeaufsichtigten Kiosk-Geraet NIE beantwortet wird -
# das Geraet blieb dauerhaft ohne funktionierendes RDP/Guacamole zurueck UND
# der Dialog erschien bei jedem Neustart erneut ("Meldung poppt immer wieder
# auf" - Nutzerbeschreibung). Das eigentliche Skript (siehe
# irl-keyring-setup.sh) legt EINMALIG einen leeren, unverschluesselten
# Login-Schluesselbund per interner D-Bus-Methode an - OHNE jeden Prompt,
# live verifiziert (2026-09-01) auf dem Testgeraet. Idempotent: tut nichts,
# wenn bereits ein Schluesselbund existiert.
KEYRING_SETUP_SCRIPT="${HOME_DIR}/.local/bin/irl-streamer-keyring-setup.sh"
cp "${PROJECT_DIR}/provision/irl-keyring-setup.sh" "${KEYRING_SETUP_SCRIPT}"
chmod +x "${KEYRING_SETUP_SCRIPT}"
chown "${TARGET_USER}:${TARGET_USER}" "${KEYRING_SETUP_SCRIPT}"

# Sicherstellen, dass der SYSTEM-Daemon (per Ubuntu-Preset standardmaessig
# aktiv) kein RDP auf Port 3389 offen haelt - sonst Portkonflikt mit dem
# gleich folgenden Nutzer-Modus.
timeout 10 grdctl --system rdp disable 2>/dev/null || true

RDP_TLS_DIR="${HOME_DIR}/.local/state/rdp-tls"
mkdir -p "${RDP_TLS_DIR}"
if [ ! -s "${RDP_TLS_DIR}/rdp.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "${RDP_TLS_DIR}/rdp.key" -out "${RDP_TLS_DIR}/rdp.crt" \
    -subj "/CN=irl-streamer-os"
fi
chmod 600 "${RDP_TLS_DIR}/rdp.key"
chown -R "${TARGET_USER}:${TARGET_USER}" "${RDP_TLS_DIR}"

RDP_SETUP_SCRIPT="${HOME_DIR}/.local/bin/irl-streamer-set-rdp.sh"
cat > "${RDP_SETUP_SCRIPT}" <<EOF
#!/usr/bin/env bash
# Login-Schluesselbund ZUERST einrichten (siehe Kommentar oben) - muss vor
# jedem grdctl-Aufruf gelaufen sein, egal welche Reihenfolge GNOME fuer
# mehrere Autostart-Eintraege tatsaechlich waehlt. Idempotent/schnell,
# wenn bereits eingerichtet (Normalfall ab dem zweiten Login).
"${HOME_DIR}/.local/bin/irl-streamer-keyring-setup.sh" || true

# Wiederholungsschleife mit Verifikation statt Einmalversuch - live gefunden
# (2026-08-25 auf der Test-VM): direkt beim Login greifen die grdctl-
# Passwort-Aenderungen manchmal nicht (vermutlich Keyring/Secret-Service
# noch nicht bereit), obwohl Port/Zertifikat-Pfade sofort korrekt uebernommen
# werden - "grdctl status" zeigte danach dauerhaft das ALTE Passwort, und der
# Port war trotz "Status: enabled" gar nicht erreichbar. Ein Dienst-Neustart
# je Versuch erzwingt ein sauberes Neueinlesen der kompletten Konfiguration.
#
# WICHTIG (Bugfund 2026-08-31): "grdctl rdp set-credentials" speichert das
# Passwort ueber libsecret im GNOME-Keyring. Ist der Keyring aus irgendeinem
# Grund gesperrt (z.B. ungewoehnlicher Login-Ablauf), HAENGT dieser Aufruf
# unbegrenzt fest, statt einen Fehler zurueckzugeben - live per SSH bestae-
# tigt (grdctl blockierte >30s ohne jede Fehlermeldung). Ohne gesetztes
# Passwort bleibt RDP auf "Username/Password: (null)" stehen und jede
# Verbindung wird mit "Server refused connection" abgewiesen. Jeder
# grdctl-Aufruf laeuft daher jetzt mit einem 10s-Timeout - haengt einer fest,
# bricht die Schleife den Versuch ab und startet nach 2s Pause den naechsten,
# statt das gesamte Autostart-Skript (und damit den Login) zu blockieren.
systemctl --user enable --now gnome-remote-desktop.service 2>/dev/null || true
for i in \$(seq 1 15); do
  timeout 10 grdctl rdp set-port 3389 2>/dev/null
  timeout 10 grdctl rdp set-tls-cert "${RDP_TLS_DIR}/rdp.crt" 2>/dev/null
  timeout 10 grdctl rdp set-tls-key "${RDP_TLS_DIR}/rdp.key" 2>/dev/null
  timeout 10 grdctl rdp set-credentials "${TARGET_USER}" "${RDP_PASSWORD}" 2>/dev/null
  timeout 10 grdctl rdp set-auth-methods credentials 2>/dev/null
  timeout 10 grdctl rdp disable-view-only 2>/dev/null
  timeout 10 grdctl rdp enable 2>/dev/null
  systemctl --user restart gnome-remote-desktop.service 2>/dev/null
  sleep 2
  if timeout 10 grdctl status --show-credentials 2>/dev/null | grep -q "Password: ${RDP_PASSWORD}" \\
     && ss -tln 2>/dev/null | grep -q ':3389 '; then
    exit 0
  fi
done
EOF
chmod +x "${RDP_SETUP_SCRIPT}"

cat > "${HOME_DIR}/.config/autostart/irl-streamer-rdp.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=IRL Streamer RDP-Freigabe
Exec=${RDP_SETUP_SCRIPT}
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF

# --- 8. Desktop-Icon fuer das Diagnose-Dashboard ----------------------------
progress 11 "Desktop-Verknuepfungen werden angelegt..."
mkdir -p "${HOME_DIR}/Desktop"
# Icon-Datei nach /opt (nicht mehr lose auf dem Desktop, Nutzerwunsch
# 2026-08-25) - .desktop-Dateien duerfen als "Icon=" einen absoluten Pfad
# verwenden, muessen also nicht im Icon-Theme oder direkt neben der
# .desktop-Datei liegen.
mkdir -p "${PROJECT_DIR}/icons"
cp "${PROJECT_DIR}/docker/irl-diagnostics-src/static/icon-192.png" "${PROJECT_DIR}/icons/irl-diagnostics.png" 2>/dev/null || true
cp "${PROJECT_DIR}/docker/guacamole/guacamole-icon.svg" "${PROJECT_DIR}/icons/guacamole.svg" 2>/dev/null || true
cp "${PROJECT_DIR}/provision/assets/belabox-icon.png" "${PROJECT_DIR}/icons/belabox.png" 2>/dev/null || true
rm -f "${HOME_DIR}/Desktop/irl-diagnostics-icon.png"
cat > "${HOME_DIR}/Desktop/IRL-Diagnostics.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=4. IRL Diagnostic Tool
Comment=Diagnose-Dashboard fuer den IRL-Stream oeffnen
Exec=xdg-open https://localhost:5002/diagnostic/
Icon=${PROJECT_DIR}/icons/irl-diagnostics.png
Terminal=false
Categories=Network;
EOF
chmod +x "${HOME_DIR}/Desktop/IRL-Diagnostics.desktop"

# OBS-Verknuepfung auf dem Desktop (auf Nutzerwunsch, 2026-08-24) - nutzt
# denselben Wrapper wie der Autostart, damit Doppelklick auf dem Desktop
# dieselbe Aufloesungs-/Szenen-Logik bekommt wie der automatische Start.
#
# KRITISCHER BUGFIX (03.09.): Icon-Pfad NICHT mehr hart kodiert - das
# alte PPA-Paket installierte das Icon unter /usr/share/icons/..., das
# neue offizielle .deb (siehe OBS-32.2.2-Umstieg oben) installiert es
# stattdessen unter /usr/local/share/icons/... - ein fest verdrahteter
# Pfad zeigt nach dem Versions-/Paketquellenwechsel ins Leere und das
# Icon verschwindet vom Desktop (live gefunden). Stattdessen zur Laufzeit
# nach der tatsaechlich vorhandenen Icon-Datei suchen (erste passende
# Instanz gewinnt), robust gegen kuenftige Paketquellenwechsel.
OBS_ICON_PATH="$(find /usr/share/icons /usr/local/share/icons -iname 'com.obsproject.Studio.png' 2>/dev/null | head -1)"
OBS_ICON_PATH="${OBS_ICON_PATH:-/usr/share/icons/hicolor/256x256/apps/com.obsproject.Studio.png}"
cat > "${HOME_DIR}/Desktop/OBS-Studio.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=6. OBS Studio
Comment=OBS Studio oeffnen (mit IRL-Streamer-Szenen)
Exec=${LAUNCH_SCRIPT}
Icon=${OBS_ICON_PATH}
Terminal=false
Categories=AudioVideo;
EOF
chmod +x "${HOME_DIR}/Desktop/OBS-Studio.desktop"

# Chrome-Verknuepfung (fuer Twitch-Login/Streamkey - siehe Kommentar bei den
# apt-sources oben, OBS' eigener CEF-Browser wird von Twitch abgelehnt).
cat > "${HOME_DIR}/Desktop/Google-Chrome.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=1. Google Chrome
Comment=Fuer Twitch-Login/Streamkey (OBS' eingebauter Browser wird von Twitch abgelehnt)
Exec=google-chrome-stable
Icon=/opt/google/chrome/product_logo_256.png
Terminal=false
Categories=Network;
EOF
chmod +x "${HOME_DIR}/Desktop/Google-Chrome.desktop"

# Guacamole-Verknuepfung (Phase 3) - SSH+VNC-Fernzugriff gebuendelt.
# Offizielles Apache-Guacamole-Logo (guac-tricolor-logo.svg, aus dem
# oeffentlichen apache/guacamole-website-Repo, 2026-08-25 geladen und ins
# Projekt unter docker/guacamole/ uebernommen) statt eines generischen
# System-Icons. GTK/GNOME rendert .desktop-Icons als SVG-Pfad direkt, keine
# Konvertierung noetig.
cat > "${HOME_DIR}/Desktop/Guacamole.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=5. Fernzugriff - Server
Comment=SSH/VNC-Fernzugriff auf dieses Geraet oeffnen
Exec=xdg-open https://localhost:5002/guacamole/
Icon=${PROJECT_DIR}/icons/guacamole.svg
Terminal=false
Categories=Network;
EOF
chmod +x "${HOME_DIR}/Desktop/Guacamole.desktop"

# Fernzugriff-Verknuepfung (optional, Nutzerwunsch 2026-08-25) - im
# Gegensatz zu den anderen Icons hier bewusst NICHT selbstloeschend nach
# Erfolg (siehe irl-streamer-fernzugriff-einrichten.sh): das ist eine
# optionale, jederzeit wiederholbare Aktion, kein einmaliger Setup-Schritt.
#
# HAERTUNG/UX-Fix (Nutzerwunsch 2026-09-09): das sichtbare Terminal-Fenster
# (Terminal=true) plus abschliessendes "mit Enter schliessen" wurde als
# stoerend empfunden - seit der automatischen Belabox-Erkennung (siehe
# belabox-discover.sh) laeuft JEDER erfolgs-/fehlerrelevante Schritt bereits
# ueber sichtbare Zenity-Dialoge (fail_dialog() bei jedem Fehlerpfad, ein
# abschliessender Erfolgs-/Warnungs-Dialog am Ende) - das Terminal-Fenster
# selbst zeigt nur noch redundante log()-Zeilen, die kein Nutzer mehr lesen
# muss. Terminal=false versteckt das Fenster komplett, das Skript laeuft
# im Hintergrund und kommuniziert ausschliesslich per Zenity mit dem Nutzer.
cat > "${HOME_DIR}/Desktop/IRL-Streamer-OS-Fernzugriff-einrichten.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=2. VPN - Belabox
Comment=Verbindet dieses Geraet per WireGuard mit der Belabox, egal an welchem Internetrouter sie haengt
Exec=sudo /usr/bin/bash ${PROJECT_DIR}/provision/irl-streamer-fernzugriff-einrichten.sh
Icon=network-vpn
Terminal=false
Categories=Network;
EOF
chmod +x "${HOME_DIR}/Desktop/IRL-Streamer-OS-Fernzugriff-einrichten.desktop"

# BelaUI-Verknuepfung (Nutzerwunsch 03.09.): oeffnet die Weboberflaeche der
# Belabox direkt ueber die feste WireGuard-Tunnel-Adresse (10.10.10.2,
# siehe BELABOX_HOST in irl-diagnostics-src/main.py). Bewusst NICHT ueber
# die Caddy-/belabox/-Route (siehe Caddyfile-Kommentar zur :5003-Ausnahme -
# BelaUIs eigenes Frontend-JS verbindet sich fest per Klartext-ws://, ueber
# HTTPS geladen waere die WebSocket-Verbindung als Mixed-Content geblockt),
# sondern DIREKT per HTTP auf die Tunnel-IP - funktioniert nur lokal auf
# diesem Mini-PC selbst bzw. per Guacamole-Fernzugriff darauf, nicht von
# aussen (das ist auch der Zweck: dieses Icon liegt auf dem Mini-PC-
# Desktop, wird also nur dort angeklickt, wo der Tunnel direkt erreichbar
# ist).
cat > "${HOME_DIR}/Desktop/IRL-Streamer-OS-Belabox-GUI.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Belabox - Nur mit VPN
Comment=Oeffnet die Weboberflaeche der Belabox ueber den WireGuard-Tunnel
Exec=xdg-open http://10.10.10.2:80
Icon=${PROJECT_DIR}/icons/belabox.png
Terminal=false
Categories=Network;
EOF
chmod +x "${HOME_DIR}/Desktop/IRL-Streamer-OS-Belabox-GUI.desktop"

# --- 8b. Lizenz-Icon + taeglicher Ablauf-Check (systemd-Timer) -------------
# Desktop-Icon "Lizenz aktivieren" - ruft den Zenity-Dialog auf, der
# license-client.sh activate im Hintergrund aufruft (siehe
# provision/licensing/irl-streamer-license-activate.sh). "sudo" darin
# braucht KEIN Passwort, weil die license.json-Zustandsdatei sonst nicht
# von "streamer" beschreibbar waere (liegt unter root:root /opt/irl-streamer-os).
# WICHTIG: dieser Block steht bewusst VOR der "vertrauenswuerdig
# markieren"-Schleife unten, damit auch DIESES neue Icon vom generischen
# Trust-Fix erfasst wird statt mit rotem X zu bleiben.
cat > /etc/sudoers.d/irl-streamer-license <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/bash ${PROJECT_DIR}/provision/licensing/license-client.sh activate *
EOF
chmod 440 /etc/sudoers.d/irl-streamer-license

# BUGFIX (05.09., live gefunden - PermissionError beim OBS-Autostart):
# eigener sudoers-Eintrag NUR fuer license-guard.sh (siehe Kommentar im
# OBS-Launch-Wrapper oben) - der Lock-Check vor jedem OBS-Start braucht
# root, um den root-only-geschuetzten Lizenz-Tresor lesen zu koennen.
cat > /etc/sudoers.d/irl-streamer-license-guard <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/bash ${PROJECT_DIR}/provision/licensing/license-guard.sh
EOF
chmod 440 /etc/sudoers.d/irl-streamer-license-guard

# HAERTUNG (Nutzerwunsch 2026-09-09, gehoert zum Terminal=false-Fix oben):
# ohne aktive/"warme" sudo-Session braeuchte "sudo" im .desktop-Icon ein TTY
# fuer die Passwort-Abfrage - das gibt es bei Terminal=false nicht, das
# Skript wuerde beim Icon-Klick lautlos fehlschlagen. Dedizierter NOPASSWD-
# Eintrag NUR fuer dieses eine Skript (gleiches Muster wie license-guard.sh/
# license-client.sh oben) macht den Klick zuverlaessig, ohne generell
# passwortlosen root-Zugriff fuer den TARGET_USER zu eroeffnen.
cat > /etc/sudoers.d/irl-streamer-fernzugriff <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/bash ${PROJECT_DIR}/provision/irl-streamer-fernzugriff-einrichten.sh
EOF
chmod 440 /etc/sudoers.d/irl-streamer-fernzugriff

cat > "${HOME_DIR}/Desktop/IRL-Streamer-OS-Lizenz-aktivieren.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Streamer OS Lizenz eingeben
Comment=Aktivierungscode eingeben, den du per E-Mail erhalten hast
Exec=bash ${PROJECT_DIR}/provision/licensing/irl-streamer-license-activate.sh
Icon=dialog-password
Terminal=false
Categories=System;
EOF
chmod +x "${HOME_DIR}/Desktop/IRL-Streamer-OS-Lizenz-aktivieren.desktop"

# Ablauf-/Warn-Check per systemd-Timer (nicht per cron - dieses Projekt
# nutzt durchgehend systemd-Units, siehe irl-streamer-provision.service
# selbst). Intervall bewusst kurz (5 Minuten, Nutzerentscheidung 03.09.,
# vorher 4h): eine Laufzeit-Aenderung in der Lizenzverwaltung soll zeitnah
# auf dem Kunden-Desktop sichtbar werden, ohne dass ein manueller
# "systemctl start irl-license-check.service" noetig ist. OnBootSec sorgt
# zusaetzlich fuer eine Pruefung kurz nach jedem Neustart, nicht erst nach
# Ablauf des ersten Intervalls - relevant, weil ein IRL-Streaming-Mini-PC
# oft laengere Zeit ausgeschaltet ist.
# Bewusst UNAUFFAELLIG benannt (Nutzerwunsch 2026-09-05, Haertung gegen
# einfache Lizenzumgehung): ein Name wie "irl-license-check" verraet per
# "systemctl list-timers" oder "systemctl list-units" sofort, wonach ein
# Kunde suchen muesste, um die Lizenzpruefung gezielt zu deaktivieren
# (systemctl disable/mask). "irl-sysmaint-check" klingt nach generischer
# Systempflege und faellt in einer laengeren Unit-Liste nicht besonders
# auf. Das ist AUSDRUECKLICH keine Verschluesselung/echter Schutz (siehe
# Projekt-README: keine perfekte DRM-Loesung) - nur eine kleine Huerde
# gegen den durchschnittlichen, nicht besonders technisch versierten
# Kunden, der gezielt nach "license"/"lizenz" suchen wuerde. Interne
# Dateinamen/Variablen (license-daily-check.sh, license-locked etc.)
# bleiben unveraendert - nur der von aussen sichtbare systemd-Unit-Name
# aendert sich.
cat > /etc/systemd/system/irl-sysmaint-check.service <<EOF
[Unit]
Description=IRL Streamer OS - Systempflege-Check

[Service]
Type=oneshot
ExecStart=/usr/bin/bash ${PROJECT_DIR}/provision/licensing/license-daily-check.sh
EOF

cat > /etc/systemd/system/irl-sysmaint-check.timer <<EOF
[Unit]
Description=IRL Streamer OS - Systempflege-Check (Timer)

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now irl-sysmaint-check.timer

# --- 8b0. Update-Check gegen das oeffentliche Release-Repo -----------------
# (Nutzerwunsch 2026-09-09): taeglicher Timer (analog zum Lizenz-Check-
# Muster oben), prueft die VERSION-Datei im oeffentlichen
# irl-streamer-os-releases-Repo gegen die lokal installierte Version (siehe
# provision/irl-streamer-update-check.sh fuer die volle Erklaerung des
# Zwei-Repo-Modells + der Versionssprung-Sicherheit). Bewusst als
# EIGENSTAENDIGER Timer (nicht in den bestehenden irl-sysmaint-check.timer
# integriert) - unterschiedliche Zwecke (Lizenz vs. Software-Update),
# unterschiedliche mentale Modelle, sollen unabhaengig voneinander
# fehlschlagen/deaktiviert werden koennen, ohne sich gegenseitig zu
# beeinflussen. Taeglich statt alle 5 Minuten (Nutzerentscheidung 09.09.):
# ein Software-Update ist kein zeitkritischer Sicherheits-/Sperrmechanismus
# wie die Lizenzpruefung, taeglich reicht voellig und vermeidet unnoetige
# GitHub-Anfragen.
cat > /etc/systemd/system/irl-streamer-update-check.service <<EOF
[Unit]
Description=IRL Streamer OS - Update-Check gegen oeffentliches Release-Repo

[Service]
Type=oneshot
ExecStart=/usr/bin/bash ${PROJECT_DIR}/provision/irl-streamer-update-check.sh
EOF

cat > /etc/systemd/system/irl-streamer-update-check.timer <<EOF
[Unit]
Description=IRL Streamer OS - Update-Check (Timer, taeglich)

[Timer]
OnBootSec=5min
OnUnitActiveSec=1d
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now irl-streamer-update-check.timer

# --- 8b1b. Schneller Wächter gegen manuelles "docker start" waehrend Sperre --
# Haertung (Nutzerwunsch 2026-09-05): der obige 5-Minuten-Timer prueft die
# Lizenz-SIGNATUR (braucht Python/Netzwerk, bewusst nicht zu oft). Ein
# Kunde mit sudo koennte in der Luecke dazwischen aber einfach manuell
# "docker start guacamole" (o.ae.) tippen, sobald er sieht dass ein
# Dienst gesperrt wurde - der Dienst liefe dann bis zu 5 Minuten normal
# weiter. Dieser zweite, unabhaengige Waechter braucht dagegen NUR die
# Existenz der Sperr-Markierung (license-service-lock.sh, kein Python/
# Netzwerk noetig) und laeuft dauerhaft im Hintergrund mit kurzem
# Intervall - stoppt einen manuell gestarteten Dienst binnen Sekunden
# wieder, statt bis zum naechsten 5-Minuten-Lauf zu warten. Bewusst
# ebenfalls unauffaellig benannt.
cat > /etc/systemd/system/irl-sysmaint-guard.service <<EOF
[Unit]
Description=IRL Streamer OS - Systempflege-Waechter

[Service]
Type=simple
ExecStart=/usr/bin/bash ${PROJECT_DIR}/provision/licensing/license-guard-daemon.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now irl-sysmaint-guard.service

# --- 8b2. Lizenz-/Testphasen-Anzeige unten rechts auf dem Desktop ----------
# GNOME-Shell-Erweiterung, die eine reine Textdatei periodisch anzeigt
# (Nutzerwunsch 2026-09-03): "Testversion noch XX Tage gültig" bzw.
# "Lizenz gültig bis TT.MM.JJJJ", je nach Zustand. Die eigentliche
# Lizenzlogik/Textgenerierung macht ausschliesslich license-widget-status.sh
# (aufgerufen von license-daily-check.sh + license-client.sh nach jeder
# Statusaenderung) - die Erweiterung selbst liest nur eine fertige Zeile.
log "Richte Lizenz-/Testphasen-Anzeige auf dem Desktop ein"
WIDGET_EXT_SRC="${PROJECT_DIR}/provision/assets/gnome-extensions/irl-license-widget@irlstreameros.de"
WIDGET_EXT_DEST="${HOME_DIR}/.local/share/gnome-shell/extensions/irl-license-widget@irlstreameros.de"
mkdir -p "${WIDGET_EXT_DEST}"
cp -r "${WIDGET_EXT_SRC}/"* "${WIDGET_EXT_DEST}/"
chown -R "${TARGET_USER}:${TARGET_USER}" "${HOME_DIR}/.local/share/gnome-shell"

# Einmalig eine erste Statuszeile erzeugen (Testphase wurde oben in Schritt
# 0b bereits gestartet), sonst zeigt die Erweiterung beim allerersten Login
# kurz gar nichts an, bis der taegliche Timer das erste Mal laeuft.
bash "${PROJECT_DIR}/provision/licensing/license-widget-status.sh" || true

# gnome-extensions enable traegt nur den dconf-Schluessel ein - GNOME Shell
# selbst uebernimmt neu hinzugekommene Erweiterungen unter Wayland erst bei
# der naechsten Anmeldung (kein Live-Scan moeglich, live verifiziert
# 2026-09-03). Voellig unproblematisch hier, weil provision.sh ohnehin vor
# dem ersten richtigen Login des Nutzers laeuft (frisches Autoinstall-Image).
sudo -u "${TARGET_USER}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "${TARGET_USER}")/bus" \
  dconf write /org/gnome/shell/enabled-extensions \
  "$(sudo -u "${TARGET_USER}" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "${TARGET_USER}")/bus" dconf read /org/gnome/shell/enabled-extensions 2>/dev/null | python3 -c "
import sys
raw = sys.stdin.read().strip()
uuid = 'irl-license-widget@irlstreameros.de'
if raw:
    items = [x.strip().strip(\"'\") for x in raw.strip('[]').split(',') if x.strip()]
else:
    items = []
if uuid not in items:
    items.append(uuid)
print('[' + ', '.join(\"'\" + i + \"'\" for i in items) + ']')
")" 2>/dev/null || log "WARNUNG: enabled-extensions per dconf konnte nicht vorab gesetzt werden - Nutzer muss die Erweiterung beim ersten Login einmalig manuell aktivieren (gnome-extensions enable irl-license-widget@irlstreameros.de)."

# ZUSAETZLICH systemweite dconf-Override-Datei (Nutzerwunsch 2026-09-04,
# Fehlerbild live verifiziert: der obige Live-dconf-write ueber D-Bus
# schlaegt waehrend der Autoinstall/Curtin-Phase praktisch IMMER fehl, weil
# in diesem Moment noch keine echte Benutzer-Session mit laufendem D-Bus
# existiert - der Fallback-Warnlog griff bisher regelmaessig, wodurch das
# Widget nach der Installation eingerichtet, aber NICHT eingeschaltet war
# und der Nutzer es manuell per 'gnome-extensions enable' aktivieren musste).
# Diese Methode braucht keine laufende Session (analog zum bestehenden
# Screensaver-/Tastatur-/Hintergrund-Muster weiter oben in diesem Skript) und
# ist deshalb die zuverlaessige Variante - der Live-Versuch oben bleibt als
# zusaetzlicher Weg bestehen, schadet aber nicht, falls er doch mal klappt.
#
# Gleichzeitig (Nutzerwunsch 2026-09-04): "persoenlicher Ordner"-Icon (Home)
# soll NICHT auf dem Desktop liegen (bleibt ueber Dock/Taskleiste weiter
# erreichbar - das betrifft nur die DING-Desktop-Icons-Einstellung, nicht
# die Anwendung selbst).
cat > /etc/dconf/db/local.d/04-irl-streamer-os-license-widget <<'EOF'
[org/gnome/shell]
enabled-extensions=['irl-license-widget@irlstreameros.de']

[org/gnome/shell/extensions/ding]
show-home=false
EOF
dconf update

# --- 8c. Relay-Tunnel: automatischer Aufbau + stuendliche Verifizierung
#         (Umbau auf reines Relay-only-Modell) -----------------------------
# JEDER Kunde bekommt automatisch einen WireGuard-Relay-Tunnel + eine
# generierte Subdomain <slug>.irlstreameros.de - das ist der einzige
# Zugriffsweg von aussen. Es gibt keinen lokalen Erreichbarkeits-Check
# (oeffentliche IP/UPnP/Portforward) mehr, siehe entfernte
# connectivity-checker.sh (Git-Historie) - der einzig relevante Zustand
# ist "Relay-Tunnel steht UND ist verifiziert" (Ampel rot/gruen, siehe
# docker/irl-diagnostics-src/main.py _check_connectivity()).
log "Richte automatischen Relay-Tunnel-Aufbau ein"
chmod +x "${PROJECT_DIR}/provision/irl-connectivity-report-client.sh" \
  "${PROJECT_DIR}/provision/systemd/irl-stream-active-check.sh" 2>/dev/null || true

# systemd-Einheiten aus dem Repo-Payload uebernehmen (liegen bereits fertig
# unter provision/systemd/, siehe Git-Historie) - stuendlicher Timer, der
# den Relay-Tunnel aufbaut/verifiziert.
install -m 0644 "${PROJECT_DIR}/provision/systemd/irl-connectivity-report.service" \
  /etc/systemd/system/irl-connectivity-report.service
install -m 0644 "${PROJECT_DIR}/provision/systemd/irl-connectivity-report.timer" \
  /etc/systemd/system/irl-connectivity-report.timer

systemctl daemon-reload
systemctl enable --now irl-connectivity-report.timer
# Einmal sofort ausfuehren, damit der Relay-Tunnel moeglichst schnell nach
# der Installation steht, statt bis zu einer Stunde auf den ersten
# Timer-Lauf zu warten.
bash "${PROJECT_DIR}/provision/irl-connectivity-report-client.sh" \
  || log "WARNUNG: erste Relay-Tunnel-Provisionierung meldete einen Fehler - der stuendliche Timer versucht es automatisch erneut."

# Desktop-Icons als "vertrauenswuerdig" markieren (Nutzerwunsch, 2026-08-25) -
# GNOME/Nautilus zeigt frisch erstellte .desktop-Dateien sonst mit rotem X
# an und verlangt pro Icon einen manuellen Rechtsklick > "Start erlauben".
# Nutzt dieselbe GVFS-Metadata-Markierung wie dieser Dialog, ueber die
# Session-Bus des eingeloggten Nutzers - die existiert bereits, weil dieses
# Skript per Doppelklick auf ein Desktop-Icon aus genau dieser Session
# heraus gestartet wurde.
STREAMER_UID="$(id -u "${TARGET_USER}")"
for f in "${HOME_DIR}/Desktop/"*.desktop; do
  [ -f "${f}" ] || continue
  # "true" als woertlicher String, NICHT "yes" - live gefunden (2026-08-25):
  # "gio set" kann fuer GVFS-Metadaten ohnehin nur Strings schreiben (kein
  # echter Boolean-Typ ueber die CLI), und Nautilus' Trust-Check vergleicht
  # den gespeicherten Wert woertlich mit dem String "true". "yes" sah in
  # "gio info" fast identisch aus, wurde aber NICHT als vertrauenswuerdig
  # erkannt (rotes X blieb bestehen).
  sudo -u "${TARGET_USER}" \
    XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
    gio set "${f}" "metadata::trusted" true 2>/dev/null \
    || log "WARNUNG: konnte ${f} nicht als vertrauenswuerdig markieren (Rechtsklick > Start erlauben behilft sich manuell)"
done

# Gut sichtbare Zugangsdaten-Datei auf dem Schreibtisch (Nutzerwunsch,
# 2026-08-25) - ohne die waeren die per Zufall generierten Passwoerter nur
# im Terminal-Output oder unter /opt/irl-streamer-os/state/*.txt zu finden,
# was eine fremde Person nicht kennt.
# Aktuelle lokale IP-Adresse ermitteln (fuer den Zugriff von anderen
# Geraeten wie Handy/Tablet im selben Netz - "localhost" funktioniert nur
# direkt auf diesem Mini-PC). Ueber die primaere Default-Route-Schnittstelle
# ermittelt, gleiches Muster wie beim eindeutigen Hostnamen weiter oben.
DESKTOP_IFACE="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
DESKTOP_IP="$(ip -4 -br addr show "${DESKTOP_IFACE}" 2>/dev/null | awk '{print $3}' | cut -d/ -f1)"
DESKTOP_IP="${DESKTOP_IP:-<IP-dieses-Mini-PCs>}"

cat > "${HOME_DIR}/Desktop/Zugangsdaten - keep safe.txt" <<EOF
IRL Streamer OS - Zugangsdaten
===============================

Alle Web-Dienste laufen gebuendelt ueber EINEN einzigen HTTPS-Port (5002,
Nutzerentscheidung 2026-09-02: nur ein Port muss am Router freigegeben
werden). Guacamole und das Diagnose-Dashboard sind ueber eigene Pfade
erreichbar; OBS-WebSocket liegt bewusst auf der Wurzel (viele
OBS-Steuer-Apps koennen nur "Host"+"Port" eingeben, keinen eigenen Pfad).

Diagnose-Dashboard: https://localhost:5002/diagnostic/ (auf diesem Mini-PC)
                     https://${DESKTOP_IP}:5002/diagnostic/ (von anderen Geraeten im selben Netz, z.B. Handy/Tablet)
  Benutzer: streamer
  Passwort: ${DASHBOARD_PASSWORD}
  HTTPS noetig fuer die DJI-Kamera-Anbindung (Web Bluetooth) - auf diesem
  Mini-PC ist das Zertifikat schon automatisch vertrauenswuerdig gemacht
  worden. Auf JEDEM WEITEREN Geraet (Handy/Tablet) einmalig
  https://${DESKTOP_IP}:5002/root-ca.crt herunterladen und als vertrauenswuerdig
  importieren (siehe Installationsanleitung, Abschnitt HTTPS-Zertifikat).
  Sobald der Relay-Tunnel automatisch eingerichtet ist (Ampel im
  Diagnose-Dashboard steht dann auf GRUEN), ist das Dashboard zusaetzlich
  ueber https://<deine-subdomain>.irlstreameros.de:<eigener Port> von
  ueberall erreichbar, MIT einem echten, automatisch von Let's Encrypt
  ausgestellten Zertifikat (terminiert auf dem Relay-Server) - dann ist
  auf externen Geraeten gar kein Root-CA-Import mehr noetig. Die genaue
  Adresse/den Port zeigt das Dashboard selbst an, sobald die Ampel gruen
  ist.

Guacamole (Fernzugriff auf dieses Geraet): https://localhost:5002/guacamole/
  Benutzer: streamer
  Passwort: ${GUAC_PASSWORD}
  (SSH- und RDP-Verbindung zu diesem Geraet sind darin bereits fertig
  eingerichtet und ueber die Weboberflaeche bearbeitbar - fuer den
  normalen Gebrauch muss man die RDP-/OBS-Websocket-Passwoerter unten
  nicht separat eingeben)

Weitere technische Passwoerter (normalerweise nicht noetig):
  RDP: ${RDP_PASSWORD}
  OBS-Websocket: ${OBS_WS_PASSWORD}

Diese Datei kann geloescht werden, sobald die Passwoerter an anderer
Stelle sicher gespeichert wurden.
EOF
chmod 600 "${HOME_DIR}/Desktop/Zugangsdaten - keep safe.txt"

# --- 8c. Feste Desktop-Icon-Anordnung (Nutzerwunsch, 2026-09-01) -----------
# GNOME/Nautilus (DING-Extension) positioniert Icons per GVFS-Metadata-
# Attribut "metadata::nautilus-icon-position" (Pixel-Koordinaten x,y), NICHT
# alphabetisch - die Namensnummerierung oben (1. Google Chrome usw.) sorgt
# nur fuer eine sinnvolle Lesereihenfolge, die tatsaechliche Position auf
# dem 1920x1080-Bildschirm wird hier hart gesetzt. Werte 1:1 live am
# Testgeraet (192.168.10.223) mit dem Nutzer abgestimmt:
#   - Lizenz-Icon: ganz oben links (Einstiegspunkt fuer neue Geraete)
#   - Rechte Spalte, von oben nach unten: 1.Chrome, 2.VPN/Belabox,
#     3.Diagnostic Tool, 4.Fernzugriff-Server, 5.OBS
#   - Unterste Reihe links: Zugangsdaten, direkt rechts daneben die
#     Installationsanleitung
# Die DING-Extension muss nach dem Setzen kurz deaktiviert/reaktiviert
# werden, damit sie die neuen Positionen sofort uebernimmt statt erst beim
# naechsten Login (live am Testgeraet verifiziert).
progress 12 "Desktop-Icons werden angeordnet..."
set_icon_pos() {
  local file="${HOME_DIR}/Desktop/${1}" pos="${2}"
  [ -e "${file}" ] || { log "WARNUNG: ${file} nicht gefunden, Position wird uebersprungen"; return; }
  sudo -u "${TARGET_USER}" \
    XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
    gio set "${file}" "metadata::nautilus-icon-position" "${pos}" 2>/dev/null \
    || log "WARNUNG: Icon-Position fuer ${file} konnte nicht gesetzt werden"
}

set_icon_pos "IRL-Streamer-OS-Lizenz-aktivieren.desktop"         "34,34"
set_icon_pos "IRL-Streamer-OS-Belabox-GUI.desktop"                "34,150"
set_icon_pos "Google-Chrome.desktop"                              "1789,34"
set_icon_pos "IRL-Streamer-OS-Fernzugriff-einrichten.desktop"    "1789,150"
set_icon_pos "IRL-Diagnostics.desktop"                            "1789,266"
set_icon_pos "Guacamole.desktop"                                  "1789,383"
set_icon_pos "OBS-Studio.desktop"                                 "1789,499"
set_icon_pos "Zugangsdaten - keep safe.txt"                       "34,965"
set_icon_pos "Installationsanleitung.desktop"                     "220,965"

sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
  gio set "${HOME_DIR}/Desktop/Zugangsdaten - keep safe.txt" "metadata::custom-icon" \
  "file://${PROJECT_DIR}/provision/assets/zugangsdaten-icon.png" 2>/dev/null \
  || log "WARNUNG: Custom-Icon fuer Zugangsdaten-Datei konnte nicht gesetzt werden"

# "Persoenlicher Ordner"-Icon vom Desktop entfernen (Nutzerwunsch 03.09.,
# kein tatsaechlicher Bedarf fuer diesen Zugang auf einem Kunden-Appliance-
# Desktop, der ausschliesslich die vordefinierten Icons zeigen soll).
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
  gsettings set org.gnome.shell.extensions.ding show-home false 2>/dev/null \
  || log "WARNUNG: 'Persoenlicher Ordner'-Icon konnte nicht ausgeblendet werden"

sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
  gnome-extensions disable ding@rastersoft.com 2>/dev/null || true
sleep 1
sudo -u "${TARGET_USER}" \
  XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
  gnome-extensions enable ding@rastersoft.com 2>/dev/null || true

# --- 8d. Absturzmeldung fuer OBS unterdruecken (Nutzerwunsch, 2026-09-01) ---
# Bekanntes, harmloses Problem: OBS 32.x linkt auf Ubuntu gegen die
# System-libsrt (aktuell 1.5.4), und genau diese Kombination hat einen
# dokumentierten Upstream-Bug (siehe obsproject/obs-studio#12361,
# Haivision/srt-Historie) - ein SIGSEGV in libsrt-gnutls' __cxa_finalize
# BEIM PROGRAMMENDE, NACHDEM OBS sich bereits sauber heruntergefahren hat
# (Log zeigt "Freeing OBS context data", "Number of memory leaks: 0" VOR
# dem Crash). Kein Daten-/Streamverlust, nur eine verunsichernde Apport-
# Absturzmeldung fuer den Nutzer. OBS-Maintainer stufen das explizit als
# "nicht unterstuetzt fuer Distro-libsrt" ein (siehe Issue-Diskussion) -
# ein Fix muesste von Ubuntu/Haivision kommen, nicht von uns. Deshalb hier
# NUR die Meldung unterdruecken (Apport-Blacklist fuer /usr/bin/obs), der
# Fehler selbst bleibt bestehen, ist aber fuer den Betrieb irrelevant.
mkdir -p /etc/apport/blacklist.d
echo "/usr/bin/obs" > /etc/apport/blacklist.d/obs-srt-shutdown-crash

# --- 9. Berechtigungen + Abschluss ------------------------------------------
progress 12 "Einrichtung wird abgeschlossen..."
chown -R "${TARGET_USER}:${TARGET_USER}" "${HOME_DIR}"
chmod 600 "${WS_PASS_FILE}" "${DASH_PASS_FILE}" "${GUAC_PASS_FILE}" "${RDP_PASS_FILE}"
chmod 600 "${SSH_KEY_DIR}/id_ed25519"

touch "${PROJECT_DIR}/.provisioned"

# "Einrichten"-Icon nach erfolgreichem Abschluss entfernen (Nutzerwunsch,
# 2026-08-25): ein zweiter Lauf waere zwar meist idempotent (Passwoerter
# bleiben unveraendert, siehe die "if [ ! -s ... ]"-Checks oben), aber
# "docker compose up -d --build" wuerde belabox-receiver/guacamole/etc.
# NEU ERSTELLEN - waehrend eines laufenden Streams also einen Unterbruch
# verursachen. Zugangsdaten.txt bleibt als sichtbares "fertig
# eingerichtet"-Signal auf dem Desktop.
rm -f "${HOME_DIR}/Desktop/IRL-Streamer-OS-einrichten.desktop"

log "Provisioning abgeschlossen. Dashboard-Login: streamer / $(cat "${DASH_PASS_FILE}")"
log "Guacamole-Login: admin / $(cat "${GUAC_PASS_FILE}") (SSH- und RDP-Verbindung darin bereits fertig konfiguriert)"
log "Diese Zugangsdaten stehen auch in ${DASH_PASS_FILE} bzw. ${WS_PASS_FILE} bzw. ${GUAC_PASS_FILE} bzw. ${RDP_PASS_FILE}."

# Abschluss-Zusammenfassung (Nutzerwunsch 2026-08-25): kompakter Ueberblick,
# was erfolgreich installiert wurde und was nicht, mit Empfehlung bei
# Fehlern - damit ein Ausfall wie der OBS-PPA-Vorfall (Launchpad-Stoerung,
# siehe Kommentar dort) nicht unbemerkt bleibt, nur weil set -e den Rest
# der Provisionierung nicht mehr abbricht.
SUMMARY_FILE="$(mktemp)"
{
  if command -v obs >/dev/null 2>&1; then
    echo "[OK]    OBS Studio"
  else
    echo "[FEHLT] OBS Studio - dieses Skript spaeter erneut ausfuehren (haeufigste Ursache: OBS-PPA/Launchpad kurzzeitig nicht erreichbar)"
  fi
  if command -v google-chrome-stable >/dev/null 2>&1; then
    echo "[OK]    Google Chrome"
  else
    echo "[FEHLT] Google Chrome - dieses Skript spaeter erneut ausfuehren"
  fi
  if systemctl is-active --quiet docker; then
    echo "[OK]    Docker"
  else
    echo "[FEHLT] Docker laeuft nicht - dieses Skript spaeter erneut ausfuehren"
  fi
  for c in belabox-receiver irl-diagnostics guacd guacamole; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "${c}"; then
      echo "[OK]    Container: ${c}"
    else
      echo "[FEHLT] Container: ${c} laeuft nicht - dieses Skript spaeter erneut ausfuehren, ggf. 'docker compose logs ${c}' zur Fehlersuche pruefen"
    fi
  done
} > "${SUMMARY_FILE}"
chmod 644 "${SUMMARY_FILE}"

log "Zusammenfassung:"
while IFS= read -r line; do log "  ${line}"; done < "${SUMMARY_FILE}"

if command -v zenity >/dev/null 2>&1; then
  sudo -u "${TARGET_USER}" \
    XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
    WAYLAND_DISPLAY="wayland-0" \
    zenity --text-info --title="IRL Streamer OS - Zusammenfassung" \
      --filename="${SUMMARY_FILE}" --width=580 --height=320 2>/dev/null || true
fi
rm -f "${SUMMARY_FILE}"

# Neustart-Abfrage (Nutzerwunsch, 2026-08-25): mehrere Aenderungen greifen
# erst nach einem Neustart, weil sie ueber Autostart-Eintraege laufen, die
# GNOME nur beim SITZUNGSSTART einliest, nicht waehrend einer laufenden
# Sitzung (RDP-Freigabe, live gefunden 2026-08-25: greift sonst gar nicht,
# bis zum naechsten Login). Dialog laeuft als der eingeloggte Nutzer (nicht
# root), damit er auf dem Wayland-Bildschirm tatsaechlich erscheint - gleiches
# Muster wie beim Icon-Trust-Fix oben (sudo -u mit expliziter
# Sitzungsumgebung), zusaetzlich WAYLAND_DISPLAY fuer die eigentliche
# Bildschirmausgabe.
if command -v zenity >/dev/null 2>&1; then
  if sudo -u "${TARGET_USER}" \
      XDG_RUNTIME_DIR="/run/user/${STREAMER_UID}" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${STREAMER_UID}/bus" \
      WAYLAND_DISPLAY="wayland-0" \
      zenity --question --title="IRL Streamer OS" \
        --text="Einrichtung abgeschlossen.\n\nEin Neustart ist erforderlich, damit alle Aenderungen (Fernzugriff/RDP, Bildschirmaufloesung, Tastaturlayout) vollstaendig greifen.\n\nJetzt neu starten?" \
        --ok-label="Jetzt neu starten" --cancel-label="Spaeter" --width=420 2>/dev/null; then
    log "Neustart auf Nutzerwunsch..."
    reboot
  else
    log "Neustart verschoben - bitte manuell neu starten, sobald moeglich, damit RDP/Aufloesung/Tastatur vollstaendig greifen."
  fi
else
  log "HINWEIS: Ein Neustart ist noch erforderlich, damit alle Aenderungen (Fernzugriff/RDP, Bildschirmaufloesung, Tastaturlayout) vollstaendig greifen."
fi
