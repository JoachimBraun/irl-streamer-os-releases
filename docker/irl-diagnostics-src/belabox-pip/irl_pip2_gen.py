"""IRL-PiP 2.0: Konfiguration -> belacoder-Pipeline (irlpip2) + Steuerdatei mix2.conf mit Szenen.

Reine Funktionen ohne Zugriff auf die Box (testbar). Der Dienst irl-pip-ctl.py benutzt sie. Seit V1.112 gibt es nur
noch diesen Weg (PiP 1.0 mit irl_pip_gen.py/irlpipmix ist entfernt); eine alte Konfiguration mit mode "v1" wird als
"v2" gelesen.

Grundidee (siehe SPEC.md): ALLE Kameras werden staendig dekodiert und landen je in einem Ringspeicher (irlpip2vsink/
irlpip2asink, ein Platz = slot je Kamera). Der Taktgeber irlpip2vsrc/irlpip2asrc setzt daraus im festen 30-fps-Takt das
Ausgabebild 1920x1080 zusammen - WIE, steht allein in der Steuerdatei mix2.conf (wird alle 200 ms auf Aenderung
geprueft). Ein Szenenwechsel (wer gross, wer klein, Position, Groesse, Crop, Rand, Ton, Sync) schreibt daher nur diese
Datei neu: kein Pipeline-Neubau, kein Encoder-Neustart.

Kameras entkoppelt (Phase 2): belacoder beendet sich bei JEDEM Fehler auf dem Pipeline-Bus. Deshalb enthaelt die
belacoder-Pipeline KEINE Kamera-Elemente mehr, nur Taktgeber + Encoder + Muxer (render_pipeline). Jede Kamera laeuft
in einem eigenen Zubringer-Prozess (feeder_argv: gst-launch-1.0 rtmpsrc ! flvdemux ! ... ! irlpip2vsink/asink shm=...),
der die Bilder/Ton in den gemeinsamen Speicher /dev/shm/irlpip2-slotN legt. Stirbt ein Zubringer (Verbindungsabbruch,
Kamera aus, Decoderfehler), startet der Dienst ihn nach 1 s neu - die Sendung laeuft ungestoert weiter (letztes Bild
bis 2 s, dann ausgeblendet, Ton = Stille). Kamera kommt/geht: nur Zubringer + mix2.conf, kein Encoder-Neustart.
Die Pipeline haengt nur noch von Modus und Ausgabegroesse ab - siehe restart_signature(). Auch der Gesamtpuffer
(latency) wirkt live ueber die Steuerdatei (die Ringe sind fuer bis zu 3000 ms Puffer + 3000 ms delay ausgelegt).

Konfigurationsfelder: main/audio/main_delay_ms/margin_pct/smooth_ms/pips (Grundfelder, siehe _validate_base) und
  mode          immer "v2" (aeltere Angaben "v1" werden still auf "v2" gesetzt)
  latency_ms    Gesamtpuffer 500..3000 ms (Standard 1500); wirkt live (Steuerdatei), Zubringer starten neu
  scenes        Liste benannter Anordnungen (hoechstens MAX_SCENES):
                  name   Anzeigename (1..32 Zeichen: Buchstaben, Ziffern, Leerzeichen, . - _ ( ) +)
                  main   Kamera, die das ganze Bild fuellt (Ebene 0)
                  audio  "main" (Ton der grossen Kamera), "none" (Stille) oder ein Kamera-Schluessel
                  pips   bis zu 3 kleine Bilder wie in den Grundfeldern (cam, enabled, corner, fx, fy, size_pct, border, color,
                         crop_left/right/top/bottom in Prozent) - delay_ms wird hier NICHT benutzt
  active_scene  Name der gerade gezeigten Szene
Kamera-Plaetze (slot): 0 = main, 1.. = pips in Listenreihenfolge (Grundfelder; alle Kameras, auch ausgeblendete).
Sync (Bild+Ton je Kamera gemeinsam): Verzoegerung der Hauptkamera = main_delay_ms, der anderen = ihr delay_ms aus pips.
Im v2-Modus wirkt das live ueber die Steuerdatei (slot=N delay=...).
"""
import re

