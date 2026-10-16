"""Bild-in-Bild (PiP): reine Logik fuer das Dashboard (ohne SSH, ohne FastAPI - dadurch testbar).

Regeln (Nutzerwunsch 12.10.2026):
  * Die ERSTE Kamera in der Dashboard-Kameraliste (dji_cameras[0]) ist immer das Hauptbild.
  * Die 2., 3. und 4. Kamera koennen als kleine Bilder ueber das Hauptbild gelegt werden (Position frei verschiebbar).
  * Jede Kamera hat auf der Box einen Namen = letztes Stueck ihrer RTMP-Adresse (.../publish/<Name>).

Die Box (Dienst irl-pip) validiert alles noch einmal selbst; hier wird nur in ein sauberes, vollstaendiges Layout uebersetzt.
"""
import io
import os
import re
import tarfile
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

CAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
MAX_PIPS = 3
CORNERS = ("tl", "tr", "bl", "br", "free")
DEFAULT_CORNERS = ("br", "bl", "tr")
# belaUI-Pipeline-ID von "custom/irl_pip" = SHA-1 des Pipeline-Pfads (so berechnet belaUI die IDs)
PIP_PIPELINE_ID = "6cb5071f05535a7516d7f5c3d6f8744d086b4162"
PIP_PIPELINE_LABEL = "Bild-in-Bild (mehrere Kameras)"
BUNDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "belabox-pip")
BUNDLE_FILES = ("libgstirlpip-aarch64.so", "irl-pip-ctl.py", "irl_pip_gen.py", "irl-pip-cli",
                "irl-pip-setup.sh", "SHA256SUMS")


class PipWindow(BaseModel):
    """Ein kleines Bild. 'cam' ist der Kamera-Name (siehe camera_names)."""
    cam: str
    enabled: bool = True
    corner: str = "br"          # tl|tr|bl|br oder "free" (dann zaehlen fx/fy)
    fx: float = 1.0             # 0..1: Anteil des freien Platzes (links->rechts), nur bei corner=free
    fy: float = 1.0             # 0..1: Anteil des freien Platzes (oben->unten)
    size_pct: float = 25.0      # Breite in Prozent des Hauptbilds (8..50)
    border: int = 4             # Rahmen in Pixel (0..40)
    color: str = "ffffff"       # Rahmenfarbe RRGGBB
    delay_ms: int = 0           # Versatz fuer Lippensynchronitaet (0..3000)
    crop_left: float = 0.0      # Beschnitt der Kameraquelle in Prozent ihrer Breite (0..45, links+rechts max. 90)
    crop_right: float = 0.0     # Beschnitt rechts (0..45)
    crop_top: float = 0.0       # Beschnitt oben in Prozent der Kamera-Hoehe (0..45, oben+unten max. 90)
    crop_bottom: float = 0.0    # Beschnitt unten (0..45)


class PipConfig(BaseModel):
    windows: list[PipWindow] = []
    audio: str = "main"         # "main" oder Kamera-Name
    main_delay_ms: int = 0
    smooth_ms: int = 100        # Glaettung / Reserve gegen schubweise Ankunft (0..500)


def camera_name(rtmp_url: str, index: int) -> str:
    """Name der Kamera auf der Box: letztes Pfadstueck der RTMP-Adresse, sonst 'camN' (N = Position, ab 1)."""
    try:
        path = urlsplit((rtmp_url or "").strip()).path
    except ValueError:
        path = ""
    seg = [s for s in path.split("/") if s][-1] if [s for s in path.split("/") if s] else ""
    return seg if CAM_RE.match(seg) else "cam%d" % (index + 1)


def camera_names(rtmp_urls: list) -> tuple:
    """-> (Namen, Probleme). Doppelte Namen sind ein Problem (die Box kennt jede Kamera nur einmal)."""
    names = [camera_name(u, i) for i, u in enumerate(rtmp_urls)]
    problems = []
    seen = {}
    for i, n in enumerate(names):
        if n in seen:
            problems.append("Kamera %d und Kamera %d haben denselben Namen '%s' (letztes Stueck der RTMP-Adresse)" % (seen[n] + 1, i + 1, n))
        else:
            seen[n] = i
    return names, problems


def publish_url_fix(url: str) -> Optional[str]:
    """Die BELABOX nimmt Kamerastreams unter /publish/<Name> an. Gibt die korrigierte Adresse zurueck,
    oder None, wenn sie schon stimmt bzw. keine brauchbare Adresse ist. Host/Port bleiben unveraendert
    (die Kamera erreicht die Box je nach Netz unter einer anderen Adresse als das Dashboard)."""
    u = (url or "").strip()
    if not u:
        return None
    try:
        s = urlsplit(u)
    except ValueError:
        return None
    parts = [p for p in s.path.split("/") if p]
    if s.scheme != "rtmp" or not s.hostname or not parts:
        return None
    if len(parts) >= 2 and parts[0] == "publish":
        return None
    name = parts[-1]
    if not CAM_RE.match(name):
        return None
    return urlunsplit((s.scheme, s.netloc, "/publish/" + name, "", ""))


