"""Bild-in-Bild (PiP): reine Logik fuer das Dashboard (ohne SSH, ohne FastAPI - dadurch testbar).

Regeln (Nutzerwunsch 12.10.2026):
  * Die ERSTE Kamera in der Dashboard-Kameraliste (dji_cameras[0]) ist immer das Hauptbild.
  * Die 2., 3. und 4. Kamera koennen als kleine Bilder ueber das Hauptbild gelegt werden (Position frei verschiebbar).
  * Jede Kamera hat auf der Box einen Namen = letztes Stueck ihrer RTMP-Adresse (.../publish/<Name>).

Die Box (Dienst irl-pip) validiert alles noch einmal selbst; hier wird nur in ein sauberes, vollstaendiges Layout uebersetzt.

IRL-PiP 2.0 (seit V1.112 der einzige Modus, PiP 1.0 ist entfernt): Szenen (benannte Anordnungen: welche Kamera gross,
welche klein + Position/Groesse/Crop/Rand, Tonquelle) und eine aktive Szene. Ein Szenenwechsel schaltet auf der Box nur
die Steuerdatei mix2.conf um - ohne Encoder-Neustart. Sync bleibt je Kamera (main_delay_ms / delay_ms der Fenster) und
gilt fuer alle Szenen. Die vordefinierten Szenen erzeugt derselbe Code wie auf der Box (belabox-pip/irl_pip2_gen.py).
"""
import io
import os
import re
import sys
import tarfile
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, field_validator

CAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
MAX_PIPS = 3
CORNERS = ("tl", "tr", "bl", "br", "free")
DEFAULT_CORNERS = ("br", "bl", "tr")
# belaUI-Pipeline-ID von "custom/irl_pip" = SHA-1 des Pipeline-Pfads (so berechnet belaUI die IDs)
PIP_PIPELINE_ID = "6cb5071f05535a7516d7f5c3d6f8744d086b4162"
PIP_PIPELINE_LABEL = "Bild-in-Bild RTMP Kameras (/publish/dji1 + /publish/dji2 usw.)"
BUNDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "belabox-pip")
BUNDLE_FILES = ("libgstirlpip2-aarch64.so", "irl-pip-ctl.py", "irl_pip2_gen.py", "irl-pip-cli",
                "irl-pip-setup.sh", "SHA256SUMS")
OPTIONAL_BUNDLE_FILES = ()
MODES = ("v2",)                 # PiP 1.0 ("v1") ist seit V1.112 entfernt, siehe PipConfig._immer_v2
MAX_SCENES = 12
SCENE_RE = re.compile(r"^[0-9A-Za-z\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00df _.()+-]{1,32}$")
LATENCY_MIN, LATENCY_MAX = 500, 3000


def _gen2():
    """Box-Generator IRL-PiP 2.0 (liegt im Paket belabox-pip/) - eine Quelle fuer die vordefinierten Szenen."""
    if BUNDLE_DIR not in sys.path:
        sys.path.insert(0, BUNDLE_DIR)
    import irl_pip2_gen
    return irl_pip2_gen


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


class PipScene(BaseModel):
    """IRL-PiP 2.0: eine benannte Anordnung. 'main' fuellt das ganze Bild, 'windows' sind die kleinen Bilder (delay_ms
    wird hier nicht benutzt - Sync gehoert zur Kamera, siehe PipConfig.main_delay_ms / windows[].delay_ms)."""
    name: str
    main: str = ""              # Kamera-Name des grossen Bilds ("" = erste Kamera)
    audio: str = "main"         # "main" (Ton der grossen Kamera), "none" (Stille) oder Kamera-Name
    windows: list[PipWindow] = []


