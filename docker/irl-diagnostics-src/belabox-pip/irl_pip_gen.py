"""IRL-PiP: Konfiguration -> belacoder-Pipeline + Steuerdatei des Mischers (irlpipmix).

Reine Funktionen ohne Zugriff auf die Box (testbar). Der Dienst irl-pip-ctl.py benutzt sie.

Konfiguration (JSON, siehe validate_config):
  main            Kamera-Schluessel des Hauptbilds (RTMP-Name unter rtmp://127.0.0.1/publish/<key>)
  audio           "main" oder ein Kamera-Schluessel: Ton von dieser Kamera
  main_delay_ms   Verzoegerung von Hauptbild und Ton (0..3000)
  margin_pct      Abstand zum Rand bei Eckpositionen (Prozent der Breite)
  smooth_ms       Glaettung: Mindest-Reserve gegen Ankunftsschwankungen der kleinen Bilder (0..500, Standard 100)
  pips            bis zu 3 kleine Bilder: cam, enabled, corner (tl|tr|bl|br|free), fx, fy (0..1, nur free),
                  size_pct (8..50, Breite in Prozent des Hauptbilds), border (0..40 px), color (RRGGBB), delay_ms (0..3000),
                  crop_left/crop_right/crop_top/crop_bottom (0..45, Prozent der Kameraquelle; beschneidet das Bild VOR dem
                  Verkleinern, z. B. um aus 16:9 ein Quadrat zu machen - das kleine Bild wird dabei NIE groesser als es
                  ohne Beschnitt waere (gleiches size_pct, volles Kamerabild); der Beschnitt kann es nur verkleinern,
                  der Ausschnitt wird in diese unveraenderte Referenzgroesse eingepasst, nicht gestreckt)
"""
import re

MAX_PIPS = 3
CAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")
CORNERS = ("tl", "tr", "bl", "br", "free")
RTMP_BASE = "rtmp://127.0.0.1/publish"
MIX_CONF = "/run/irl-pip/mix.conf"
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


