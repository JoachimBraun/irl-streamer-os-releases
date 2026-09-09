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

PUBLIC_KEY_B64 wird EINMALIG beim ISO-Build aus dem Lizenzserver
abgerufen (GET /public_key) und hier fest eingetragen - siehe
iso-build/build-desktop-autoinstall-iso.sh, Abschnitt "Lizenz-Public-Key
einbetten". Aendert sich der Server-Schluessel jemals (sollte er nicht),
muss eine neue ISO gebaut werden.

Aufruf: license-check.py /pfad/zu/license.json
Ausgabe (stdout, ein JSON-Objekt):
  {"state": "valid", "kind": "trial"|"license", "expires_at": "...",
   "days_remaining": 12}
  {"state": "invalid", "reason": "signature_mismatch"|"corrupt_file"}
  {"state": "expired", "kind": "...", "expires_at": "..."}
"""
import base64
import json
import sys
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# TODO(build-time): wird beim ISO-Build automatisch durch den echten
# Public Key des Lizenzservers ersetzt (siehe build-desktop-autoinstall-iso.sh).
# Dieser Platzhalter macht JEDE Signatur ungueltig, bis er ersetzt wird -
# bewusst "fail closed" statt "fail open", damit ein vergessener
# Ersetzungsschritt nicht versehentlich jede Lizenz als gueltig durchwinkt.
PUBLIC_KEY_B64 = "REPLACE_AT_BUILD_TIME"


def load_public_key() -> Ed25519PublicKey:
    raw = base64.b64decode(PUBLIC_KEY_B64)
    return Ed25519PublicKey.from_public_bytes(raw)


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"state": "invalid", "reason": "usage"}))
        sys.exit(1)

    path = sys.argv[1]

    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        print(json.dumps({"state": "invalid", "reason": "corrupt_file"}))
        sys.exit(0)

    required = {"payload", "signature", "kind", "expires_at"}
    if not required.issubset(data.keys()):
        print(json.dumps({"state": "invalid", "reason": "missing_fields"}))
        sys.exit(0)

    try:
        pubkey = load_public_key()
        sig = base64.b64decode(data["signature"])
        pubkey.verify(sig, data["payload"].encode())
    except (InvalidSignature, ValueError, Exception):
        print(json.dumps({"state": "invalid", "reason": "signature_mismatch"}))
        sys.exit(0)

    # Signatur ist gueltig -> payload ist definitiv unveraendert vom Server
    # ausgestellt worden. Jetzt Ablauf pruefen (payload und expires_at
    # muessen konsistent sein - payload ist die eigentliche signierte
    # Wahrheit, expires_at im JSON ist nur eine bequeme Kopie davon).
    try:
        expected_payload = f"{data['device_fingerprint']}|{data['kind']}|{data['expires_at']}"
    except KeyError:
        print(json.dumps({"state": "invalid", "reason": "missing_fields"}))
        sys.exit(0)

    if expected_payload != data["payload"]:
        # expires_at im JSON wurde nachtraeglich veraendert, ohne den
        # signierten payload-String mit anzupassen - waere zwar wegen der
        # Signaturpruefung oben schon aufgefallen (data["payload"] ist ja
        # signiert, nicht data["expires_at"]), aber dieser Zusatzcheck
        # macht den Vertrauensanker explizit: die WAHRHEIT ist immer
        # "payload", nie das bequeme expires_at-Feld daneben.
        print(json.dumps({"state": "invalid", "reason": "payload_mismatch"}))
        sys.exit(0)

    expires_at = datetime.fromisoformat(data["expires_at"])
    now = datetime.now(timezone.utc)
    days_remaining = (expires_at - now).days

    if now >= expires_at:
        print(json.dumps({
            "state": "expired",
            "kind": data["kind"],
            "expires_at": data["expires_at"],
        }))
    else:
        print(json.dumps({
            "state": "valid",
            "kind": data["kind"],
            "expires_at": data["expires_at"],
            "days_remaining": days_remaining,
        }))


if __name__ == "__main__":
    main()
