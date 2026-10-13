/* IRL Streamer OS - Bild-in-Bild-Baustein fuer GStreamer (NV12), eigene Umsetzung.
 *
 * Problem: Ein normaler GStreamer-"compositor" muss das grosse Hardware-Decoder-Bild mit der CPU LESEN.
 * Auf dem RK3588 ist das extrem langsam (gemessen auf der ROCK 5B+: ca. 3,7 Bilder/s statt 30).
 * Loesung: Das KLEINE Bild wird schon im Hardware-Decoder verkleinert und nur in einen Ringspeicher
 * kopiert; der Mischer SCHREIBT es in das grosse Bild und liest das grosse Bild nie.
 *
 * Elemente:
 *   irlpipsink   Eingang fuer ein kleines Bild (Eigenschaft slot = 0..2), legt Bild + Zeitstempel im Ringspeicher ab.
 *   irlpipmix    steht im Hauptbild-Pfad (NV12). Schreibt je Hauptbild das passende kleine Bild an die
 *                konfigurierte Stelle. Waehlt das neueste kleine Bild, dessen Zeitstempel nicht nach dem des
 *                Hauptbilds liegt (abzueglich Versatz) -> Gleichlauf auch bei unterschiedlicher Kameraverzoegerung.
 *                Die Anordnung steht in einer Steuerdatei (config-file), die alle 300 ms auf Aenderung geprueft
 *                wird: Position, Rahmen, Versatz und Ein-/Ausblenden gelten ohne Neustart der Sendekette.
 *
 * Steuerdatei, eine Zeile je kleines Bild (Zeilen mit # sind Kommentar):
 *   slot=0 enabled=1 x=1400 y=760 border=4 color=ffffff delay=0
 *   x,y   linke obere Ecke in Bildpunkten des Hauptbilds (werden auf gerade Werte gerundet)
 *   border Rahmenbreite in Bildpunkten (0 = keiner), color = RRGGBB, delay = Versatz in ms (>0: kleines Bild spaeter)
 *
 * Kleines Bild laenger als 2 s ohne neue Bilder -> wird nicht mehr eingeblendet (kein eingefrorenes Bild).
 * Lizenz: MIT.
 */
#ifdef HAVE_CONFIG_H
#include "config.h"
#endif
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <glib/gstdio.h>
#include <gst/gst.h>
#include <gst/base/gstbasesink.h>
#include <gst/video/video.h>
#include <gst/video/gstvideofilter.h>

GST_DEBUG_CATEGORY_STATIC(irlpip_debug);
#define GST_CAT_DEFAULT irlpip_debug

#define NSLOTS 3
#define RING_CAP 120                    /* 4 s bei 30 Bildern/s: reicht fuer einen Versatz bis 3 s */
#define STALE_US (2 * G_USEC_PER_SEC)
#define MARGIN_MIN_US 40000        /* Reserve gegen Ankunftsschwankungen: kleines Bild wird so viel spaeter als die fruehest moegliche Ankunft gezeigt */
#define MARGIN_MAX_US 300000       /* die Reserve passt sich je Kamera an die gemessene Schwankung an (zwischen diesen Grenzen) */
#define MARGIN_PAD_US 10000
#define LEAK_US 50                 /* Taktrueckgewinnung: Referenz darf je Bild so viel nach oben wandern (Uhren driften) */
#define CONF_POLL_US 300000

typedef struct { guint8 *data; GstClockTime pts; gint64 ref_us; } Frame;
typedef struct {
  Frame f[RING_CAP];
  gsize size;
  guint rd, count;
  gint w, h;
  guint8 *cur;
  GstClockTime cur_pts;
  gboolean have_cur;
  gint64 last_push;
  guint64 pushed, dropped, shown, repeated, held, skipped;
  gint64 prev_push;
  gdouble jit_us;                 /* groesste juengste Verspaetung gegenueber der fruehesten Ankunft (klingt langsam ab) */
  gint64 margin_us;
  gint64 off_us;                  /* Taktrueckgewinnung: Ankunftszeit minus Kamera-Zeitstempel (untere Huellkurve) */
  gboolean off_init;
  GstClockTime prev_pts;
  guint64 gaps[4];                /* Abstand zum vorigen Bild: <8 ms, 8-25 ms, 25-45 ms, >45 ms */
} Ring;