MODES = ("v2",)
LEGACY_MODES = ("v1", "v2")   # "v1" nur noch beim Einlesen alter Konfigurationen erlaubt
OUT_W, OUT_H = 1920, 1080
MAX_SLOTS = 4
MAX_LAYERS = 4
MAX_SCENES = 12
LATENCY_MIN, LATENCY_MAX, LATENCY_DEFAULT = 500, 3000, 1500
DELAY_MAX = 3000
CROP_MAX_PERMILLE = 450
SCENE_RE = re.compile(r"^[0-9A-Za-z\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00df _.()+-]{1,32}$")
MIX2_CONF = "/run/irl-pip/mix2.conf"
PLUGIN_ELEMENTS = ("irlpip2vsink", "irlpip2asink", "irlpip2vsrc", "irlpip2asrc")
SHM_NAME = "irlpip2"            # gemeinsamer Speicher der Zubringer: /dev/shm/irlpip2-slot0..3
GST_LAUNCH = "gst-launch-1.0"

# ---- gemeinsame Grundlagen (frueher irl_pip_gen.py; PiP 1.0 ist seit V1.112 entfernt) ----------------------------
MAX_PIPS = 3
CAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
CORNERS = ("tl", "tr", "bl", "br", "free")
RTMP_BASE = "rtmp://127.0.0.1/publish"
DEFAULT_RES = (1920, 1080)

