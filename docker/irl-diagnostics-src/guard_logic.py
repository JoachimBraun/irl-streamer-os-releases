"""RTMP-Schutzschicht der Belabox: reine Logik fuer das Dashboard (ohne SSH, ohne FastAPI - dadurch testbar).

Hintergrund: Kameras (DJI Osmo Action 4 / Pocket 3) senden ihren RTMP-Strom unregelmaessig beschaedigt an die Belabox; nginx trennt dann
sofort und die Kamera gibt auf. Auf der Belabox laeuft deshalb ein kleiner Dienst (belabox-guard/rtmp_guard.py), der den Strom prueft,
beschaedigte Stellen repariert und nur gueltige Daten an nginx weiterreicht. Das Dashboard spielt ihn bei jeder Belabox automatisch ein
und haelt ihn aktuell (wie beim Bild-in-Bild-Paket: Paket per SSH hochladen, Einrichtungsskript auf der Box ausfuehren).
"""
import hashlib
import io
import json
import os
import tarfile
from typing import Optional

BUNDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "belabox-guard")
BUNDLE_FILES = ("rtmp_guard.py", "guard-nat.sh", "guard-retire.sh", "guard-setup.sh",
                "irl-rtmp-guard.service", "irl-rtmp-guard-b.service")
EXECUTABLE = ("guard-nat.sh", "guard-retire.sh", "guard-setup.sh")
REMOTE_DIR = "/opt/irl-rtmp-guard"


def bundle_hash() -> str:
    """Kennung des Pakets (Dateinamen + Inhalte). Steht nach dem Einspielen auf der Box in BUNDLE_HASH."""
    h = hashlib.sha256()
    for name in BUNDLE_FILES:
        path = os.path.join(BUNDLE_DIR, name)
        if not os.path.isfile(path):
            raise FileNotFoundError("Schutzschicht-Paket unvollstaendig: %s fehlt" % name)
        h.update(name.encode() + b"\0")
        with open(path, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()[:12]


def file_version() -> str:
    """Kennung von rtmp_guard.py allein (so meldet es auch die laufende Instanz auf der Box)."""
    with open(os.path.join(BUNDLE_DIR, "rtmp_guard.py"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def build_bundle() -> bytes:
    """tar.gz mit allen Box-Dateien, dazu SHA256SUMS (Pruefung auf der Box) und BUNDLE_HASH."""
    buf = io.BytesIO()
    sums = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name: str, data: bytes, mode: int) -> None:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0
            tar.addfile(ti, io.BytesIO(data))

        for name in BUNDLE_FILES:
            path = os.path.join(BUNDLE_DIR, name)
            if not os.path.isfile(path):
                raise FileNotFoundError("Schutzschicht-Paket unvollstaendig: %s fehlt" % name)
            with open(path, "rb") as f:
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


def needs_update(status: dict, local_bundle: str, local_version: str) -> Optional[str]:
    """None, wenn die Box aktuell ist; sonst ein kurzer Grund (deutsch)."""
    if not status.get("installed"):
        return "nicht eingerichtet"
    if status.get("bundle") != local_bundle:
        return "Paket veraltet"
    instances = status.get("instances") or {}
    if not instances:
        return "Dienst laeuft nicht"
    if not status.get("rule_port"):
        return "Umleitung fehlt"
    holder = [i for i in instances.values() if str(i.get("port")) == str(status.get("rule_port"))]
    if not holder:
        return "Umleitung zeigt auf keine laufende Instanz"
    if holder[0].get("running_version") != local_version:
        return "laufende Version veraltet"
    return None


def summarize(status: dict, local_bundle: str, local_version: str) -> dict:
    """Kurzfassung fuer die Oberflaeche."""
    reason = needs_update(status, local_bundle, local_version)
    instances = status.get("instances") or {}
    return {
        "installed": bool(status.get("installed")),
        "up_to_date": reason is None,
        "reason": reason or "",
        "connections": sum(int(i.get("connections", 0)) for i in instances.values()),
        "repairs": sum(int(i.get("repairs", 0)) for i in instances.values()),
        "giveups": sum(int(i.get("giveups", 0)) for i in instances.values()),
        "last_repair": next((i.get("last_repair") for i in instances.values() if i.get("last_repair")), None),
        "version": local_version,
    }