static GMutex ring_lock;
static Ring rings[NSLOTS];

static void ring_reset_locked(Ring *r) { r->rd = r->count = 0; r->have_cur = FALSE; r->jit_us = 0; }

static void ring_resize_locked(Ring *r, gint w, gint h) {
  gsize need = (gsize) w * h * 3 / 2;
  if (need != r->size) {
    for (guint i = 0; i < RING_CAP; i++) { g_free(r->f[i].data); r->f[i].data = g_malloc(need); r->f[i].pts = GST_CLOCK_TIME_NONE; }
    g_free(r->cur);
    r->cur = g_malloc(need);
    r->size = need;
  }
  r->w = w; r->h = h;
  ring_reset_locked(r);
}


/* Taktrueckgewinnung: Die Zeitstempel hinter flvdemux gehoeren zur Uhr der jeweiligen Kamera (jede beginnt bei ihrer eigenen
 * Null) und sind zwischen Kameras NICHT vergleichbar; die Ankunftszeit ist dagegen ruckelig (Schuebe). Daher: glatter
 * Kamera-Zeitstempel + Versatz zur eigenen Ankunftszeit, wobei der Versatz der kleinste beobachtete Wert ist (er steigt nur
 * langsam an, damit Uhrdrift mitlaeuft). Ergebnis: glatte Referenzzeit in der Uhr des Rechners. */
static gint64 est_ref(gint64 *off, gboolean *init, GstClockTime *prev, GstClockTime pts, gint64 now, gboolean *reset, gdouble *jit) {
  *reset = FALSE;
  if (!GST_CLOCK_TIME_IS_VALID(pts)) return now;
  gint64 p = (gint64) (pts / 1000), d = now - p;
  if (*init && GST_CLOCK_TIME_IS_VALID(*prev) && pts + 100 * GST_MSECOND < *prev) { *init = FALSE; *reset = TRUE; }   /* Zeitstempel zurueckgesprungen: Kamera neu verbunden */
  if (!*init) { *off = d; *init = TRUE; }
  else if (d < *off) *off = d;
  else *off += LEAK_US;
  *prev = pts;
  if (jit) { gdouble late = (gdouble) (d - *off); if (late < 0) late = 0; *jit = late > *jit ? late : *jit * 0.995; }
  return p + *off;
}

/* ------------------------------------------------------------------------------------------ irlpipsink */

typedef struct { GstBaseSink parent; GstVideoInfo info; gboolean have_info; guint slot; } IrlPipSink;
typedef struct { GstBaseSinkClass parent_class; } IrlPipSinkClass;
G_DEFINE_TYPE(IrlPipSink, irl_pip_sink, GST_TYPE_BASE_SINK)

enum { PROP_SINK_0, PROP_SINK_SLOT };

