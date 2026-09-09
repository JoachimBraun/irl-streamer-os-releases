#!/usr/bin/env bash
# IRL Streamer OS - Versteckter Lizenz-"Tresor" (Haertung, Nutzerwunsch
# 2026-09-05: Lizenzdateien nicht mehr offensichtlich unter
# /opt/irl-streamer-os/state/license.json auffindbar, sondern in einem
# unauffaellig benannten Ordner zwischen vielen aehnlich aussehenden
# Ablenk-Dateien versteckt, mit einer verschluesselten Verweis-Datei.
#
# EHRLICHE EINORDNUNG (wichtig, siehe README zum Sicherheitsmodell): das
# ist KEINE Verschluesselung GEGEN root - ein Prozess, der als root laeuft
# (was "streamer" per sudo jederzeit kann), kann dieses Skript selbst
# lesen und lernt daraus Ordnerpfad + Entschluesselungslogik. Der Gewinn
# ist rein die ZEIT/das WISSEN, das ein durchschnittlicher Kunde
# aufbringen muesste: statt einer offensichtlich benannten Datei
# ("license.json", sofort per Texteditor aenderbar) muss er erst dieses
# Skript verstehen, dann in einem Ordner mit ~120 gleich aussehenden
# Dateien die zwei echten finden und die verschluesselte Verweisdatei
# entschluesseln. Gegen einen Kunden, der gezielt das Repo/die Skripte
# liest, ist das kein Schutz - das ist unveraendert so gewollt (siehe
# Nutzerentscheidung: 99% der Kunden waeren dazu nicht in der Lage/nicht
# gewillt).
#
# Wird EINMALIG von provision.sh aufgerufen (idempotent - prueft ob der
# Resolver bereits existiert, bevor irgendetwas neu erzeugt wird). Der
# Fernet-Schluessel UND alle Dateinamen werden PRO GERAET frisch
# zufaellig generiert (nicht einmalig beim ISO-Bau) - ein einmal
# analysiertes Schema auf einem Geraet verrät nichts ueber ein anderes.

set -euo pipefail

PROJECT_DIR="/opt/irl-streamer-os"
STATE_DIR="${PROJECT_DIR}/state"
OLD_LICENSE_FILE="${STATE_DIR}/license.json"
OLD_LOCK_FILE="${STATE_DIR}/license-locked"
RESOLVER_SCRIPT="${PROJECT_DIR}/provision/licensing/license-locate.py"
VAULT_DIR="/opt/.intel-mediasdk-cache"
DECOY_COUNT=120
LOG_PREFIX="[license-vault-init]"

log() { echo "${LOG_PREFIX} $*"; }

# Idempotenz: Resolver existiert bereits -> Tresor wurde schon einmal
# initialisiert, nichts weiter tun. Verhindert, dass ein erneuter
# provision.sh-Lauf (sollte eigentlich nur beim Ersteinrichten passieren)
# eine bereits aktive Lizenz durch einen neuen, leeren Tresor ersetzt.
if [ -f "${RESOLVER_SCRIPT}" ]; then
    log "Tresor bereits initialisiert (${RESOLVER_SCRIPT} existiert) - ueberspringe."
    exit 0
fi

log "Initialisiere versteckten Lizenz-Tresor unter ${VAULT_DIR}"
mkdir -p "${VAULT_DIR}"
chmod 700 "${VAULT_DIR}"
chown root:root "${VAULT_DIR}"

# Die gesamte Erzeugung (Ordner, Decoys, echte Dateien, Schluessel, Index,
# Resolver-Skript) laeuft in EINEM Python-Prozess, damit alle Zufallswerte
# konsistent aus derselben Quelle kommen und keine Race Conditions
# zwischen mehreren Aufrufen entstehen.
python3 - "${VAULT_DIR}" "${DECOY_COUNT}" "${RESOLVER_SCRIPT}" "${OLD_LICENSE_FILE}" "${OLD_LOCK_FILE}" <<'PYEOF'
import json
import os
import secrets
import stat
import sys

from cryptography.fernet import Fernet

vault_dir, decoy_count, resolver_script, old_license_file, old_lock_file = sys.argv[1:6]
decoy_count = int(decoy_count)


def rand_name():
    return secrets.token_hex(4) + ".cache"


def write_secret_file(path, data: bytes):
    # 600 wie die bisherige license.json/license-locked - nur root kann
    # lesen. Alle Tresor-Dateien (Decoys UND echte) bekommen dieselben
    # Rechte, damit man die echten nicht ueber abweichende Dateirechte
    # vom Rest unterscheiden kann.
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


used_names = set()


def unique_name():
    while True:
        n = rand_name()
        if n not in used_names:
            used_names.add(n)
            return n


