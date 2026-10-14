#!/usr/bin/env python3
"""irl-rtmp-guard - Schutzschicht zwischen Kamera und nginx-rtmp.

Hintergrund: Die DJI Osmo Action 4 sendet ihren RTMP-Strom unregelmaessig kaputt (mitten in einem Frame fehlt ein
Stueck, das Kopfbyte des naechsten Blocks ist falsch). nginx trennt dann sofort die Verbindung, die Kamera gibt auf.
Dieser Dienst liest den Strom der Kamera Block fuer Block mit, gibt alles unveraendert an nginx weiter und repariert
nur die beschaedigte Stelle: das Kopfbyte wird korrigiert bzw. der fehlende Teil des Blocks mit Nullen aufgefuellt,
danach rastet er wieder im Raster ein. nginx sieht einen lueckenlosen, gueltigen Strom, die Kamera merkt nichts.
Kann er sich nicht einrasten, gibt er auf und reicht den Rest unveraendert durch (dann verhaelt sich alles wie ohne
Schutzschicht) - er macht es also nie schlimmer.

Kern (Repairer) ist reine Logik ohne Netzwerk und damit testbar; unten steht der asyncio-Dienst.
"""
import asyncio
import copy
import hashlib
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import time

HANDSHAKE_LEN = 1 + 1536 + 1536          # C0 + C1 + C2
MAX_CSID = 32                            # wie nginx (max_streams 32): groessere Chunk-Stream-IDs sind ungueltig
MAX_MSG = 1 << 20                        # wie nginx max_message (1 MiB)
VALID_TYPES = frozenset((1, 2, 3, 4, 5, 6, 8, 9, 15, 16, 17, 18, 19, 20, 22))
FIXED_LEN = {1: 4, 2: 4, 3: 4, 5: 4, 6: 5}
MARGIN = 160                             # so viele Bytes darf ein Stoerstueck laenger als der Block sein
SIM_CHUNKS = 6                           # so viele Bloecke muessen nach dem Einrasten sauber parsen
MAX_LOOK = 700                           # so viele Bloecke wird hoechstens vorausgeschaut (ca. 90 KB)
AFTER_CHUNKS = 3                         # so viele Bloecke nach Ende der beschaedigten Nachricht muessen sauber sein
INNER_MIN = 4                            # so viele saubere Bloecke braucht es, damit ein spaeterer Fehler als 'zweiter Defekt' gilt
KEEP_HUNT = 60000                        # beim Weitersuchen bleiben nur die letzten so vielen Byte im Puffer
MAX_HUNT = 400000                        # Notfall-Suche: so weit darf nach dem naechsten Nachrichtenanfang gesucht werden
MAX_WAIT = 700000                        # so viele Byte darf der Puffer waehrend des Einrastens wachsen
HUNT_WINDOW = 2048                       # Suchfenster, wenn mitten zwischen zwei Nachrichten etwas kaputt ist

NEED, BAD = 0, 1


class CS:
    """Zustand eines Chunk-Streams (je csid)."""
    __slots__ = ("mlen", "mtype", "msid", "ts", "ext", "ext_val", "got")

    def __init__(self):
        self.mlen = 0
        self.mtype = 0
        self.msid = 0
        self.ts = 0
        self.ext = False
        self.ext_val = b""
        self.got = 0


_DEFAULT = CS()                          # nginx behandelt einen noch unbekannten Chunk-Stream mit Nullwerten (die Action 4 sendet
                                         # ihren ersten Block auf csid 2 gleich mit fmt 1 - ohne vorheriges fmt 0)


class GiveUp(Exception):
    pass


def _i3(b, i):
    return (b[i] << 16) | (b[i + 1] << 8) | b[i + 2]