def _clamp(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def build_box_config(pip: PipConfig, names: list) -> dict:
    """Dashboard-Konfiguration -> Konfiguration fuer den Dienst auf der Box.

    Hauptbild = names[0]. Kleine Bilder = Kameras 2..4 in Listenreihenfolge; fehlt ein gespeichertes Fenster, gilt ein Standard
    (Ecken br, bl, tr). Gespeicherte Fenster fuer Kameras, die es nicht mehr gibt, werden ignoriert."""
    if not names:
        raise ValueError("Keine Kamera konfiguriert")
    by_cam = {w.cam: w for w in pip.windows}
    pips = []
    for i, cam in enumerate(names[1:1 + MAX_PIPS]):
        w = by_cam.get(cam) or PipWindow(cam=cam, corner=DEFAULT_CORNERS[i % len(DEFAULT_CORNERS)])
        corner = w.corner if w.corner in CORNERS else DEFAULT_CORNERS[i % len(DEFAULT_CORNERS)]
        color = w.color if COLOR_RE.match(w.color or "") else "ffffff"
        pips.append({
            "cam": cam, "enabled": bool(w.enabled), "corner": corner,
            "fx": _clamp(w.fx, 0, 1, 1.0), "fy": _clamp(w.fy, 0, 1, 1.0),
            "size_pct": _clamp(w.size_pct, 8, 50, 25.0), "border": int(_clamp(w.border, 0, 40, 4)),
            "color": color.lower(), "delay_ms": int(_clamp(w.delay_ms, 0, 3000, 0)),
            "crop_left": _clamp(w.crop_left, 0, 45, 0.0), "crop_right": _clamp(w.crop_right, 0, 45, 0.0),
            "crop_top": _clamp(w.crop_top, 0, 45, 0.0), "crop_bottom": _clamp(w.crop_bottom, 0, 45, 0.0),
        })
    audio = pip.audio if pip.audio == "main" or pip.audio in [p["cam"] for p in pips] or pip.audio == names[0] else "main"
    if audio == names[0]:
        audio = "main"
    return {"main": names[0], "audio": audio, "main_delay_ms": int(_clamp(pip.main_delay_ms, 0, 3000, 0)),
            "smooth_ms": int(_clamp(pip.smooth_ms, 0, 500, 100)), "pips": pips}


def validate_windows(pip: PipConfig, names: list) -> None:
    """Strenge Pruefung fuer Eingaben aus dem Browser (wirft ValueError mit deutscher Meldung)."""
    seen = set()
    for w in pip.windows:
        if not CAM_RE.match(w.cam or ""):
            raise ValueError("Ungueltiger Kamera-Name")
        if w.cam in seen:
            raise ValueError("Kamera %s kommt doppelt vor" % w.cam)
        seen.add(w.cam)
        if w.corner not in CORNERS:
            raise ValueError("Unbekannte Position bei %s" % w.cam)
        if not COLOR_RE.match(w.color or ""):
            raise ValueError("Rahmenfarbe bei %s muss RRGGBB sein" % w.cam)
        for label, v, lo, hi in (("Groesse", w.size_pct, 8, 50), ("Rahmen", w.border, 0, 40), ("Versatz", w.delay_ms, 0, 3000),
                                 ("Position X", w.fx, 0, 1), ("Position Y", w.fy, 0, 1),
                                 ("Beschnitt links", w.crop_left, 0, 45), ("Beschnitt rechts", w.crop_right, 0, 45),
                                 ("Beschnitt oben", w.crop_top, 0, 45), ("Beschnitt unten", w.crop_bottom, 0, 45)):
            if not (lo <= v <= hi):
                raise ValueError("%s bei %s muss zwischen %s und %s liegen" % (label, w.cam, lo, hi))
    if not (0 <= pip.main_delay_ms <= 3000):
        raise ValueError("Versatz des Hauptbilds muss zwischen 0 und 3000 ms liegen")
    if not (0 <= pip.smooth_ms <= 500):
        raise ValueError("Glaettung muss zwischen 0 und 500 ms liegen")
    if pip.audio != "main" and not CAM_RE.match(pip.audio or ""):
        raise ValueError("Ungueltige Ton-Quelle")


def build_bundle() -> bytes:
    """tar.gz mit den Box-Dateien (liegen im Dashboard-Image unter belabox-pip/)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in BUNDLE_FILES:
            path = os.path.join(BUNDLE_DIR, name)
            if not os.path.isfile(path):
                raise FileNotFoundError("PiP-Paket unvollstaendig: %s fehlt" % name)
            ti = tar.gettarinfo(path, arcname=name)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mode = 0o755 if name in ("irl-pip-cli", "irl-pip-setup.sh") else 0o644
            with open(path, "rb") as f:
                tar.addfile(ti, f)
    return buf.getvalue()


def publishing_streams(stat_xml: str) -> set:
    """Namen aller Streams, an denen gerade ein Sender (Kamera) veroeffentlicht - aus der nginx-rtmp-Statistik (/stat)."""
    try:
        root = ET.fromstring(stat_xml.strip())
    except ET.ParseError:
        raise ValueError("nginx-Statistik nicht lesbar")
    live = set()
    for st in root.iter("stream"):
        name = (st.findtext("name") or "").strip()
        if name and any(c.find("publishing") is not None for c in st.iter("client")):
            live.add(name)
    return live