def validate_config(raw):
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
    Wird von V1.108 (resolve_layout) und IRL-PiP 2.0 (irl_pip2_gen) gemeinsam benutzt."""
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


def resolve_layout(cfg, live):
    """live: {kamera: (breite, hoehe)} der gerade sendenden Kameras. Liefert das wirklich nutzbare Layout oder None.

    Faellt die Hauptkamera aus, wird die erste sendende Kamera der Liste Hauptbild; fehlende kleine Bilder entfallen.
    """
    order = [cfg["main"]] if cfg["main"] else []
    order += [p["cam"] for p in cfg["pips"] if p["enabled"]]
    alive = [c for c in order if c in live]
    if not alive:
        return None
    main = cfg["main"] if cfg["main"] in live else alive[0]
    main_res = tuple(live[main]) if live[main] and live[main][0] else DEFAULT_RES
    W, H = main_res
    pips = []
    for p in cfg["pips"]:
        if not p["enabled"] or p["cam"] == main or p["cam"] not in live:
            continue
        cw, ch = live[p["cam"]] if live[p["cam"]] and live[p["cam"]][0] else DEFAULT_RES
        w, h, cl, cr, ct, cb = pip_geometry(p, W, H, cw, ch)
        pips.append(dict(p, slot=len(pips), w=w, h=h, crop_l=cl, crop_r=cr, crop_t=ct, crop_b=cb))
    audio = cfg["audio"]
    if audio == "main" or audio not in ([main] + [p["cam"] for p in pips]):
        audio = main
    return {"main": main, "main_res": (W, H), "audio": audio, "main_delay_ms": cfg["main_delay_ms"],
            "margin_pct": cfg["margin_pct"], "smooth_ms": cfg["smooth_ms"], "pips": pips}


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


def render_mix_conf(layout):
    """Inhalt der Steuerdatei fuer irlpipmix (wird live nachgeladen)."""
    lines = ["# IRL-PiP Steuerdatei (vom Dienst erzeugt)", "margin=%d" % layout["smooth_ms"]]
    for p in layout["pips"]:
        x, y = pip_position(layout, p)
        lines.append("slot=%d enabled=1 x=%d y=%d border=%d color=%s delay=%d"
                     % (p["slot"], x, y, p["border"], p["color"], p["delay_ms"]))
    return "\n".join(lines) + "\n"


def restart_signature(layout):
    """Aenderungen hieran brauchen einen Neustart des Encoders (Pipeline wird neu gebaut)."""
    return (layout["main"], layout["main_res"], layout["audio"], layout["main_delay_ms"],
            tuple((p["cam"], p["slot"], p["w"], p["h"], p["crop_l"], p["crop_r"], p["crop_t"], p["crop_b"])
                  for p in layout["pips"]))


def _delay_queue(ms, extra=""):
    if ms > 0:
        return ("queue max-size-time=%d max-size-buffers=0 max-size-bytes=0 min-threshold-time=%d%s"
                % ((ms + 1000) * 1000000, ms * 1000000, extra))
    return "queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0%s" % extra


def render_pipeline(layout, mix_conf=MIX_CONF):
    """belacoder-Pipeline (eine Datei). Namen v_delay, a_delay, venc_bps und appsink erwartet belacoder."""
    big = "queue max-size-time=10000000000 max-size-buffers=1000 max-size-bytes=41943040"
    main, audio = layout["main"], layout["audio"]
    mdelay = layout["main_delay_ms"]
    cams = [main] + [p["cam"] for p in layout["pips"]]
    out = []
    for i, cam in enumerate(cams):
        out.append("rtmpsrc location=%s/%s do-timestamp=true !\nflvdemux name=d%d\n" % (RTMP_BASE, cam, i))
    # Hauptbild: Hardware-Decoder -> Mischer -> Encoder
    out.append("d0.video !\n%s !\nh264parse ! mppvideodec ! video/x-raw,format=NV12 !\n"
               "irlpipmix name=pipmix config-file=%s !\n"
               "queue max-size-time=500000000 max-size-buffers=4 leaky=downstream !\n"
               "identity name=v_delay signal-handoffs=TRUE !\n"
               "mpph265enc zero-copy-pkt=0 qp-max=51 gop=60 name=venc_bps !\n"
               "h265parse config-interval=-1 ! %s ! mux.\n" % (_delay_queue(mdelay), mix_conf, big))
    # kleine Bilder: im Decoder verkleinert (ohne Beschnitt) bzw. beschnitten+skaliert (mit Beschnitt), in den Ringspeicher des Mischers
    for p in layout["pips"]:
        i = cams.index(p["cam"])
        cl, cr, ct, cb = p["crop_l"], p["crop_r"], p["crop_t"], p["crop_b"]
        if cl or cr or ct or cb:
            # Beschnitt braucht das volle Decoder-Bild (kein Hardware-Resize beim Decodieren); Zuschnitt + Skalierung per Software.
            out.append("d%d.video !\nqueue max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0 !\n"
                       "h264parse ! mppvideodec ! video/x-raw,format=NV12 !\n"
                       "videocrop left=%d right=%d top=%d bottom=%d !\n"
                       "videoscale ! video/x-raw,format=NV12,width=%d,height=%d !\n"
                       "queue max-size-time=300000000 max-size-buffers=2 leaky=downstream ! irlpipsink slot=%d\n"
                       % (i, cl, cr, ct, cb, p["w"], p["h"], p["slot"]))
        else:
            out.append("d%d.video !\nqueue max-size-time=3000000000 max-size-buffers=0 max-size-bytes=0 !\n"
                       "h264parse ! mppvideodec width=%d height=%d ! video/x-raw,format=NV12 !\n"
                       "queue max-size-time=300000000 max-size-buffers=2 leaky=downstream ! irlpipsink slot=%d\n"
                       % (i, p["w"], p["h"], p["slot"]))
    # Ton: nur eine Kamera, die anderen werden verworfen (sonst staut der Demuxer)
    for i, cam in enumerate(cams):
        if cam == audio:
            d = mdelay
            if cam != main:
                d += next(p["delay_ms"] for p in layout["pips"] if p["cam"] == cam)
            out.append("d%d.audio !\n%s !\naacparse ! avdec_aac ! identity name=a_delay signal-handoffs=TRUE !\n"
                       "audioconvert ! audioresample quality=10 sinc-filter-mode=1 ! opusenc bitrate=128000 ! opusparse !\n"
                       "queue max-size-time=10000000000 max-size-buffers=1000 ! mux.\n" % (i, _delay_queue(d)))
        else:
            out.append("d%d.audio !\nqueue max-size-buffers=4 leaky=downstream ! fakesink sync=false async=false\n" % i)
    out.append("mpegtsmux name=mux !\nappsink name=appsink\n")
    return "\n".join(out)