# --- Ablenk-Dateien: zufaelliger Muell in plausibler Groesse (200B-3KB),
# gleiches Namensschema + gleiche Rechte wie die echten Dateien - rein
# zur Tarnung, keine Funktion.
for _ in range(decoy_count):
    name = unique_name()
    size = secrets.randbelow(3 * 1024 - 200) + 200
    write_secret_file(os.path.join(vault_dir, name), secrets.token_bytes(size))

# --- Echte Dateien: Inhalt einer evtl. schon bestehenden license.json/
# license-locked wird migriert (Aufwaerts-Kompatibilitaet, falls
# provision.sh doch mal auf einem bereits lizenzierten Geraet erneut
# liefe), sonst leer angelegt - license-client.sh befuellt sie beim
# naechsten trial-start/activate ganz normal weiter.
license_name = unique_name()
lock_name = unique_name()

license_content = b""
if os.path.exists(old_license_file):
    with open(old_license_file, "rb") as f:
        license_content = f.read()
write_secret_file(os.path.join(vault_dir, license_name), license_content)

if os.path.exists(old_lock_file):
    with open(old_lock_file, "rb") as f:
        lock_content = f.read()
    write_secret_file(os.path.join(vault_dir, lock_name), lock_content)
    os.remove(old_lock_file)
# Falls keine Sperre aktiv war: KEINE Lock-Datei anlegen (Existenz =
# gesperrt, wie bisher) - lock_name ist trotzdem im Index vermerkt, damit
# license-service-lock.sh/license-daily-check.sh spaeter wissen, unter
# welchem Namen sie die Sperr-Datei bei Bedarf ANLEGEN muessen.

if os.path.exists(old_license_file):
    os.remove(old_license_file)

# --- Schluessel + verschluesselter Index -----------------------------------
key = Fernet.generate_key()
key_name = unique_name()
write_secret_file(os.path.join(vault_dir, key_name), key)

index_payload = json.dumps({
    "license_file": license_name,
    "lock_file": lock_name,
}).encode()
index_name = unique_name()
write_secret_file(os.path.join(vault_dir, index_name), Fernet(key).encrypt(index_payload))

# --- Resolver-Skript: PRO GERAET frisch erzeugt, kennt nur den
# Ordnerpfad + welche zwei Dateinamen Schluessel/Index sind - die
# eigentlichen license_file/lock_file-Namen liegen NUR verschluesselt im
# Index, nicht hier im Klartext.
resolver_source = f'''#!/usr/bin/env python3
"""
IRL Streamer OS - Lizenz-Tresor-Resolver (automatisch generiert von
license-vault-init.sh, NICHT von Hand bearbeiten - ein erneuter Lauf von
provision.sh ueberschreibt diese Datei nicht, siehe Idempotenz-Check dort,
aber manuelle Aenderungen hier wuerden bei einer zukuenftigen bewussten
Neu-Initialisierung verloren gehen).

Aufruf: license-locate.py license_file|lock_file
Ausgabe: absoluter Pfad auf stdout (kein Zeilenumbruch-Wirrwarr, ein
einzelner Pfad), Exit-Code 1 bei unbekanntem Namen.

Auch als Python-Modul nutzbar (import license_locate; license_locate.resolve("license_file")).
"""
import json
import os
import sys

from cryptography.fernet import Fernet

VAULT_DIR = {vault_dir!r}
KEY_FILENAME = {key_name!r}
INDEX_FILENAME = {index_name!r}


def resolve(name: str) -> str:
    with open(os.path.join(VAULT_DIR, KEY_FILENAME), "rb") as f:
        key = f.read()
    with open(os.path.join(VAULT_DIR, INDEX_FILENAME), "rb") as f:
        encrypted_index = f.read()
    index = json.loads(Fernet(key).decrypt(encrypted_index))
    if name not in index:
        raise KeyError(f"Unbekannter Tresor-Eintrag: {{name}}")
    return os.path.join(VAULT_DIR, index[name])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Nutzung: license-locate.py license_file|lock_file", file=sys.stderr)
        sys.exit(1)
    try:
        print(resolve(sys.argv[1]))
    except (KeyError, FileNotFoundError) as exc:
        print(f"FEHLER: {{exc}}", file=sys.stderr)
        sys.exit(1)
'''

with open(resolver_script, "w") as f:
    f.write(resolver_source)
os.chmod(resolver_script, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

print(f"[license-vault-init] Tresor initialisiert: {decoy_count} Ablenk-Dateien + 2 echte Dateien + Schluessel + Index unter {vault_dir}")
PYEOF

chown -R root:root "${VAULT_DIR}"
log "Fertig."