def read_header(buf, i, n, states):
    """Liest den Kopf eines Blocks bei Position i. Gibt NEED, BAD oder (fmt, csid, hdr_len, new) zurueck.
    new = (mlen, mtype, msid, ts, ext, ext_val, new_message)."""
    if i >= n:
        return NEED
    b0 = buf[i]
    fmt = b0 >> 6
    csid = b0 & 63
    if csid < 2 or csid >= MAX_CSID:
        return BAD                        # 0/1 = Mehrbyte-Form (nie noetig), >=32 = ungueltig
    st = states.get(csid)
    dm = st if st is not None else _DEFAULT
    p = i + 1
    ext = False
    ext_val = b""
    if fmt == 0:
        if n < p + 11:
            return NEED
        ts = _i3(buf, p)
        mlen = _i3(buf, p + 3)
        mtype = buf[p + 6]
        msid = buf[p + 7] | (buf[p + 8] << 8) | (buf[p + 9] << 16) | (buf[p + 10] << 24)
        p += 11
        if msid >= 64:
            return BAD
        new_msg = True
        if ts == 0xFFFFFF:
            ext = True
    elif fmt == 1:
        if n < p + 7:
            return NEED
        ts = _i3(buf, p)
        mlen = _i3(buf, p + 3)
        mtype = buf[p + 6]
        msid = dm.msid
        p += 7
        new_msg = True
        if ts == 0xFFFFFF:
            ext = True
    elif fmt == 2:
        if n < p + 3:
            return NEED
        ts = _i3(buf, p)
        mlen, mtype, msid = dm.mlen, dm.mtype, dm.msid
        p += 3
        new_msg = True
        if ts == 0xFFFFFF:
            ext = True
    else:
        ts = 0
        mlen, mtype, msid = dm.mlen, dm.mtype, dm.msid
        new_msg = not (0 < dm.got < dm.mlen)
        ext = dm.ext
        if ext:
            ext_val = dm.ext_val
    if fmt != 3 and st is not None and 0 < st.got < st.mlen:
        return BAD                        # neue Nachricht, obwohl die alte auf dem Chunk-Stream nicht fertig ist
    if ext:
        if n < p + 4:
            return NEED
        ext_val = bytes(buf[p:p + 4])
        p += 4
    if new_msg:
        if mtype not in VALID_TYPES or mlen > MAX_MSG:
            return BAD
        if mtype in (8, 9) and mlen == 0:
            return BAD
        fl = FIXED_LEN.get(mtype)
        if fl is not None and mlen != fl:
            return BAD
        if mtype == 4 and mlen < 6:
            return BAD
    return fmt, csid, p - i, (mlen, mtype, msid, ts, ext, ext_val, new_msg)


def apply_chunk(buf, i, n, states, cs_in, hdr):
    """Wendet einen gueltig gelesenen Kopf auf den Zustand an. Gibt (Chunk-Ende, Nachricht fertig?, Nutzlast-Start) zurueck oder
    None, wenn die Nutzlast noch nicht vollstaendig da ist (Zustand dann unveraendert)."""
    fmt, csid, hl, (mlen, mtype, msid, ts, ext, ext_val, new_msg) = hdr
    st = states.get(csid)
    got = 0 if new_msg else st.got
    plen = min(cs_in, mlen - got)
    end = i + hl + plen
    if n < end:
        return None
    if st is None:
        st = states[csid] = CS()
    if new_msg:
        st.mlen, st.mtype, st.msid = mlen, mtype, msid
        if fmt == 0:
            st.ts = ts
        elif fmt in (1, 2):
            st.ts = (st.ts + ts) & 0xFFFFFFFF
        st.ext = ext
        st.ext_val = ext_val
        st.got = 0
    st.got += plen
    return end, st.got >= st.mlen, i + hl


