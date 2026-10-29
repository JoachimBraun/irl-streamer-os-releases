"""Sendekette der Belabox (belabox-net/): reine Logik fuer das Dashboard (ohne SSH, ohne FastAPI - dadurch testbar).

Inhalt des Box-Pakets (eigene Umsetzung, Ideen aus IRL4YOU BOX, siehe belabox-net/README.md): Ersatz-Internetweg je Netzkarte,
belacoder = Original + Kennzahlen-Datei (Regler und srtla_send unveraendert), Haenger-Waechter. Das Dashboard spielt es wie die RTMP-Schutzschicht automatisch ein und haelt es aktuell.
"""
import gzip
import hashlib
import io
import json
import os
import tarfile
from typing import Optional

BUNDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "belabox-net")
BUNDLE_FILES = ("net-setup.sh", "belacoder-aarch64", "irl-net-fallback-route", "irl_net_watch.py",
                "irl-net-watch.service", "build-on-box.sh", "README.md", "patches/belacoder-stats.patch",
                # GPL-3.0-Pflicht: Lizenztext + vollstaendiger Quelltext zu belacoder-aarch64
                "COPYING-belacoder.txt", "src/belacoder-ccce9ca.tar.gz")
EXECUTABLE = ("net-setup.sh", "belacoder-aarch64", "build-on-box.sh")
REMOTE_DIR = "/opt/irl-net"


def bundle_hash() -> str:
    """Kennung des Pakets (Dateinamen + Inhalte). Steht nach dem Einspielen auf der Box in BUNDLE_HASH."""
    h = hashlib.sha256()
    for name in BUNDLE_FILES:
        path = os.path.join(BUNDLE_DIR, name)
        if not os.path.isfile(path):
            raise FileNotFoundError("Sendekette-Paket unvollstaendig: %s fehlt" % name)
        h.update(name.encode() + b"\0")
        with open(path, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()[:12]


def build_bundle() -> bytes:
    """tar.gz mit allen Box-Dateien, dazu SHA256SUMS (Pruefung auf der Box) und BUNDLE_HASH."""
    buf = io.BytesIO()
    sums = []
    # gzip-Kopf ohne Zeitstempel (mtime=0): gleiche Dateien -> byte-gleiches Paket (sonst je nach Sekunde verschieden)
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        def add(name: str, data: bytes, mode: int) -> None:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0
            tar.addfile(ti, io.BytesIO(data))

        for name in BUNDLE_FILES:
            with open(os.path.join(BUNDLE_DIR, name), "rb") as f:
                data = f.read()
            sums.append("%s  %s" % (hashlib.sha256(data).hexdigest(), name))
            add(name, data, 0o755 if name in EXECUTABLE else 0o644)
        add("SHA256SUMS", ("\n".join(sums) + "\n").encode(), 0o644)
        add("BUNDLE_HASH", (bundle_hash() + "\n").encode(), 0o644)
    return buf.getvalue()


def parse_status(out: str) -> dict:
    """Letzte JSON-Zeile des Status-Befehls der Box; alles andere gilt als 'nicht eingerichtet'."""
    try:
        info = json.loads((out or "").strip().splitlines()[-1])
        if not isinstance(info, dict):
            raise ValueError
    except Exception:
        return {"installed": False}
    info.setdefault("installed", False)
    return info


def needs_update(status: dict, local_bundle: str) -> Optional[str]:
    """None, wenn die Box aktuell ist; sonst ein kurzer Grund (deutsch)."""
    if not status.get("installed"):
        return "nicht eingerichtet"
    if status.get("bundle") != local_bundle:
        return "Paket veraltet"
    if status.get("watch") != "active":
        return "Waechter laeuft nicht"
    if not status.get("fallback_hook"):
        return "Ersatzweg fehlt"
    if status.get("belacoder_patched") and not status.get("belaui_env"):
        return "belaUI noch ohne neue Einstellungen"
    return None


def summarize(status: dict, local_bundle: str) -> dict:
    """Kurzfassung fuer die Oberflaeche."""
    reason = needs_update(status, local_bundle)
    return {
        "installed": bool(status.get("installed")),
        "up_to_date": reason is None,
        "reason": reason or "",
        "belacoder_patched": bool(status.get("belacoder_patched")),
        "srtla_patched": bool(status.get("srtla_patched")),
        "fallback_hook": bool(status.get("fallback_hook")),
        "watch": status.get("watch", ""),
        "interventions_last_hour": int(status.get("interventions_last_hour") or 0),
        "last_intervention": status.get("last_intervention"),
        "last_reason": status.get("last_reason", ""),
        "stats_age_s": status.get("stats_age_s"),
        "bundle": local_bundle,
    }