class PipConfig(BaseModel):
    windows: list[PipWindow] = []
    audio: str = "main"         # "main" oder Kamera-Name
    main_delay_ms: int = 0
    smooth_ms: int = 100        # Glaettung / Reserve gegen schubweise Ankunft (0..500)
    mode: str = "v2"            # immer "v2" (IRL-PiP 2.0); aeltere gespeicherte Angabe "v1" wird beim Laden zu "v2"
    latency_ms: int = 1500      # v2: Gesamtpuffer 500..3000 ms (Aenderung = Encoder-Neustart)
    scenes: list[PipScene] = []  # v2: leer = vordefinierte Szenen (Standard, Getauscht, Nur Hauptbild, Nur Facecam)
    active_scene: str = "Standard"

    @field_validator("mode", mode="before")
    @classmethod
    def _immer_v2(cls, v):
        """PiP 1.0 gibt es nicht mehr: jede gespeicherte oder gesendete Angabe (auch "v1") gilt als "v2". Nie ein Fehler,
        damit eine alte Konfigurationsdatei immer ladbar bleibt."""
        return "v2"


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
    box = {"main": names[0], "audio": audio, "main_delay_ms": int(_clamp(pip.main_delay_ms, 0, 3000, 0)),
           "smooth_ms": int(_clamp(pip.smooth_ms, 0, 500, 100)), "pips": pips}
    # IRL-PiP 2.0: Modus, Puffer, Szenen (Kameras, die es nicht mehr gibt, fallen weg - wie bei den Fenstern oben)
    g2 = _gen2()
    cams = g2.slot_cams(box)
    if pip.scenes:
        scenes, seen = [], set()
        for sc in pip.scenes[:MAX_SCENES]:
            raw = {"name": sc.name, "main": sc.main or names[0], "audio": sc.audio,
                   "pips": [w.model_dump() for w in sc.windows]}
            try:
                q = g2.validate_scene(raw, cams, names[0])
            except ValueError:
                continue
            if q["name"] not in seen:
                seen.add(q["name"])
                scenes.append(q)
    else:
        scenes = g2.default_scenes(box)
    names_sc = [s["name"] for s in scenes]
    box.update(mode="v2",
               latency_ms=int(_clamp(pip.latency_ms, LATENCY_MIN, LATENCY_MAX, 1500)), scenes=scenes,
               active_scene=pip.active_scene if pip.active_scene in names_sc else (names_sc[0] if names_sc else ""))
    return box


def effective_scenes(pip: PipConfig, names: list) -> list:
    """Szenen fuer das Dashboard (Format von PipScene): die gespeicherten oder - solange keine gespeichert sind - die
    vordefinierten. Fenster fuer alle Kameras ausser der grossen (fehlende ausgeblendet), damit der Editor jede Kamera
    einer Szene ein-/ausblenden kann."""
    if not names:
        return []
    box = build_box_config(pip, names)
    cams = _gen2().slot_cams(box)
    out = []
    for sc in box["scenes"]:
        by_cam = {p["cam"]: p for p in sc["pips"]}
        windows = []
        for i, cam in enumerate(c for c in cams if c != sc["main"]):
            p = by_cam.get(cam)
            if p is None:
                p = dict(PipWindow(cam=cam, corner=DEFAULT_CORNERS[i % len(DEFAULT_CORNERS)]).model_dump(), enabled=False)
            windows.append(PipWindow(**dict(p, delay_ms=0)).model_dump())
        out.append({"name": sc["name"], "main": sc["main"], "audio": sc["audio"], "windows": windows})
    return out


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
    if not (LATENCY_MIN <= pip.latency_ms <= LATENCY_MAX):
        raise ValueError("Puffer muss zwischen %d und %d ms liegen" % (LATENCY_MIN, LATENCY_MAX))
    if len(pip.scenes) > MAX_SCENES:
        raise ValueError("Hoechstens %d Szenen" % MAX_SCENES)
    seen_sc = set()
    for sc in pip.scenes:
        if not SCENE_RE.match(sc.name or "") or sc.name != sc.name.strip():
            raise ValueError("Ungueltiger Szenen-Name '%s' (1-32 Zeichen: Buchstaben, Ziffern, Leerzeichen, . - _ ( ) +)" % sc.name)
        if sc.name in seen_sc:
            raise ValueError("Szene %s gibt es doppelt" % sc.name)
        seen_sc.add(sc.name)
        if sc.main and not CAM_RE.match(sc.main):
            raise ValueError("Szene %s: ungueltige Kamera fuer das grosse Bild" % sc.name)
        if sc.audio not in ("main", "none") and not CAM_RE.match(sc.audio or ""):
            raise ValueError("Szene %s: ungueltige Ton-Quelle" % sc.name)
        try:
            validate_windows(PipConfig(windows=sc.windows), names)
        except ValueError as exc:
            raise ValueError("Szene %s: %s" % (sc.name, exc))
    if pip.scenes and pip.active_scene not in seen_sc:
        raise ValueError("Aktive Szene %s gibt es nicht" % pip.active_scene)


def build_bundle() -> bytes:
    """tar.gz mit den Box-Dateien (liegen im Dashboard-Image unter belabox-pip/)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in BUNDLE_FILES + OPTIONAL_BUNDLE_FILES:
            path = os.path.join(BUNDLE_DIR, name)
            if not os.path.isfile(path):
                if name in OPTIONAL_BUNDLE_FILES:
                    continue
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