static GstStaticPadTemplate sink_tmpl = GST_STATIC_PAD_TEMPLATE(
    "sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS("video/x-raw, format=(string)NV12"));

static gboolean sink_set_caps(GstBaseSink *bs, GstCaps *caps) {
  IrlPipSink *self = (IrlPipSink *) bs;
  if (!gst_video_info_from_caps(&self->info, caps)) return FALSE;
  g_mutex_lock(&ring_lock);
  ring_resize_locked(&rings[self->slot], GST_VIDEO_INFO_WIDTH(&self->info), GST_VIDEO_INFO_HEIGHT(&self->info));
  g_mutex_unlock(&ring_lock);
  self->have_info = TRUE;
  GST_INFO_OBJECT(self, "kleines Bild %dx%d (Platz %u)", GST_VIDEO_INFO_WIDTH(&self->info), GST_VIDEO_INFO_HEIGHT(&self->info), self->slot);
  return TRUE;
}

static GstFlowReturn sink_render(GstBaseSink *bs, GstBuffer *buf) {
  IrlPipSink *self = (IrlPipSink *) bs;
  GstVideoFrame f;
  if (!self->have_info || !gst_video_frame_map(&f, &self->info, buf, GST_MAP_READ)) return GST_FLOW_OK;
  Ring *r = &rings[self->slot];
  const gint w = r->w, h = r->h;
  g_mutex_lock(&ring_lock);
  gint64 now = g_get_monotonic_time();
  gboolean rst;
  gint64 ref = est_ref(&r->off_us, &r->off_init, &r->prev_pts, GST_BUFFER_PTS(buf), now, &rst, &r->jit_us);
  r->margin_us = CLAMP((gint64) r->jit_us + MARGIN_PAD_US, MARGIN_MIN_US, MARGIN_MAX_US);
  if (rst) ring_reset_locked(r);
  if (r->count == RING_CAP) { r->rd = (r->rd + 1) % RING_CAP; r->count--; r->dropped++; }
  Frame *dst = &r->f[(r->rd + r->count) % RING_CAP];
  const guint8 *y = GST_VIDEO_FRAME_PLANE_DATA(&f, 0), *uv = GST_VIDEO_FRAME_PLANE_DATA(&f, 1);
  const gint sy = GST_VIDEO_FRAME_PLANE_STRIDE(&f, 0), suv = GST_VIDEO_FRAME_PLANE_STRIDE(&f, 1);
  for (gint i = 0; i < h; i++) memcpy(dst->data + (gsize) i * w, y + (gsize) i * sy, w);
  for (gint i = 0; i < h / 2; i++) memcpy(dst->data + (gsize) w * h + (gsize) i * w, uv + (gsize) i * suv, w);
  dst->pts = GST_BUFFER_PTS(buf);
  dst->ref_us = ref;
  r->count++; r->pushed++;
  r->last_push = now;
  if (r->prev_push) { gint64 g = r->last_push - r->prev_push; r->gaps[g < 8000 ? 0 : g < 25000 ? 1 : g < 45000 ? 2 : 3]++; }
  r->prev_push = r->last_push;
  g_mutex_unlock(&ring_lock);
  gst_video_frame_unmap(&f);
  return GST_FLOW_OK;
}

static void sink_set_property(GObject *o, guint id, const GValue *v, GParamSpec *p) {
  IrlPipSink *self = (IrlPipSink *) o;
  if (id == PROP_SINK_SLOT) self->slot = g_value_get_uint(v);
  else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void sink_get_property(GObject *o, guint id, GValue *v, GParamSpec *p) {
  IrlPipSink *self = (IrlPipSink *) o;
  if (id == PROP_SINK_SLOT) g_value_set_uint(v, self->slot);
  else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void irl_pip_sink_class_init(IrlPipSinkClass *k) {
  GObjectClass *go = G_OBJECT_CLASS(k);
  GstElementClass *el = GST_ELEMENT_CLASS(k);
  GstBaseSinkClass *bs = GST_BASE_SINK_CLASS(k);
  go->set_property = sink_set_property; go->get_property = sink_get_property;
  g_object_class_install_property(go, PROP_SINK_SLOT, g_param_spec_uint("slot", "slot", "Platz des kleinen Bilds (0..2)", 0, NSLOTS - 1, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  gst_element_class_add_static_pad_template(el, &sink_tmpl);
  gst_element_class_set_static_metadata(el, "IRL PiP Eingang", "Sink/Video", "Nimmt ein kleines NV12-Bild fuer irlpipmix entgegen", "IRL Streamer OS");
  bs->set_caps = sink_set_caps; bs->render = sink_render;
}
static void irl_pip_sink_init(IrlPipSink *self) { self->slot = 0; gst_base_sink_set_sync(GST_BASE_SINK(self), FALSE); gst_base_sink_set_async_enabled(GST_BASE_SINK(self), FALSE); }

/* ------------------------------------------------------------------------------------------ irlpipmix */

typedef struct { gboolean enabled; gint x, y, border; guint8 Y, U, V; gint delay_ms; } SlotCfg;
typedef struct {
  GstVideoFilter parent;
  gchar *config_file;
  SlotCfg cfg[NSLOTS];
  gint64 margin_min_us;           /* Mindest-Reserve (Steuerdatei: margin=NN in ms) */
  gint64 last_check;
  gint64 last_mtime_ns;
  guint64 frames, reloads;
  gint64 prev_main;
  guint64 main_gaps[4];
  gint64 off_us;
  gboolean off_init;
  GstClockTime prev_pts;
  gint64 last_stats;
  gboolean warned[NSLOTS];
} IrlPipMix;
typedef struct { GstVideoFilterClass parent_class; } IrlPipMixClass;
G_DEFINE_TYPE(IrlPipMix, irl_pip_mix, GST_TYPE_VIDEO_FILTER)

enum { PROP_MIX_0, PROP_MIX_CONFIG, PROP_MIX_STATS };

static GstStaticPadTemplate mix_sink_tmpl = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS("video/x-raw, format=(string)NV12"));
static GstStaticPadTemplate mix_src_tmpl = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS("video/x-raw, format=(string)NV12"));

static void rgb_to_yuv(guint32 rgb, guint8 *Y, guint8 *U, guint8 *V) {
  gint r = (rgb >> 16) & 255, g = (rgb >> 8) & 255, b = rgb & 255;
  gint y = (66 * r + 129 * g + 25 * b + 128) / 256 + 16;
  gint u = (-38 * r - 74 * g + 112 * b + 128) / 256 + 128;
  gint v = (112 * r - 94 * g - 18 * b + 128) / 256 + 128;
  *Y = CLAMP(y, 16, 235); *U = CLAMP(u, 16, 240); *V = CLAMP(v, 16, 240);
}

static void mix_parse_config(IrlPipMix *self) {
  gchar *txt = NULL;
  SlotCfg n[NSLOTS];
  gint64 gmargin = MARGIN_MIN_US;
  memset(n, 0, sizeof n);
  for (gint i = 0; i < NSLOTS; i++) { n[i].Y = 235; n[i].U = n[i].V = 128; }
  if (self->config_file && g_file_get_contents(self->config_file, &txt, NULL, NULL)) {
    gchar **lines = g_strsplit(txt, "\n", -1);
    for (gchar **l = lines; *l; l++) {
      gchar *s = g_strstrip(*l);
      if (!*s || *s == '#') continue;
      gchar **tok = g_strsplit_set(s, " \t", -1);
      gint slot = -1; SlotCfg c = {0};
      c.Y = 235; c.U = c.V = 128;
      for (gchar **t = tok; *t; t++) {
        gchar *eq = strchr(*t, '='); if (!eq) continue;
        *eq = 0; const gchar *k = *t, *v = eq + 1;
        if (!strcmp(k, "margin")) gmargin = CLAMP(atoi(v), 0, 1000) * 1000;
        else if (!strcmp(k, "slot")) slot = atoi(v);
        else if (!strcmp(k, "enabled")) c.enabled = atoi(v) != 0;
        else if (!strcmp(k, "x")) c.x = atoi(v);
        else if (!strcmp(k, "y")) c.y = atoi(v);
        else if (!strcmp(k, "border")) c.border = CLAMP(atoi(v), 0, 40);
        else if (!strcmp(k, "delay")) c.delay_ms = CLAMP(atoi(v), -3000, 3000);
        else if (!strcmp(k, "color")) rgb_to_yuv((guint32) strtoul(v, NULL, 16), &c.Y, &c.U, &c.V);
      }
      g_strfreev(tok);
      if (slot >= 0 && slot < NSLOTS) n[slot] = c;
    }
    g_strfreev(lines);
    g_free(txt);
    memcpy(self->cfg, n, sizeof n);
    self->margin_min_us = gmargin;
    self->reloads++;
  }
}

static void mix_maybe_reload(IrlPipMix *self) {
  gint64 now = g_get_monotonic_time();
  if (now - self->last_check < CONF_POLL_US) return;
  self->last_check = now;
  if (!self->config_file) return;
  struct stat st;
  if (stat(self->config_file, &st) != 0) return;
  gint64 m = (gint64) st.st_mtim.tv_sec * 1000000000LL + st.st_mtim.tv_nsec;
  if (m != self->last_mtime_ns) { self->last_mtime_ns = m; mix_parse_config(self); }
}

static void fill_rect(GstVideoFrame *f, gint x0, gint y0, gint x1, gint y1, guint8 Y, guint8 U, guint8 V) {
  const gint W = GST_VIDEO_FRAME_WIDTH(f), H = GST_VIDEO_FRAME_HEIGHT(f);
  x0 = MAX(0, x0) & ~1; y0 = MAX(0, y0) & ~1; x1 = MIN(W, x1) & ~1; y1 = MIN(H, y1) & ~1;
  if (x1 <= x0 || y1 <= y0) return;
  guint8 *py = GST_VIDEO_FRAME_PLANE_DATA(f, 0), *puv = GST_VIDEO_FRAME_PLANE_DATA(f, 1);
  const gint sy = GST_VIDEO_FRAME_PLANE_STRIDE(f, 0), suv = GST_VIDEO_FRAME_PLANE_STRIDE(f, 1);
  for (gint r = y0; r < y1; r++) memset(py + (gsize) r * sy + x0, Y, x1 - x0);
  for (gint r = y0 / 2; r < y1 / 2; r++) { guint8 *d = puv + (gsize) r * suv + x0; for (gint c = 0; c < x1 - x0; c += 2) { d[c] = U; d[c + 1] = V; } }
}


static void mix_write_stats(IrlPipMix *self) {
  gint64 now = g_get_monotonic_time();
  if (!self->config_file || now - self->last_stats < G_USEC_PER_SEC) return;
  self->last_stats = now;
  GString *g = g_string_new(NULL);
  g_mutex_lock(&ring_lock);
  g_string_append_printf(g, "{\"frames\":%" G_GUINT64_FORMAT ",\"reloads\":%" G_GUINT64_FORMAT ",\"main_pts_ms\":%" G_GINT64_FORMAT ",\"main_gaps\":[%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT "],\"slots\":[", self->frames, self->reloads, GST_CLOCK_TIME_IS_VALID(self->prev_pts) ? (gint64) (self->prev_pts / GST_MSECOND) : (gint64) -1, self->main_gaps[0], self->main_gaps[1], self->main_gaps[2], self->main_gaps[3]);
  for (gint s = 0; s < NSLOTS; s++) {
    Ring *r = &rings[s];
    g_string_append_printf(g, "%s{\"w\":%d,\"h\":%d,\"pushed\":%" G_GUINT64_FORMAT ",\"shown\":%" G_GUINT64_FORMAT ",\"repeated\":%" G_GUINT64_FORMAT ",\"dropped\":%" G_GUINT64_FORMAT ",\"held\":%" G_GUINT64_FORMAT ",\"skipped\":%" G_GUINT64_FORMAT ",\"gaps\":[%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT "],\"age_ms\":%d,\"pts_ms\":%" G_GINT64_FORMAT ",\"margin_ms\":%d}",
                           s ? "," : "", r->w, r->h, r->pushed, r->shown, r->repeated, r->dropped, r->held, r->skipped, r->gaps[0], r->gaps[1], r->gaps[2], r->gaps[3], r->last_push ? (gint) ((now - r->last_push) / 1000) : -1, GST_CLOCK_TIME_IS_VALID(r->prev_pts) ? (gint64) (r->prev_pts / GST_MSECOND) : (gint64) -1, (gint) (r->margin_us / 1000));
  }
  g_mutex_unlock(&ring_lock);
  g_string_append(g, "]}\n");
  gchar *path = g_strconcat(self->config_file, ".stats", NULL), *tmp = g_strconcat(path, ".tmp", NULL);
  if (g_file_set_contents(tmp, g->str, -1, NULL)) g_rename(tmp, path);
  g_free(path); g_free(tmp);
  g_string_free(g, TRUE);
}

static GstFlowReturn mix_transform_ip(GstVideoFilter *vf, GstVideoFrame *frame) {
  IrlPipMix *self = (IrlPipMix *) vf;
  GstBuffer *buf = frame->buffer;
  GstClockTime pts = GST_BUFFER_PTS(buf);
  const gint W = GST_VIDEO_FRAME_WIDTH(frame), H = GST_VIDEO_FRAME_HEIGHT(frame);
  guint8 *py = GST_VIDEO_FRAME_PLANE_DATA(frame, 0), *puv = GST_VIDEO_FRAME_PLANE_DATA(frame, 1);
  const gint sy = GST_VIDEO_FRAME_PLANE_STRIDE(frame, 0), suv = GST_VIDEO_FRAME_PLANE_STRIDE(frame, 1);
  mix_maybe_reload(self);
  mix_write_stats(self);
  self->frames++;
  gint64 tnow = g_get_monotonic_time();
  gboolean mrst;
  gint64 main_ref = est_ref(&self->off_us, &self->off_init, &self->prev_pts, pts, tnow, &mrst, NULL);
  { gint64 t = tnow; if (self->prev_main) { gint64 g = t - self->prev_main; self->main_gaps[g < 8000 ? 0 : g < 25000 ? 1 : g < 45000 ? 2 : 3]++; } self->prev_main = t; }
  gint64 now = g_get_monotonic_time();
  for (gint s = 0; s < NSLOTS; s++) {
    SlotCfg *c = &self->cfg[s];
    if (!c->enabled) continue;
    g_mutex_lock(&ring_lock);
    Ring *r = &rings[s];
    if (r->w <= 0 || now - r->last_push > STALE_US) { g_mutex_unlock(&ring_lock); continue; }
    /* Alle kleinen Bilder, deren Referenzzeit nicht nach dem Ziel liegt, gelten als faellig; das neueste davon wird gezeigt.
     * Die Referenzzeiten sind glatt, so ergibt sich im Normalfall genau ein neues kleines Bild je Hauptbild. */
    gint64 target = main_ref - (gint64) c->delay_ms * 1000 - MAX(r->margin_us, self->margin_min_us ? self->margin_min_us : MARGIN_MIN_US);
    gboolean fresh = FALSE;
    guint popped = 0;
    while (r->count > 0 && r->f[r->rd].ref_us <= target) {
      Frame *fr = &r->f[r->rd];
      memcpy(r->cur, fr->data, r->size);
      r->cur_pts = fr->pts; r->have_cur = TRUE; fresh = TRUE;
      r->rd = (r->rd + 1) % RING_CAP; r->count--; popped++;
    }
    if (popped > 1) r->skipped += popped - 1;
    if (!r->have_cur) { g_mutex_unlock(&ring_lock); continue; }
    if (fresh) r->shown++; else r->repeated++;
    const gint w = r->w, h = r->h;
    const gint x = c->x & ~1, y = c->y & ~1;
    if (x < 0 || y < 0 || x + w > W || y + h > H) {
      if (!self->warned[s]) { GST_WARNING_OBJECT(self, "kleines Bild %d passt nicht ins Hauptbild (x=%d y=%d %dx%d in %dx%d)", s, x, y, w, h, W, H); self->warned[s] = TRUE; }
      g_mutex_unlock(&ring_lock); continue;
    }
    for (gint i = 0; i < h; i++) memcpy(py + (gsize) (y + i) * sy + x, r->cur + (gsize) i * w, w);
    for (gint i = 0; i < h / 2; i++) memcpy(puv + (gsize) (y / 2 + i) * suv + x, r->cur + (gsize) w * h + (gsize) i * w, w);
    g_mutex_unlock(&ring_lock);
    if (c->border > 0) {
      gint b = (c->border + 1) & ~1;
      fill_rect(frame, x - b, y - b, x + w + b, y, c->Y, c->U, c->V);
      fill_rect(frame, x - b, y + h, x + w + b, y + h + b, c->Y, c->U, c->V);
      fill_rect(frame, x - b, y, x, y + h, c->Y, c->U, c->V);
      fill_rect(frame, x + w, y, x + w + b, y + h, c->Y, c->U, c->V);
    }
  }
  return GST_FLOW_OK;
}

static void mix_set_property(GObject *o, guint id, const GValue *v, GParamSpec *p) {
  IrlPipMix *self = (IrlPipMix *) o;
  if (id == PROP_MIX_CONFIG) { g_free(self->config_file); self->config_file = g_value_dup_string(v); self->last_mtime_ns = 0; self->last_check = 0; mix_parse_config(self); }
  else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void mix_get_property(GObject *o, guint id, GValue *v, GParamSpec *p) {
  IrlPipMix *self = (IrlPipMix *) o;
  if (id == PROP_MIX_CONFIG) g_value_set_string(v, self->config_file);
  else if (id == PROP_MIX_STATS) {
    GString *g = g_string_new(NULL);
    g_mutex_lock(&ring_lock);
    g_string_append_printf(g, "frames=%" G_GUINT64_FORMAT " reloads=%" G_GUINT64_FORMAT, self->frames, self->reloads);
    for (gint s = 0; s < NSLOTS; s++) {
      Ring *r = &rings[s];
      g_string_append_printf(g, " | slot%d %dx%d pushed=%" G_GUINT64_FORMAT " shown=%" G_GUINT64_FORMAT " repeated=%" G_GUINT64_FORMAT " dropped=%" G_GUINT64_FORMAT,
                             s, r->w, r->h, r->pushed, r->shown, r->repeated, r->dropped);
    }
    g_mutex_unlock(&ring_lock);
    g_value_take_string(v, g_string_free(g, FALSE));
  } else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void mix_finalize(GObject *o) { g_free(((IrlPipMix *) o)->config_file); G_OBJECT_CLASS(irl_pip_mix_parent_class)->finalize(o); }

static void irl_pip_mix_class_init(IrlPipMixClass *k) {
  GObjectClass *go = G_OBJECT_CLASS(k);
  GstElementClass *el = GST_ELEMENT_CLASS(k);
  GstVideoFilterClass *vf = GST_VIDEO_FILTER_CLASS(k);
  go->set_property = mix_set_property; go->get_property = mix_get_property; go->finalize = mix_finalize;
  g_object_class_install_property(go, PROP_MIX_CONFIG, g_param_spec_string("config-file", "config-file", "Steuerdatei mit der Anordnung (wird live nachgeladen)", NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_MIX_STATS, g_param_spec_string("stats", "stats", "Zaehler (nur lesen)", NULL, G_PARAM_READABLE | G_PARAM_STATIC_STRINGS));
  gst_element_class_add_static_pad_template(el, &mix_sink_tmpl);
  gst_element_class_add_static_pad_template(el, &mix_src_tmpl);
  gst_element_class_set_static_metadata(el, "IRL PiP Mischer", "Filter/Effect/Video", "Schreibt kleine Bilder in das Hauptbild (NV12, ohne das grosse Bild zu lesen)", "IRL Streamer OS");
  vf->transform_frame_ip = mix_transform_ip;
}
static void irl_pip_mix_init(IrlPipMix *self) {
  self->margin_min_us = MARGIN_MIN_US;
  for (gint i = 0; i < NSLOTS; i++) { self->cfg[i].Y = 235; self->cfg[i].U = self->cfg[i].V = 128; }
}

static gboolean plugin_init(GstPlugin *plugin) {
  GST_DEBUG_CATEGORY_INIT(irlpip_debug, "irlpip", 0, "IRL PiP");
  return gst_element_register(plugin, "irlpipsink", GST_RANK_NONE, irl_pip_sink_get_type()) &&
         gst_element_register(plugin, "irlpipmix", GST_RANK_NONE, irl_pip_mix_get_type());
}

#ifndef PACKAGE
#define PACKAGE "irlpip"
#endif
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, irlpip, "IRL Streamer OS Bild-in-Bild", plugin_init, "0.1.0", "MIT/X11", "irl-pip", "https://irlstreameros.de")