class Repairer:
    """Nimmt den Bytestrom Kamera -> Server entgegen und liefert den (reparierten) Strom fuer den Server."""

    def __init__(self, log=None, name="?"):
        self.buf = bytearray()
        self.hs_left = HANDSHAKE_LEN
        self.mode = "handshake"           # handshake | parse | pass
        self.states = {}
        self.cs_in = 128
        self.have_av = False
        self.last_csid = None
        self._try_n = 0
        self._eh = 0
        self.hunt_dropped = 0
        self.av_csid = None
        self._out_extra = bytearray()
        self.log = log or (lambda *a, **k: None)
        self.name = name
        self.stats = {"bytes_in": 0, "bytes_out": 0, "repairs": 0, "exact_fix": 0, "filled": 0, "dropped": 0,
                      "giveups": 0, "messages": 0, "emergency": 0}
        self.dumps = []                   # (Beschreibung, bytes) fuer die Analyse nach einem Eingriff

    # ---------------------------------------------------------------- Eingang
    def feed(self, data):
        self.stats["bytes_in"] += len(data)
        if self.mode == "pass":
            self.stats["bytes_out"] += len(data)
            return bytes(data)
        out = bytearray()
        self.buf += data
        try:
            self._run(out)
        except GiveUp as e:
            self.stats["giveups"] += 1
            self.log("AUFGEGEBEN: %s - reiche den Rest unveraendert durch" % e)
            out += self.buf
            self.buf = bytearray()
            self.mode = "pass"
        self.stats["bytes_out"] += len(out)
        return bytes(out)

    # ---------------------------------------------------------------- Hauptschleife
    def _run(self, out):
        buf = self.buf
        if self.mode == "handshake":
            if self.hs_left == HANDSHAKE_LEN and buf and buf[0] != 3:
                self.mode = "pass"
                out += buf
                buf.clear()
                return
            take = min(self.hs_left, len(buf))
            out += buf[:take]
            del buf[:take]
            self.hs_left -= take
            if self.hs_left:
                return
            self.mode = "parse"
        states = self.states
        i = 0
        n = len(buf)
        start = 0                          # ab hier ist noch nichts nach 'out' kopiert
        while True:
            hdr = read_header(buf, i, n, states)
            if hdr is NEED:
                break
            if hdr is BAD:
                out += buf[start:i]        # alles bis hierher ist in Ordnung und geht unveraendert raus
                del buf[:i]
                i = start = 0
                n = len(buf)
                res = self._resync(buf, 0, n, out)
                if self._out_extra:
                    out += self._out_extra
                    self._out_extra = bytearray()
                if res is None:            # braucht mehr Daten
                    break
                i = start = res
                continue
            r = apply_chunk(buf, i, n, states, self.cs_in, hdr)
            if r is None:
                break
            end, done, pstart = r
            csid = hdr[1]
            self.last_csid = csid
            if done:
                st = states[csid]
                self.stats["messages"] += 1
                if st.mtype in (8, 9):
                    self.have_av = True
                    self.av_csid = csid
                elif st.mtype == 1 and st.mlen == 4 and end - pstart >= 4:
                    v = int.from_bytes(buf[end - 4:end], "big") & 0x7FFFFFFF
                    if 1 <= v <= 0x100000:
                        self.cs_in = v
                elif st.mtype == 2 and end - pstart >= 4:
                    ac = int.from_bytes(buf[end - 4:end], "big")
                    if ac in states:
                        states[ac].got = states[ac].mlen
            i = end
        out += buf[start:i]
        del buf[:i]

    # ---------------------------------------------------------------- Einrasten
    def _sim_ok(self, buf, p, n, states):
        """Parst ab Position p bis zu SIM_CHUNKS Bloecke auf einer Kopie des Zustands. 'ok' / 'bad' / 'need'."""
        sim = {c: copy.copy(s) for c, s in states.items()}
        pos = p
        cs_in = self.cs_in
        for _ in range(SIM_CHUNKS):
            h = read_header(buf, pos, n, sim)
            if h is NEED:
                return "need"
            if h is BAD:
                return "bad"
            r = apply_chunk(buf, pos, n, sim, cs_in, h)
            if r is None:
                return "need"
            pos = r[0]
        return "ok"

    def _resync(self, buf, i, n, out):
        """Bei Position i steht ein ungueltiger Blockkopf. Gibt die neue Position zurueck (Daten ab 'out' bereits behandelt),
        None wenn mehr Daten noetig sind, oder wirft GiveUp."""
        if not self.have_av:
            raise GiveUp("ungueltiger Kopf bei Verbindungsbeginn (kein Audio/Video bisher)")
        states = self.states
        active = [c for c, s in states.items() if 0 < s.got < s.mlen]
        victim = None
        if len(active) == 1:
            victim = active[0]
        elif len(active) > 1 and self.last_csid in active:
            victim = self.last_csid
        elif len(active) > 1:
            return self._emergency(buf, i, n, active, time.time())   # Opfer unklar: alle unfertigen Nachrichten auffuellen
        t0 = time.time()
        if victim is None:
            res = self._hunt_free(buf, i, n)
            if res is None:
                return None
            self.stats["dropped"] += res - i
            self._remember("frei", buf, i, res, n)
            self.log("EINGERASTET (zwischen Nachrichten): %d Byte verworfen, Kopfbyte %02x" % (res - i, buf[i]))
            return res
        st = states[victim]
        H = 1 + (4 if st.ext else 0)
        C = min(self.cs_in, st.mlen - st.got)
        L = H + C
        if n < self._try_n + 512 and n - i < MAX_WAIT:
            return None                   # erst wieder versuchen, wenn genug neue Daten da sind
        self._try_n = n
        chosen = None
        for p in range(i, i + L + MARGIN + 1):    # p == i: ein ganzer Block fehlte
            if p == i:
                prefs = (C,)
            elif p == i + L:
                prefs = (C, 0)                # Kopfbyte kaputt, Nutzdaten heil -> verlustfrei korrigierbar
            else:
                prefs = (0, C)                # Stoerbytes eingeschoben (nichts verloren) oder Block gekuerzt/ersetzt
            res = [(c, self._look(buf, p, n, victim, c)) for c in prefs]
            ok = [c for c, r in res if r == "ok"]
            if ok:
                chosen = (p, ok[0])
                break
            if any(r == "need" for _, r in res):
                if n - i > MAX_WAIT:
                    raise GiveUp("Nachricht zu gross zum Einrasten")
                return None
            sh = [c for c, r in res if r in ("shallow", "inner")]
            if sh:
                chosen = (p, sh[0])
                break
        if chosen is None:
            return self._emergency(buf, i, n, [victim], t0)
        self._try_n = 0
        self._eh = 0
        p, c = chosen
        if c == 0:
            self.stats["dropped"] += p - i
            kind = "%d Stoerbyte vor dem naechsten Block verworfen (nichts fehlte)" % (p - i)
        else:
            head = bytes([0xC0 | victim]) + (st.ext_val if st.ext else b"")
            if p == i + L:
                payload = bytes(buf[i + H:i + H + C])
                kind = "Kopfbyte %02x -> %02x korrigiert, Nutzdaten heil" % (buf[i], 0xC0 | victim)
                self.stats["exact_fix"] += 1
            elif p < i + L:
                frag = bytes(buf[i + H:p]) if p > i + H else b""
                payload = frag + b"\x00" * (C - len(frag))
                kind = "Block war nur %d statt %d Byte lang, %d Byte mit Nullen aufgefuellt" % (p - i, L, L - (p - i))
                self.stats["filled"] += 1
            else:
                payload = bytes(buf[i + H:i + H + C])
                kind = "Block durch %d Byte lange Stoerung ersetzt, Rest verworfen" % (p - i)
                self.stats["dropped"] += p - (i + L)
            out += head + payload
            st.got += C
        self.stats["repairs"] += 1
        self._remember("reparatur", buf, i, p, n)
        self.log("REPARIERT (Nachricht typ %d, %d/%d Byte, csid %d): %s [%.1f ms]" %
                 (st.mtype, st.got, st.mlen, victim, kind, (time.time() - t0) * 1000))
        return p

    def _look(self, buf, p, n, victim, c):
        """Prueft die Annahme 'ab p geht es sauber weiter, der kaputte Block hat c Byte der Nachricht gebracht' ueber das Ende der
        Nachricht hinaus: 'ok' (Nachricht endet passend und danach folgen gueltige Bloecke), 'bad', 'need' (mehr Daten noetig)
        oder 'shallow' (Nachricht zu lang fuer die Pruefung, aber die ersten Bloecke passen)."""
        sim = {k: copy.copy(v) for k, v in self.states.items()}
        sim[victim].got += c
        done = sim[victim].got >= sim[victim].mlen
        pos, chunks, after = p, 0, 0
        cs_in = self.cs_in
        while True:
            h = read_header(buf, pos, n, sim)
            if h is NEED:
                return "need"
            if h is BAD:
                # Fehler VOR dem Ende der Nachricht (nach einigen sauberen Bloecken): ein zweiter Defekt im selben Frame - die Annahme
                # ist dadurch nicht widerlegt, der naechste Defekt wird als eigenes Ereignis behandelt.
                return "inner" if (not done and chunks >= INNER_MIN) else "bad"
            r = apply_chunk(buf, pos, n, sim, cs_in, h)
            if r is None:
                return "need"
            pos = r[0]
            chunks += 1
            if done:
                after += 1
                if after >= AFTER_CHUNKS:
                    return "ok"
            elif r[1] and h[1] == victim:
                done = True
            if chunks >= MAX_LOOK:
                return "shallow"

    def _boundary_ok(self, buf, p, n, states, csid):
        """p ist ein glaubwuerdiger Anfang einer Audio/Video-Nachricht auf csid, wenn dort ein AUSDRUECKLICHER Kopf (fmt 0/1/2)
        steht, die Nachricht in ihrem Raster vollstaendig und sauber durchparst und danach noch zwei gueltige Bloecke folgen.
        Zufallstreffer mitten in Nutzdaten schaffen das praktisch nie. Rueckgabe: 'ok' / 'bad' / 'need'."""
        b = buf[p]
        if (b >> 6) > 2 or (b & 63) != csid:
            return "bad"
        sim = {c: copy.copy(v) for c, v in states.items()}
        pos, chunks, after, done = p, 0, 0, False
        cs_in = self.cs_in
        while True:
            h = read_header(buf, pos, n, sim)
            if h is NEED:
                return "need"
            if h is BAD:
                return "bad"
            if chunks == 0 and h[3][1] not in (8, 9):
                return "bad"
            r = apply_chunk(buf, pos, n, sim, cs_in, h)
            if r is None:
                return "need"
            pos = r[0]
            chunks += 1
            if done:
                after += 1
                if after >= 2:
                    return "ok"
            elif r[1] and h[1] == csid:
                done = True

    def _find_boundary(self, buf, i, n, states, csid):
        """Sucht ab i+1 den naechsten sauberen Nachrichtenanfang. Rueckgabe: Position oder None (mehr Daten noetig).
        Gibt NIE auf: Findet sich lange nichts, werden die alten Bytes verworfen (nichts Unbestaetigtes geht an nginx)."""
        p = max(self._eh, i + 1)
        want = (0x00 | csid, 0x40 | csid, 0x80 | csid)
        while p < n:
            if buf[p] in want:
                r = self._boundary_ok(buf, p, n, states, csid)
                if r == "need":
                    self._eh = p
                    return None
                if r == "ok":
                    return p
            p += 1
        self._eh = n
        if n - i > MAX_HUNT:
            cut = n - i - KEEP_HUNT                   # alles vor den letzten KEEP_HUNT Byte wurde geprueft und ist Muell
            del buf[i:i + cut]
            self.stats["dropped"] += cut
            self.hunt_dropped += cut
            self._eh = max(0, self._eh - cut)
        return None

    def _emergency(self, buf, i, n, victims, t0):
        """Nichts passt: Rest aller unfertigen Nachrichten mit Nullen auffuellen (nginx bekommt sie vollstaendig) und beim naechsten
        sauberen Nachrichtenanfang weitermachen. Alles dazwischen wird verworfen."""
        sim = {c: copy.copy(v) for c, v in self.states.items()}
        for v in victims:
            sim[v].got = sim[v].mlen
        csid = self.av_csid if self.av_csid is not None else victims[0]
        h0 = self.hunt_dropped
        p = self._find_boundary(buf, i, n, sim, csid)
        if p is None:
            return None
        fill = bytearray()
        total = 0
        for v in victims:
            st = self.states[v]
            rest = st.mlen - st.got
            head = bytes([0xC0 | v]) + (st.ext_val if st.ext else b"")
            r = rest
            while r > 0:
                t = min(self.cs_in, r)
                fill += head + bytes(t)
                r -= t
            total += rest
            st.got = st.mlen
        self._out_extra += fill
        st = self.states[victims[0]]
        self.stats["emergency"] += 1
        self.stats["repairs"] += 1
        self.stats["dropped"] += p - i
        self._remember("notfall", buf, i, p, n)
        self.log("REPARIERT (Notfall, %d unfertige Nachricht(en), typ %d): Rest von %d Byte mit Nullen aufgefuellt, %d Byte Muell verworfen, "
                 "weiter bei naechster Nachricht [%.1f ms]" % (len(victims), st.mtype, total, (p - i) + (self.hunt_dropped - h0), (time.time() - t0) * 1000))
        self._try_n = 0
        self._eh = 0
        self.hunt_dropped = 0
        return p

    def _hunt_free(self, buf, i, n):
        """Kaputter Kopf zwischen zwei Nachrichten: beim naechsten sauberen Audio/Video-Anfang weitermachen."""
        if self.av_csid is None:
            raise GiveUp("kein Audio/Video-Chunk-Stream bekannt")
        p = self._find_boundary(buf, i, n, self.states, self.av_csid)
        if p is not None:
            self._eh = 0
        return p

    def _remember(self, what, buf, i, p, n):
        if len(self.dumps) < 8:
            self.dumps.append((what, bytes(buf[max(0, i - 160):min(n, p + 160)]), i - max(0, i - 160)))


