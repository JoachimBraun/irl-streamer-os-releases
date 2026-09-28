#!/usr/bin/env python3
"""
IRL Streamer OS - Lokale Lizenz-Signaturpruefung

Prueft eine lokal gespeicherte license.json GEGEN DEN FEST EINGEBETTETEN
PUBLIC KEY - komplett offline, kein Netzwerkzugriff noetig. Das ist der
Kern des Sicherheitsmodells: ein Angreifer kann die Datei beliebig
bearbeiten (z.B. expires_at in die Zukunft setzen), aber ohne den privaten
Schluessel des Servers keine gueltige Signatur fuer den geaenderten Inhalt
erzeugen - die Pruefung schlaegt dann fehl und der Status faellt auf
"invalid" zurueck (wird wie "abgelaufen" behandelt, nicht wie "gueltig").

V1.85: PUBLIC_KEY_B64 ist jetzt FEST IM REPO eingetragen (oeffentlicher
Schluessel, kein Geheimnis). Vorher wurde er beim ISO-Build per sed
eingesetzt und bei jedem Update von irl-streamer-update-check.sh wieder
"gerettet" - fehleranfaellig. Der ISO-Build prueft jetzt nur noch, dass
dieser Wert dem Live-Key des Lizenzservers entspricht. Der alte
1.84-Updater (sed Build-Platzhalter -> Key) ist dadurch harmlos: der
Platzhalter kommt in dieser Datei nicht mehr vor, sed ersetzt nichts.

Aufruf:
  license-check.py /pfad/zu/license.json
      -> {"state": "valid", "kind": ..., "expires_at": ..., "days_remaining": N}
      -> {"state": "expired", "kind": ..., "expires_at": ...}
      -> {"state": "invalid", "reason": "..."}
  license-check.py --observe-server-time <epoch>
      merkt sich die (per TLS vom eigenen Lizenzserver gelieferte)
      Serverzeit als Untergrenze fuer "jetzt" (Schutz gegen Zurueckstellen
      der Systemuhr, siehe effective_now()).
  license-check.py --grace-status
      -> {"in_grace": true|false, "installed_at": ..., "grace_ends_at": ...}
      Karenzzeit fuer Geraete OHNE gueltige Lizenzdatei (siehe unten).

Exit-Code bei der Pruefung immer 0 (Ergebnis steht im JSON).
"""
import base64
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Oeffentlicher Schluessel des Lizenzservers (lizenz.irlstreameros.de,
# GET /public_key) - siehe Modul-Kommentar oben.
PUBLIC_KEY_B64 = "kpR8NYOyY4PczrrYSNu8q8BvJo/OTnuPCl8jpeWfdNc="

# Root-only Zustand AUSSERHALB des Update-rsync-Ziels (/opt/irl-streamer-os),
# damit ein Update ihn nie ueberschreibt. Im Diagnose-Container (liest
# /opt/irl-streamer-os read-only) existiert dieser Ordner nicht - dann wird
# schlicht ohne Uhr-Schutz gearbeitet (reine Anzeige, keine Sperrlogik).
STATE_DIR = "/var/lib/irl-streamer-os"
CLOCK_STATE_FILE = os.path.join(STATE_DIR, "license-clock.json")
INSTALLED_AT_FILE = os.path.join(STATE_DIR, "installed-at")

# Toleranz, bevor eine zurueckgestellte Uhr als Manipulation gilt (RTC-
# Drift, falsche Zeit vor dem ersten NTP-Sync nach leerer BIOS-Batterie).
CLOCK_ROLLBACK_TOLERANCE = timedelta(days=1)

# Karenzzeit ohne gueltige Lizenz (z.B. Geraet nie online gewesen).
GRACE_PERIOD = timedelta(days=14)


def load_public_key(b64: str = PUBLIC_KEY_B64) -> Ed25519PublicKey:
    raw = base64.b64decode(b64)
    return Ed25519PublicKey.from_public_bytes(raw)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _atomic_write_json(path, data):
    """tmp + rename im selben Ordner - nie eine halb geschriebene Datei."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _last_seen(clock_state_file):
    state = _read_json(clock_state_file) or {}
    try:
        return float(state.get("last_seen_server_epoch", 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def effective_now(system_now: datetime, clock_state_file: str = CLOCK_STATE_FILE):
    """Liefert (now, tampered). last_seen stammt NUR aus der Serverzeit
    (--observe-server-time), NIE aus der lokalen Uhr - eine versehentlich
    in die Zukunft gestellte Systemuhr kann so keinen legitimen Kunden
    dauerhaft aussperren. Liegt die Systemzeit mehr als 1 Tag VOR der
    zuletzt gesehenen Serverzeit, gilt sie als manipuliert -> Serverzeit."""
    last_seen = _last_seen(clock_state_file)
    if last_seen <= 0:
        return system_now, False
    last_dt = datetime.fromtimestamp(last_seen, tz=timezone.utc)
    if system_now < last_dt - CLOCK_ROLLBACK_TOLERANCE:
        return last_dt, True
    return system_now, False


def observe_server_time(epoch: float, clock_state_file: str = CLOCK_STATE_FILE) -> float:
    """Merkt sich max(bisher, epoch) und gibt den neuen Wert zurueck."""
    old = _last_seen(clock_state_file)
    new = max(old, float(epoch))
    if new != old:
        _atomic_write_json(clock_state_file, {"last_seen_server_epoch": new})
    return new


def parse_expiry(value):
    """ISO-Zeitstempel MIT Zeitzone -> datetime, sonst None. Ein Wert ohne
    Zeitzone (naive datetime) fuehrte frueher zu einem TypeError-Absturz
    beim Vergleich mit der UTC-Zeit - jetzt sauber "invalid"."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def evaluate(data, now: datetime, pubkey: Ed25519PublicKey) -> dict:
    """Reine Pruef-Logik ohne Datei-/Uhr-Zugriffe (testbar)."""
    if not isinstance(data, dict):
        return {"state": "invalid", "reason": "corrupt_file"}

    required = {"payload", "signature", "kind", "expires_at"}
    if not required.issubset(data.keys()):
        return {"state": "invalid", "reason": "missing_fields"}

    try:
        sig = base64.b64decode(data["signature"])
        pubkey.verify(sig, str(data["payload"]).encode())
    except Exception:  # noqa: BLE001 - jeder Fehler = ungueltig (fail closed)
        return {"state": "invalid", "reason": "signature_mismatch"}

    # Signatur gueltig -> payload unveraendert vom Server. Die WAHRHEIT ist
    # immer "payload", nie das bequeme expires_at-Feld daneben.
    try:
        expected_payload = f"{data['device_fingerprint']}|{data['kind']}|{data['expires_at']}"
    except KeyError:
        return {"state": "invalid", "reason": "missing_fields"}
    if expected_payload != data["payload"]:
        return {"state": "invalid", "reason": "payload_mismatch"}

    expires_at = parse_expiry(data["expires_at"])
    if expires_at is None:
        return {"state": "invalid", "reason": "bad_expiry"}

    if now >= expires_at:
        return {"state": "expired", "kind": data["kind"], "expires_at": data["expires_at"]}
    return {
        "state": "valid",
        "kind": data["kind"],
        "expires_at": data["expires_at"],
        "days_remaining": (expires_at - now).days,
    }


def grace_status(now: datetime, installed_at_file: str = INSTALLED_AT_FILE) -> dict:
    """Karenzzeit fuer Geraete OHNE gueltige Lizenz. installed-at legt
    provision.sh EINMALIG an (Bestandsgeraete: beim ersten Lauf von 1.85 ->
    die Karenz beginnt erst dann, niemand wird durch das Update sofort
    gesperrt). Fehlt/unlesbar -> IN der Karenz (nie faelschlich sperren)."""
    try:
        with open(installed_at_file) as f:
            installed = datetime.fromtimestamp(float(f.read().strip()), tz=timezone.utc)
    except (OSError, ValueError, OverflowError):
        return {"in_grace": True, "installed_at": None, "grace_ends_at": None}
    ends = installed + GRACE_PERIOD
    return {
        "in_grace": now < ends,
        "installed_at": installed.isoformat(),
        "grace_ends_at": ends.isoformat(),
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    if len(argv) == 2 and argv[0] == "--observe-server-time":
        try:
            new = observe_server_time(float(argv[1]))
        except (ValueError, OSError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        print(json.dumps({"ok": True, "last_seen_server_epoch": new}))
        return 0

    if len(argv) == 1 and argv[0] == "--grace-status":
        now, _ = effective_now(datetime.now(timezone.utc))
        print(json.dumps(grace_status(now)))
        return 0

    if len(argv) != 1:
        print(json.dumps({"state": "invalid", "reason": "usage"}))
        return 1

    data = _read_json(argv[0])
    if data is None:
        print(json.dumps({"state": "invalid", "reason": "corrupt_file"}))
        return 0

    try:
        pubkey = load_public_key()
    except Exception:  # noqa: BLE001
        print(json.dumps({"state": "invalid", "reason": "bad_public_key"}))
        return 0

    now, tampered = effective_now(datetime.now(timezone.utc))
    result = evaluate(data, now, pubkey)
    if tampered:
        result["clock_rollback_detected"] = True
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