DEFAULT_CONFIG = {
    "main": "", "audio": "main", "main_delay_ms": 0, "margin_pct": 2.0, "smooth_ms": 100, "pips": [],
}


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def _float(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _validate_base(raw):
    """Prueft und bereinigt eine Konfiguration. Wirft ValueError mit deutscher Meldung bei unbrauchbaren Eingaben."""
    if not isinstance(raw, dict):
        raise ValueError("Konfiguration muss ein Objekt sein")
    main = raw.get("main", "")
    if main and (not isinstance(main, str) or not CAM_RE.match(main)):
        raise ValueError("Ungueltiger Kamera-Name beim Hauptbild")
    audio = raw.get("audio", "main")
    if audio != "main" and (not isinstance(audio, str) or not CAM_RE.match(audio)):
        raise ValueError("Ungueltige Ton-Quelle")
    pips_in = raw.get("pips", [])
    if not isinstance(pips_in, list):
        raise ValueError("pips muss eine Liste sein")
    if len(pips_in) > MAX_PIPS:
        raise ValueError("Hoechstens %d kleine Bilder" % MAX_PIPS)
    pips, seen = [], {main} if main else set()
    for i, p in enumerate(pips_in):
        if not isinstance(p, dict):
            raise ValueError("Kleines Bild %d ist ungueltig" % (i + 1))
        cam = p.get("cam", "")
        if not isinstance(cam, str) or not CAM_RE.match(cam):
            raise ValueError("Kleines Bild %d: ungueltiger Kamera-Name" % (i + 1))
        if cam in seen:
            raise ValueError("Kamera %s ist schon belegt (jede Kamera nur einmal)" % cam)
        seen.add(cam)
        corner = p.get("corner", "br")
        if corner not in CORNERS:
            raise ValueError("Kleines Bild %d: unbekannte Ecke" % (i + 1))
        color = p.get("color", "ffffff")
        if not isinstance(color, str) or not COLOR_RE.match(color):
            raise ValueError("Kleines Bild %d: Farbe muss RRGGBB sein" % (i + 1))
        pips.append({
            "cam": cam, "enabled": bool(p.get("enabled", True)), "corner": corner,
            "fx": _float(p.get("fx"), 0.0, 1.0, 1.0), "fy": _float(p.get("fy"), 0.0, 1.0, 1.0),
            "size_pct": _float(p.get("size_pct"), 8.0, 50.0, 25.0),
            "border": _int(p.get("border"), 0, 40, 4), "color": color.lower(),
            "delay_ms": _int(p.get("delay_ms"), 0, 3000, 0),
            "crop_left": _float(p.get("crop_left"), 0.0, 45.0, 0.0), "crop_right": _float(p.get("crop_right"), 0.0, 45.0, 0.0),
            "crop_top": _float(p.get("crop_top"), 0.0, 45.0, 0.0), "crop_bottom": _float(p.get("crop_bottom"), 0.0, 45.0, 0.0),
        })
    if audio != "main" and audio not in seen:
        raise ValueError("Ton-Quelle %s ist keine der gewaehlten Kameras" % audio)
    return {
        "main": main, "audio": audio, "main_delay_ms": _int(raw.get("main_delay_ms"), 0, 3000, 0),
        "margin_pct": _float(raw.get("margin_pct"), 0.0, 10.0, 2.0), "smooth_ms": _int(raw.get("smooth_ms"), 0, 500, 100), "pips": pips,
    }


def pip_geometry(p, W, H, cw, ch):
    """Groesse des kleinen Bilds und Beschnitt in Pixeln -> (w, h, cl, cr, ct, cb).

    W, H: Hauptbild/Ausgabe, cw, ch: Kameraquelle. p: kleines Bild mit size_pct und crop_* (Prozent).
    """
    # Beschnitt in Pixel, VOR dem Verkleinern (gerade Werte, damit der NV12-Chroma-Plan passt)
    cl = int(round(cw * p["crop_left"] / 100.0)) & ~1
    cr = int(round(cw * p["crop_right"] / 100.0)) & ~1
    ct = int(round(ch * p["crop_top"] / 100.0)) & ~1
    cb = int(round(ch * p["crop_bottom"] / 100.0)) & ~1
    src_w = max(16, cw - cl - cr)
    src_h = max(16, ch - ct - cb)
    # Referenzgroesse: Fenstergroesse OHNE Beschnitt (nur size_pct, volles Seitenverhaeltnis der Kamera).
    # Der Beschnitt darf das Fenster nie ueber diese Groesse hinaus vergroessern, nur verkleinern.
    w0 = int(round(W * p["size_pct"] / 100.0 / 16.0)) * 16
    w0 = max(64, min(w0, (W // 2) // 16 * 16))
    h0 = int(round(w0 * ch / float(cw) / 2.0)) * 2
    if cl or cr or ct or cb:
        # beschnittenes Bild so gross wie moeglich in die Referenzbox (w0 x h0) einpassen ("contain"),
        # Seitenverhaeltnis des Ausschnitts erhalten - nie breiter als w0 und nie hoeher als h0.
        if w0 * src_h / float(src_w) <= h0:
            w, h = w0, int(round(w0 * src_h / float(src_w) / 2.0)) * 2
        else:
            h = h0
            w = int(round(h0 * src_w / float(src_h) / 16.0)) * 16
            w = max(64, min(w, (W // 2) // 16 * 16))
            h = int(round(w * src_h / float(src_w) / 2.0)) * 2
    else:
        w, h = w0, h0
    # Sicherheitsgrenze (unabhaengig vom Beschnitt): das kleine Bild darf nie hoeher werden als das
    # Hauptbild selbst (z. B. eine hochformatige Kamera auf einem querformatigen Hauptbild).
    max_h = (H * 90 // 100) // 2 * 2
    if h > max_h:
        h = max_h
        w = int(round(h * src_w / float(src_h) / 16.0)) * 16
        w = max(64, min(w, (W // 2) // 16 * 16))
        h = int(round(w * src_h / float(src_w) / 2.0)) * 2
    return w, h, cl, cr, ct, cb


def pip_position(layout, p):
    """Linke obere Ecke des kleinen Bilds in Bildpunkten (gerade Werte)."""
    W, H = layout["main_res"]
    m = int(round(W * layout["margin_pct"] / 100.0)) & ~1
    w, h = p["w"], p["h"]
    if p["corner"] == "free":
        x = int(round(p["fx"] * max(0, W - w)))
        y = int(round(p["fy"] * max(0, H - h)))
    else:
        x = m if p["corner"] in ("tl", "bl") else W - w - m
        y = m if p["corner"] in ("tl", "tr") else H - h - m
    return max(0, min(x, W - w)) & ~1, max(0, min(y, H - h)) & ~1



# Namen der vordefinierten Szenen (Reihenfolge = Reihenfolge der Knoepfe)
SCENE_STANDARD = "Standard"
SCENE_SWAPPED = "Getauscht"
SCENE_MAIN_ONLY = "Nur Hauptbild"
SCENE_FACE_ONLY = "Nur Facecam"


def _pip_defaults(cam, i, enabled=True):
    return {"cam": cam, "enabled": enabled, "corner": ("br", "bl", "tr")[i % 3], "fx": 1.0, "fy": 1.0, "size_pct": 25.0,
            "border": 4, "color": "ffffff", "crop_left": 0.0, "crop_right": 0.0, "crop_top": 0.0, "crop_bottom": 0.0}


def _scene_pip(p):
    """Kleines Bild einer Szene: Grundfelder ohne delay_ms (Sync gehoert zur Kamera, nicht zur Anordnung)."""
    return {k: p[k] for k in ("cam", "enabled", "corner", "fx", "fy", "size_pct", "border", "color",
                              "crop_left", "crop_right", "crop_top", "crop_bottom")}


def slot_cams(cfg):
    """Kamera-Schluessel nach Platz (slot 0 = main). Leere Liste, wenn keine Hauptkamera eingetragen ist."""
    if not cfg.get("main"):
        return []
    return [cfg["main"]] + [p["cam"] for p in cfg.get("pips", [])][:MAX_SLOTS - 1]


def default_scenes(cfg):
    """Vordefinierte Szenen aus der Grundanordnung (main/pips): Standard (wie bisher), Getauscht (Haupt <-> erste Facecam),
    Nur Hauptbild, Nur Facecam. Mit nur einer Kamera gibt es nur Standard."""
    cams = slot_cams(cfg)
    if not cams:
        return []
    main = cams[0]
    audio = cfg.get("audio", "main") or "main"
    std = {"name": SCENE_STANDARD, "main": main, "audio": audio,
           "pips": [_scene_pip(p) for p in cfg.get("pips", [])][:MAX_SLOTS - 1]}
    if len(cams) < 2:
        return [std]
    face = cams[1]
    face_pip = next((p for p in std["pips"] if p["cam"] == face), _pip_defaults(face, 0))
    # Getauscht: die Hauptkamera nimmt Platz, Groesse, Crop und Rand der Facecam ein (die Facecam fuellt das Bild)
    swapped_pips = [dict(face_pip, cam=main, enabled=True)] + [dict(p) for p in std["pips"] if p["cam"] != face]
    # Ton bleibt bei der Kamera, die ihn in Standard liefert (meist das Mikro der Hauptkamera) - nicht bei "gross"
    keep_audio = main if audio == "main" else audio
    return [
        std,
        {"name": SCENE_SWAPPED, "main": face, "audio": keep_audio, "pips": swapped_pips},
        {"name": SCENE_MAIN_ONLY, "main": main, "audio": keep_audio,
         "pips": [dict(p, enabled=False) for p in std["pips"]]},
        {"name": SCENE_FACE_ONLY, "main": face, "audio": face,
         "pips": [dict(p, enabled=False) for p in swapped_pips]},
    ]


def _validate_pip(p, label):
    if not isinstance(p, dict):
        raise ValueError("%s ist ungueltig" % label)
    corner = p.get("corner", "br")
    if corner not in CORNERS:
        raise ValueError("%s: unbekannte Ecke" % label)
    color = p.get("color", "ffffff")
    if not isinstance(color, str) or not COLOR_RE.match(color):
        raise ValueError("%s: Farbe muss RRGGBB sein" % label)
    return {
        "cam": p.get("cam"), "enabled": bool(p.get("enabled", True)), "corner": corner,
        "fx": _float(p.get("fx"), 0.0, 1.0, 1.0), "fy": _float(p.get("fy"), 0.0, 1.0, 1.0),
        "size_pct": _float(p.get("size_pct"), 8.0, 50.0, 25.0),
        "border": _int(p.get("border"), 0, 40, 4), "color": color.lower(),
        "crop_left": _float(p.get("crop_left"), 0.0, 45.0, 0.0), "crop_right": _float(p.get("crop_right"), 0.0, 45.0, 0.0),
        "crop_top": _float(p.get("crop_top"), 0.0, 45.0, 0.0), "crop_bottom": _float(p.get("crop_bottom"), 0.0, 45.0, 0.0),
    }


def validate_scene(raw, cams, fallback_main):
    """Eine Szene pruefen. Kameras, die es (nicht mehr) gibt, fallen weg statt die ganze Konfiguration zu verwerfen."""
    if not isinstance(raw, dict):
        raise ValueError("Szene ist ungueltig")
    name = raw.get("name", "")
    name = name.strip() if isinstance(name, str) else ""
    if not SCENE_RE.match(name):
        raise ValueError("Ungueltiger Szenen-Name (1-32 Zeichen: Buchstaben, Ziffern, Leerzeichen, . - _ ( ) +)")
    main = raw.get("main", "")
    if not isinstance(main, str) or main not in cams:
        main = fallback_main
    audio = raw.get("audio", "main")
    if audio not in ("main", "none") and (not isinstance(audio, str) or audio not in cams):
        audio = "main"
    pips_in = raw.get("pips", [])
    if not isinstance(pips_in, list):
        raise ValueError("Szene %s: pips muss eine Liste sein" % name)
    pips, seen = [], {main}
    for i, p in enumerate(pips_in):
        q = _validate_pip(p, "Szene %s, kleines Bild %d" % (name, i + 1))
        if not isinstance(q["cam"], str) or q["cam"] not in cams or q["cam"] in seen:
            continue
        seen.add(q["cam"])
        pips.append(q)
        if len(pips) >= MAX_LAYERS - 1:
            break
    return {"name": name, "main": main, "audio": audio, "pips": pips}


def validate_config(raw):
    """Grundpruefung (_validate_base) + v2-Felder. Eine alte Konfiguration ohne v2-Felder bleibt gueltig:
    latency 1500 ms, vordefinierte Szenen, aktive Szene Standard. mode ist immer "v2" (alte Angabe "v1" -> "v2")."""
    cfg = _validate_base(raw)
    mode = raw.get("mode", "v2")
    if mode not in LEGACY_MODES:
        raise ValueError("Unbekannter Modus")
    cfg["mode"] = "v2"
    cfg["latency_ms"] = _int(raw.get("latency_ms"), LATENCY_MIN, LATENCY_MAX, LATENCY_DEFAULT)
    cams = slot_cams(cfg)
    scenes_in = raw.get("scenes")
    if scenes_in is None or scenes_in == []:
        scenes = default_scenes(cfg)
    else:
        if not isinstance(scenes_in, list):
            raise ValueError("scenes muss eine Liste sein")
        if len(scenes_in) > MAX_SCENES:
            raise ValueError("Hoechstens %d Szenen" % MAX_SCENES)
        scenes, names = [], set()
        for s in scenes_in:
            sc = validate_scene(s, cams, cfg["main"])
            if sc["name"] in names:
                raise ValueError("Szene %s gibt es doppelt" % sc["name"])
            names.add(sc["name"])
            scenes.append(sc)
        if not cams:
            scenes = []
    cfg["scenes"] = scenes
    active = raw.get("active_scene", SCENE_STANDARD)
    names = [s["name"] for s in scenes]
    cfg["active_scene"] = active if active in names else (names[0] if names else "")
    return cfg


def find_scene(cfg, name):
    return next((s for s in cfg.get("scenes", []) if s["name"] == name), None)


def _res(live, cam):
    r = live.get(cam) if live else None
    return tuple(r) if r and r[0] and r[1] else DEFAULT_RES


def _permille(pct):
    return max(0, min(CROP_MAX_PERMILLE, int(round(float(pct) * 10))))


def main_geometry(cw, ch):
    """Grosses Bild (Ebene 0): ganze Ausgabe; weicht das Seitenverhaeltnis der Kamera > 1 % von 16:9 ab, wird es
    unverzerrt eingepasst und mittig gesetzt (z. B. Hochkant-Kamera) - gerade Pixelwerte."""
    if abs(cw * OUT_H - ch * OUT_W) <= 0.01 * OUT_W * ch:
        return 0, 0, OUT_W, OUT_H
    if cw * OUT_H > ch * OUT_W:
        w, h = OUT_W, int(round(OUT_W * ch / float(cw) / 2.0)) * 2
    else:
        w, h = int(round(OUT_H * cw / float(ch) / 2.0)) * 2, OUT_H
    return ((OUT_W - w) // 2) & ~1, ((OUT_H - h) // 2) & ~1, w, h


def resolve_layout(cfg, live, scene_name=None):
    """-> Layout fuer Pipeline und Steuerdatei. live: {kamera: (breite, hoehe)} der gerade sendenden Kameras.

    Alle eingetragenen Kameras behalten ihren festen Platz (slot); in die Pipeline kommen nur die gerade sendenden
    (eine fehlende Kamera zeigt der Taktgeber als letztes Bild/schwarz bzw. blendet sie aus). Die Ebenen beschreiben die
    gewaehlte Szene - auch Kameras, die gerade nicht senden, damit die Steuerdatei bei Ausfall/Rueckkehr gleich bleibt.
    None, wenn keine Kamera eingetragen ist."""
    live = live or {}
    cams = slot_cams(cfg)
    if not cams:
        return None
    pip_delay = {p["cam"]: p.get("delay_ms", 0) for p in cfg["pips"]}
    slots = []
    for i, cam in enumerate(cams):
        w, h = _res(live, cam)
        delay = cfg["main_delay_ms"] if i == 0 else pip_delay.get(cam, 0)
        slots.append({"slot": i, "cam": cam, "live": cam in live, "w": w, "h": h,
                      "delay": max(0, min(DELAY_MAX, int(delay)))})
    slot_of = {s["cam"]: s["slot"] for s in slots}
    scene = find_scene(cfg, scene_name or cfg.get("active_scene")) or (cfg["scenes"][0] if cfg.get("scenes") else None)
    if scene is None:
        scene = {"name": "", "main": cams[0], "audio": "main", "pips": []}
    big = scene["main"] if scene["main"] in slot_of else cams[0]
    layers = []
    cw, ch = _res(live, big)
    x, y, w, h = main_geometry(cw, ch)
    layers.append({"slot": slot_of[big], "cam": big, "x": x, "y": y, "w": w, "h": h, "cl": 0, "cr": 0, "ct": 0, "cb": 0,
                   "border": 0, "color": "ffffff", "alpha": 100})
    pos_ref = {"main_res": (OUT_W, OUT_H), "margin_pct": cfg.get("margin_pct", 2.0)}
    for p in scene["pips"]:
        if not p["enabled"] or p["cam"] not in slot_of or p["cam"] == big or len(layers) >= MAX_LAYERS:
            continue
        cw, ch = _res(live, p["cam"])
        w, h, _cl, _cr, _ct, _cb = pip_geometry(p, OUT_W, OUT_H, cw, ch)
        x, y = pip_position(pos_ref, dict(p, w=w, h=h))
        layers.append({"slot": slot_of[p["cam"]], "cam": p["cam"], "x": x, "y": y, "w": w, "h": h,
                       "cl": _permille(p["crop_left"]), "cr": _permille(p["crop_right"]),
                       "ct": _permille(p["crop_top"]), "cb": _permille(p["crop_bottom"]),
                       "border": p["border"], "color": p["color"], "alpha": 100})
    audio = scene.get("audio", "main")
    if audio == "none":
        audio_slot = -1
    elif audio in slot_of:
        audio_slot = slot_of[audio]
    else:
        audio_slot = slot_of[big]
    return {"mode": "v2", "scene": scene["name"], "latency": cfg.get("latency_ms", LATENCY_DEFAULT),
            "out": (OUT_W, OUT_H), "slots": slots, "audio": audio_slot, "layers": layers}


def render_mix2_conf(layout):
    """Inhalt der Steuerdatei mix2.conf (SPEC-Format, wird vom Taktgeber live nachgeladen)."""
    name = re.sub(r"[^\w .()+-]", "", layout.get("scene") or "")
    lines = ["# IRL-PiP 2.0 Steuerdatei (vom Dienst erzeugt) - Szene: %s" % name,
             "latency=%d" % layout["latency"],
             "audio=%d" % layout["audio"]]
    for s in layout["slots"]:
        lines.append("slot=%d delay=%d" % (s["slot"], s["delay"]))
    for i, L in enumerate(layout["layers"]):
        lines.append("layer=%d slot=%d x=%d y=%d w=%d h=%d cl=%d cr=%d ct=%d cb=%d border=%d color=%s alpha=%d"
                     % (i, L["slot"], L["x"], L["y"], L["w"], L["h"], L["cl"], L["cr"], L["ct"], L["cb"],
                        L["border"], L["color"], L["alpha"]))
    return "\n".join(lines) + "\n"


def restart_signature(layout):
    """Nur Aenderungen hieran bauen die belacoder-Pipeline neu (Encoder-Neustart): Modus und Ausgabegroesse.
    Kameras (welche, wie viele, Aufloesung, Ausfall/Rueckkehr), Gesamtpuffer, Szene, Anordnung, Crop, Ton und Sync
    wirken live (Zubringer-Prozesse bzw. Steuerdatei mix2.conf)."""
    return ("v2", tuple(layout.get("out", (OUT_W, OUT_H))))


def render_pipeline(layout, conf=MIX2_CONF, shm=SHM_NAME):
    """belacoder-Pipeline: NUR Taktgeber (aus dem gemeinsamen Speicher), Encoder, Muxer - keine Kamera-Elemente, damit
    kein Kamerafehler je ein ERROR auf den belacoder-Bus bringt. Haengt NUR von restart_signature(layout) ab.
    Namen v_delay, a_delay, venc_bps, mux und appsink erwartet belacoder."""
    _mode, (w, h) = restart_signature(layout)
    size = "" if (w, h) == (OUT_W, OUT_H) else " width=%d height=%d" % (w, h)
    out = ["irlpip2vsrc config-file=%s shm=%s%s !\nqueue !\nidentity name=v_delay signal-handoffs=TRUE !\n"
           "mpph265enc zero-copy-pkt=0 qp-max=51 gop=60 name=venc_bps !\n"
           "h265parse config-interval=-1 !\nqueue max-size-time=10000000000 max-size-buffers=1000 max-size-bytes=41943040 !\n"
           "mux.\n" % (conf, shm, size),
           "irlpip2asrc config-file=%s shm=%s !\nidentity name=a_delay signal-handoffs=TRUE !\naudioconvert !\n"
           "opusenc bitrate=128000 ! opusparse !\nqueue max-size-time=10000000000 max-size-buffers=1000 !\nmux.\n" % (conf, shm),
           "mpegtsmux name=mux !\nappsink name=appsink\n"]
    return "\n".join(out)


def feeder_argv(cam, slot, latency=LATENCY_DEFAULT, shm=SHM_NAME, gst=GST_LAUNCH):
    """Kommandozeile (Liste, ohne Shell) des Zubringer-Prozesses einer Kamera: RTMP vom lokalen nginx -> H.264-Hardware-
    Decoder -> irlpip2vsink, AAC -> S16LE 48 kHz -> irlpip2asink, beides in den Platz slot des gemeinsamen Speichers.
    Kamera-Zeitstempel (pts hinter flvdemux) bleiben erhalten: kein do-timestamp, das Zeitmodell braucht sie."""
    if not isinstance(cam, str) or not CAM_RE.match(cam) or not 0 <= int(slot) < MAX_SLOTS:
        raise ValueError("Kamera oder Platz ungueltig")
    # Warteschlangen entkoppeln Bild und Ton hinter flvdemux; die Senken nehmen sofort ab
    q = ["queue", "max-size-time=%d" % ((int(latency) + DELAY_MAX + 1000) * 1000000), "max-size-buffers=0", "max-size-bytes=0"]
    sink = ["slot=%d" % slot, "shm=%s" % shm]
    return ([gst, "-q", "rtmpsrc", "location=%s/%s" % (RTMP_BASE, cam), "!", "flvdemux", "name=d",
             "d.video", "!"] + q + ["!", "h264parse", "!", "mppvideodec", "!", "video/x-raw,format=NV12", "!", "irlpip2vsink"] + sink +
            ["d.audio", "!"] + q + ["!", "aacparse", "!", "avdec_aac", "!", "audioconvert", "!", "audioresample", "!",
             "audio/x-raw,format=S16LE,rate=48000,channels=2", "!", "irlpip2asink"] + sink)


def feeders(layout, shm=SHM_NAME):
    """{slot: argv} der Zubringer fuer alle gerade sendenden Kameras (feste Plaetze wie in der Steuerdatei)."""
    return {s["slot"]: feeder_argv(s["cam"], s["slot"], layout["latency"], shm) for s in layout["slots"] if s["live"]}