# ===================================================================== Dienst
LOG_FILE = os.environ.get("GUARD_LOG", "/var/log/irl-rtmp-guard.log")
DUMP_DIR = os.environ.get("GUARD_DUMP_DIR", "/var/log/irl-rtmp-guard")
STATUS_FILE = os.environ.get("GUARD_STATUS", "/run/irl-rtmp-guard/status.json")
LISTEN = (os.environ.get("GUARD_LISTEN_HOST", "0.0.0.0"), int(os.environ.get("GUARD_LISTEN_PORT", "19350")))
UPSTREAM = (os.environ.get("GUARD_UP_HOST", "127.0.0.1"), int(os.environ.get("GUARD_UP_PORT", "1935")))

def _self_version():
    """Kennung dieses Programmstands (Hash der eigenen Datei) - das Einrichtungsskript erkennt damit, ob die laufende Instanz alt ist."""
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "?"


VERSION = _self_version()
logger = logging.getLogger("guard")
GLOBAL = {"conns": 0, "active": 0, "repairs": 0, "giveups": 0, "started": time.time(), "last_repair": None}


def _tune(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


async def _pipe(r, w):
    try:
        while True:
            d = await r.read(65536)
            if not d:
                break
            w.write(d)
            await w.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


def _dump(cid, rep):
    if not rep.dumps:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        files = sorted(os.listdir(DUMP_DIR))
        for f in files[:-40]:
            os.remove(os.path.join(DUMP_DIR, f))
        for k, (what, data, off) in enumerate(rep.dumps):
            fn = os.path.join(DUMP_DIR, "%s-%s-%d-%s.bin" % (time.strftime("%Y%m%d-%H%M%S"), cid, k, what))
            with open(fn, "wb") as f:
                f.write(data)
            logger.info("Rohbytes der Stelle gesichert: %s (Fehler bei Offset %d)", fn, off)
    except OSError as e:
        logger.warning("Dump nicht schreibbar: %s", e)


async def handle(cr, cw):
    peer = cw.get_extra_info("peername")
    cid = "%s_%d" % (peer[0], peer[1]) if peer else "?"
    GLOBAL["conns"] += 1
    GLOBAL["active"] += 1
    t0 = time.time()
    _tune(cw.get_extra_info("socket"))
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(*UPSTREAM), 5)
    except (OSError, asyncio.TimeoutError) as e:
        logger.error("[%s] nginx nicht erreichbar: %s", cid, e)
        cw.close()
        GLOBAL["active"] -= 1
        return
    _tune(uw.get_extra_info("socket"))
    rep = Repairer(log=lambda m: logger.warning("[%s] %s", cid, m), name=cid)
    logger.info("[%s] Verbindung geoeffnet", cid)
    down = asyncio.create_task(_pipe(ur, cw))
    try:
        while True:
            d = await cr.read(65536)
            if not d:
                break
            before = rep.stats["repairs"], rep.stats["giveups"]
            out = rep.feed(d)
            if rep.stats["repairs"] != before[0]:
                GLOBAL["repairs"] += rep.stats["repairs"] - before[0]
                GLOBAL["last_repair"] = time.strftime("%H:%M:%S")
            if rep.stats["giveups"] != before[1]:
                GLOBAL["giveups"] += 1
            if out:
                uw.write(out)
                await uw.drain()
            if down.done():
                break
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("[%s] unerwarteter Fehler im Strom - Verbindung wird unveraendert weitergereicht", cid)
    finally:
        s = rep.stats
        logger.info("[%s] Verbindung beendet nach %.0f s: %d MB rein, %d MB raus, %d Nachrichten, %d Reparaturen, %d Aufgaben",
                    cid, time.time() - t0, s["bytes_in"] >> 20, s["bytes_out"] >> 20, s["messages"], s["repairs"], s["giveups"])
        _dump(cid, rep)
        for w in (uw, cw):
            try:
                w.close()
            except Exception:
                pass
        down.cancel()
        GLOBAL["active"] -= 1


async def status_writer():
    while True:
        try:
            os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(GLOBAL, version=VERSION, uptime=int(time.time() - GLOBAL["started"])), f)
            os.replace(tmp, STATUS_FILE)
        except OSError:
            pass
        await asyncio.sleep(5)


async def main():
    server = await asyncio.start_server(handle, *LISTEN, backlog=64, reuse_address=True)
    logger.info("irl-rtmp-guard %s lauscht auf %s:%d -> nginx %s:%d", VERSION, LISTEN[0], LISTEN[1], UPSTREAM[0], UPSTREAM[1])
    asyncio.create_task(status_writer())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    server.close()


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5 << 20, backupCount=4)
    except OSError:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    logger.addHandler(h)
    if sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
