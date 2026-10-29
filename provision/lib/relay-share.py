#!/usr/bin/env python3
"""Gibt dem Diagnose-Dashboard nur die Relay-Angaben, die es wirklich braucht.

Review 29.10.2026 (B2): Das Dashboard sah bisher den ganzen Projektordner
/opt/irl-streamer-os (read-only) - darin u.a. der SSH-Schluessel fuer den
Benutzer streamer (Guacamole), die Zugangs-Passwoerter und in
relay-provision.json die komplette WireGuard-Konfiguration samt privatem
Schluessel. Mit dem bekannten sudo-Passwort waere ein uebernommenes Dashboard
damit root auf dem Host gewesen.

Jetzt bekommt der Container nur noch diesen Ordner (read-only) - mit einer
Kopie der Felder aus relay-provision.json, die das Dashboard liest (Ampel,
Ports, Dienst-Schalter, Fingerprint fuer /toggle-port). Kein WireGuard-Schluessel.

Aufruf (als root, nie fatal): relay-share.py [<relay-provision.json> [<zielordner>]]
Aufgerufen von irl-connectivity-report-client.sh (bei jedem Lauf) und provision.sh.
"""
import json
import os
import sys
import tempfile

SRC = "/opt/irl-streamer-os/state/relay-provision.json"
DST_DIR = "/var/lib/irl-streamer-os-dashboard"
FIELDS = (
    "subdomain_slug", "verified", "device_fingerprint",
    "srtla_public_port", "srtla_enabled",
    "wg_fernzugriff_public_port", "wg_fernzugriff_enabled",
    "obs_websocket_public_port", "obs_websocket_enabled",
    "belabox_webgui_public_port", "belabox_webgui_enabled",
    "diagnostic_public_port", "guacamole_public_port", "filebrowser_public_port",
    # Review 29.10.2026 (C8): signiertes Relay-Token fuer /toggle-port (Geraete-Nachweis)
    "relay_token_payload", "relay_token_signature",
)


def share(src: str = SRC, dst_dir: str = DST_DIR) -> str:
    os.makedirs(dst_dir, mode=0o700, exist_ok=True)
    os.chmod(dst_dir, 0o700)
    dst = os.path.join(dst_dir, "relay-provision.json")
    try:
        with open(src, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # Noch kein Relay (Ampel rot) -> auch keine Kopie (Dashboard zeigt rot)
        if os.path.exists(dst):
            os.unlink(dst)
            return "entfernt"
        return "keine Quelle"
    if not isinstance(data, dict):
        return "Quelle ungueltig"
    out = {k: data[k] for k in FIELDS if k in data}
    # Zeitpunkt des letzten Connectivity-Laufs (Dashboard zeigt "zuletzt geprueft")
    out["source_mtime"] = os.stat(src).st_mtime
    text = json.dumps(out, sort_keys=True)
    try:
        with open(dst, encoding="utf-8") as f:
            if f.read() == text:
                return "unveraendert"
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=dst_dir, prefix=".share-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return "geschrieben"


if __name__ == "__main__":
    try:
        print(share(*sys.argv[1:3]))
    except Exception as exc:  # nie fatal fuer den Aufrufer
        print("relay-share: %s" % exc, file=sys.stderr)
    sys.exit(0)
