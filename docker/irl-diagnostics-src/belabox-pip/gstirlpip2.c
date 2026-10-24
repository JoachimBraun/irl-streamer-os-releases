/* IRL Streamer OS - Bild-in-Bild 2.0 fuer GStreamer (NV12 + S16LE), eigene Umsetzung. Lizenz: MIT.
 *
 * Idee (siehe /opt/data/pip2/SPEC.md): Jede Kamera (Platz/slot 0..3) liefert Bild und Ton in eigene Ringe; ein eigener,
 * fest getakteter Ausgang (irlpip2vsrc, exakt 30 Bilder/s auf der Pipeline-Uhr) holt je Takt fuer jeden Platz das zeitlich
 * faellige Bild, schneidet zu, skaliert und kopiert es an seinen Platz. irlpip2asrc liefert den Ton des gewaehlten Platzes
 * lueckenlos (Stille bei Luecken, 25-ms-Rampen). Bild und Ton einer Kamera haben DIESELBE Zeitzuordnung:
 *     ref(pts) = pts + off_slot      (Uhrzeit der GStreamer-Systemuhr, ns)
 * off_slot = untere Huellkurve von (Ankunftszeit - pts) der Bilder, danach hoechstens 1 ms/s nachgefuehrt (Uhrdrift).
 * Faellig zum Takt t: groesstes ref <= t - latency - delay_slot (+ halbes Ausgabebild, damit Bild und Ton mittig liegen).
 *
 * Elemente:
 *   irlpip2vsink  slot=N   NV12 beliebiger Groesse (Strides/Meta beachtet), kopiert jedes Bild einmal in den Ring.
 *   irlpip2asink  slot=N   S16LE 48 kHz Stereo, legt Samples nach Kamera-Zeitachse ab (Ring 6 s).
 *   irlpip2vsrc   config-file=..., width/height  live, NV12, zaehlerbasiert getaktet, schreibt <config-file>.stats.
 *   irlpip2asrc   config-file=...                live, S16LE 48 kHz Stereo, 1024 Samples je Puffer.
 * EOS an den Eingaengen wird verworfen (Platz gilt als weg, Ausgabe laeuft weiter); die Ausgaenge senden nie EOS.
 * Ideen fuer Ton-Luecken/Rampen/EOS-Verwerfen aus IRL4YOU (MIT, github.com/IRL4YOU/irl4you-pip, Akinos01/IRL4YOU),
 * Drift-Nachfuehrung angelehnt an Moblin DriftTracker.
 */
#ifdef HAVE_CONFIG_H
#include "config.h"
#endif
#ifndef _GNU_SOURCE
#define _GNU_SOURCE                        /* fallocate/FALLOC_FL_PUNCH_HOLE, MADV_REMOVE (glibc) */
#endif
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <time.h>
#include <pthread.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <glib/gstdio.h>
#include <gst/gst.h>
#include <gst/base/gstbasesink.h>
#include <gst/base/gstpushsrc.h>
#include <gst/video/video.h>
#include <gst/audio/audio.h>

GST_DEBUG_CATEGORY_STATIC(irlpip2_debug);
#define GST_CAT_DEFAULT irlpip2_debug

#define NSLOTS 4
#define NLAYERS 4
#define FMAX 512                           /* harte Obergrenze Bildfaecher je Platz (Schutz bei kaputten Zeitstempeln) */
#define PIN_WORDS (FMAX / 64)
#define POOL_KEEP 6                        /* so viele freie, belegte Bildfaecher je Platz bleiben im Speicher */
#define DEF_FRAMES 203                     /* Bildfaecher je Platz: (3000 latency + 3000 delay + 500) ms * 30/s + 8 */
#define DEF_FBYTES ((gsize) 1920 * 1088 * 3 / 2)  /* Mindestgroesse je Bildfach (1080p NV12 inkl. 1088 Zeilen) */
#define MAX_READERS 4                      /* Ausgabe-Prozesse je Platz (normal 1: belacoder) */
#define A_RATE 48000
#define A_CH 2
#define A_RING (6 * A_RATE)                /* Tonring: 6 s */
#define A_SPB 1024                         /* Samples je Ausgabepuffer */
#define RAMP 1200                          /* 25 ms Aus-/Einblenden */
#define A_TOL 480                          /* 10 ms: kleinere Zeitstempel-Abweichungen gelten als lueckenlos */
#define A_JUMP_BACK 4800                   /* 100 ms rueckwaerts im Ton -> Tonring neu */
#define A_RESYNC 960                       /* 20 ms Abweichung Lesezeiger <-> Soll -> Sprung mit Rampen */
#define A_CORR_MAX 3.0                     /* max. Samples Korrektur je Puffer (~3000 ppm) beim weichen Nachziehen */
#define LEARN_NS (15 * GST_SECOND)         /* Lernphase nach (Neu-)Start: neue Minima gelten sofort */
#define NBKT 120                           /* Eimer-Minima ueber 120 s */
#define DRIFT_MAX 400e-6                   /* plausible Uhrdrift; groessere Steigungen = Netzaenderung, nicht Drift */
#define DRIFT_TOL_NS (2 * GST_MSECOND)     /* Stuetzpunkte muessen so genau auf der Drift-Geraden liegen */
#define DRIFT_MIN_SPAN_NS (6 * GST_SECOND)
#define SLEW_PPM 1000                      /* max. 1 ms/s Korrektur nach der Lernphase */
#define JUMP_BACK_NS (100 * GST_MSECOND)
#define JUMP_FWD_NS (3 * GST_SECOND)
#define NODATA_NS (3 * GST_SECOND)
#define STALE_NS (2 * GST_SECOND)          /* Ebene ohne neues Bild: so lange letztes Bild, dann schwarz/weg */
#define STICK_NS (2 * GST_MSECOND)         /* Toleranz der Bildwahl gegen ms-gerundete FLV-Zeitstempel */
#define LATE_RUN_MAX 15                    /* so viele Takte darf die Wahl "knapp ein Bild zurueck" bleiben */
#define MAX_THREADS 8
#define CONF_POLL_NS (200 * GST_MSECOND)
#define DEF_LATENCY_MS 1500
/* nur shm-Betrieb (Kameras in eigenen Zubringer-Prozessen) */
#define SHM_MAGIC 0x314d53325049524cULL   /* "LRIP2SM1" */
#define SHM_VERSION 1
#define SHM_DIR "/dev/shm/"
#define WRITER_TAKEOVER_NS (GST_SECOND)    /* Schreiber gilt als weg: so lange kein Bild/Ton (oder Prozess tot) */
#define READER_DEAD_NS (30 * GST_SECOND)   /* Leser-Eintrag ohne Lebenszeichen (Fallback, falls PID-Pruefung nicht greift) */
#define STORE_POLL_NS (100 * GST_MSECOND)  /* Leser: so oft pruefen, ob der Zubringer eine neue Datei angelegt hat */
#define WRITER_CHECK_NS (GST_SECOND)       /* Schreiber: so oft pruefen, ob die Datei noch die eigene ist */
#define LOCK_TIMEOUT_MS 20                 /* nie laenger auf die Sperre eines Platzes warten (sterbender/gestoppter Prozess); normal wird sie nur Mikrosekunden gehalten - Box-Test 24.10.: 200 ms verzoegerten den Ausgabetakt bis 119 ms */
#define BAD_RETRY_NS (5 * GST_SECOND)      /* nach "kein Platz in /dev/shm" erneut versuchen */

/* ======================================================================================== gemeinsamer Zustand
 *
 * Je Platz ein "Speicher" (Store) mit fester Struktur, ohne Zeiger:
 *   [Kopf Slot: Sperre, Zeitzuordnung, Bildring (Fach-Indizes), Statistik] [Fachkoepfe FHdr x n] [Tonring] [Bildfaecher x n]
 * - ohne shm (Standard, wie bisher): anonymer Speicher im Prozess; Kameras und Ausgaenge laufen in EINER Pipeline.
 * - mit shm=<name>: Datei <name>-slotN (ohne '/' unter /dev/shm). Die Senken (Zubringer-Prozess je Kamera) legen sie an,
 *   die Quellen (Ausgabe = belacoder) blenden sie ein. Stirbt ein Zubringer, laeuft die Ausgabe unveraendert weiter.
 * Seiten werden erst bei Benutzung belegt (Datei/Mapping duenn besetzt) und freie Faecher ueber POOL_KEEP hinaus wieder
 * freigegeben -> Speicherbedarf = tatsaechlich benoetigte Bilder (latency + max delay + 500 ms), nicht n Faecher.
 *
 * Sicherheit ueber Prozessgrenzen:
 * - Sperre: pthread-Mutex PROCESS_SHARED + ROBUST. Stirbt ein Prozess mit der Sperre (kill -9), bekommt der naechste
 *   EOWNERDEAD, macht sie wieder konsistent und setzt den Platz zurueck (Ring leer, Zuordnung neu) - nie ein Haenger.
 *   Zusaetzlich wartet niemand laenger als 20 ms (sterbender/gestoppter Prozess): dann wird der Takt fuer diesen Platz uebersprungen.
 * - Ein Bild wird AUSSERHALB der Sperre in ein freies Fach kopiert und erst danach unter der Sperre in den Ring gehaengt.
 *   Der Leser sieht nur Faecher im Ring -> nie ein halb geschriebenes Bild. Ein Fach, das ein Leser gerade zeigt, ist
 *   "gepinnt" (Bitmaske je Leser-Prozess) und wird vom Schreiber nie wiederverwendet.
 * - Schreiber-Kennung (Zufallszahl je Prozess) + Generationszaehler: ein neu gestarteter Zubringer uebernimmt den Platz
 *   nur, wenn der alte tot ist bzw. > 1 s nichts mehr geliefert hat, und setzt dabei den Platz zurueck (Neulernen).
 *   Ein bei der Uebernahme noch laufender Kopiervorgang des alten Schreibers wird verworfen (Generation passt nicht).
 * - Zeitbasis: alle Zeiten = CLOCK_MONOTONIC in ns (= GstSystemClock, Standard-Typ MONOTONIC) -> in allen Prozessen
 *   direkt vergleichbar. Weicht die Systemuhr eines Prozesses ab, wird der Versatz beim Laden gemessen und beim
 *   Ausgabetakt herausgerechnet (g_sys2mono).
 */

typedef struct {                           /* Sicht auf ein Kamerabild (Fach im Speicher; im Benchmark eigener Puffer) */
  gint w, h;
  gint uvs;                                /* Zeilenabstand der UV-Ebene (= Breite aufgerundet auf gerade) */
  gsize size;
  guint8 *data;                            /* Y (w*h), danach UV (uvs * (h+1)/2), dicht gepackt */
  GstClockTime pts;
} Frame;

static G_GNUC_UNUSED void frame_free(Frame *f) { if (f) { g_free(f->data); g_free(f); } }

typedef struct { gint32 w, h, uvs, pad; guint64 size, pts, arr; } FHdr;          /* Kopf je Bildfach */
typedef struct { guint64 token; gint32 pid, pad; guint64 beat; guint64 pins[PIN_WORDS]; } ReaderEnt;

typedef struct {
  guint64 magic;
  guint32 version, hdr_bytes;
  guint64 total, fh_off, av_off, ad_off, fd_off, fbytes;
  gint32 nframes, slot_no;
  pthread_mutex_t mtx;
  gint32 dead;                             /* durch eine neuere Datei/einen neueren Speicher ersetzt */
  gint32 writing;                          /* Fach, das der Schreiber gerade fuellt (-1) */
  guint64 writer_token, writer_beat;
  gint32 writer_pid;
  guint32 writer_gen;
  guint64 cap_ns;                          /* Ring-Zeitfenster (vom Leser: latency + max delay + 500 ms) */
  guint64 bad_retry;
  ReaderEnt rdr[MAX_READERS];
  guint8 inring[FMAX], bad[FMAX];
  guint32 touched[FMAX];                   /* belegte Bytes je Fach (Seiten vorhanden), 0 = frei/zurueckgegeben */
  /* Zeitzuordnung (Bild + Ton gemeinsam) */
  gboolean map_valid, map_video;           /* map_video = FALSE: vorlaeufig aus Ton-Ankuenften */
  gint64 off;                              /* ns: ref = pts + off */
  GstClockTime learn_until, last_slew, bstart, last_arr, v_last_arr, a_last_arr;
  /* Minima je 1-s-Eimer (Zeitpunkt + Wert von Ankunft-pts), Ring ueber 120 s, fuer Drift und untere Huellkurve */
  gint64 bk_t[NBKT], bk_m[NBKT];
  gint bk_n, bk_i;
  gint64 cur_t, cur_m;                     /* laufender Eimer */
  gdouble drift;                           /* geschaetzte Steigung von off (ns/ns), |drift| <= DRIFT_MAX */
  gint64 tgt;                              /* Ziel fuer off: niedrigste drift-bereinigte Ankunft */
  guint epoch;
  GstClockTime v_last_pts, a_last_pts;
  gboolean v_have_pts, a_have_pts;
  gint64 pin_ns;                           /* gemessener Bildabstand der Kamera */
  gint64 bias;                             /* immer 0 (Phasen-Zentrierung entfernt, s. notes) */
  /* Bildring (Fach-Indizes, nach pts aufsteigend) */
  gint16 ring[FMAX];
  guint rd, cnt;
  gint w, h;
  /* Tonring, indiziert ueber den absoluten Kamera-Sample-Index (pts * 48000) */
  gint64 a_end;                            /* Index hinter dem neuesten Sample */
  gboolean a_have;
  /* Statistik */
  guint64 in, shown, repeated, skipped, late, resets, a_in, dup, nospace, foreign;
  gdouble a_underrun_ms;
  gint64 last_target;
  gboolean have_target, eos_v, eos_a;
  guint64 copy_us_sum, copy_n, copy_us_max;
  gint64 lat_sum;
  guint64 lat_n;
} Slot;

typedef struct {                           /* prozess-lokale Sicht auf einen Speicher */
  gint refs;                               /* atomar */
  gint slot;
  gboolean shared, broken;
  gint fd;
  dev_t dev;
  ino_t ino;
  guint8 *base;
  gsize len;
  Slot *s;
  FHdr *fh;
  guint8 *a_valid;
  gint16 *a_data;
  guint8 *fdata;
  guint16 lref[FMAX];                      /* Pins dieses Prozesses je Fach (unter der Sperre) */
  gint my_rd;                              /* Leser-Eintrag dieses Prozesses */
  GstClockTime last_reap;
} Store;

#define FDATA(st, i) ((st)->fdata + (gsize) (i) * (st)->s->fbytes)

#define NTAB 16
typedef struct { gchar *path; gint slot; gboolean used; Store *st; GstClockTime last_check; } TabEnt;

static GMutex g_lock;                      /* prozess-lokal: Speicher-Tabelle, Zaehler */
static TabEnt g_tab[NTAB];
static gint g_instances;
static gint g_audio_slot = -1;
static gdouble g_silence_ms;
static GstClock *g_sysclock;
static guint64 g_token;                    /* Kennung dieses Prozesses (Schreiber/Leser) */
static gint64 g_sys2mono;                  /* CLOCK_MONOTONIC - GstSystemClock (normal 0) */
static gsize g_page = 4096;
/* Nur Pruefstand: Absturz an definierter Stelle (Umgebung IRLPIP2_TEST_DIE="lock:N" | "copy:N" | "rlock:N"):
 * SIGKILL beim N-ten Bild mit gehaltener Sperre / mitten im Kopieren bzw. beim N-ten Ausgabetakt mit gehaltener Sperre. */
static gint g_die_lock, g_die_copy, g_die_rlock;
static void test_die_init(void) {
  const gchar *e = g_getenv("IRLPIP2_TEST_DIE");
  if (!e) return;
  if (g_str_has_prefix(e, "lock:")) g_die_lock = atoi(e + 5);
  else if (g_str_has_prefix(e, "copy:")) g_die_copy = atoi(e + 5);
  else if (g_str_has_prefix(e, "rlock:")) g_die_rlock = atoi(e + 6);
}
static void test_die(gint *ctr) { if (*ctr > 0 && --*ctr == 0) { fprintf(stderr, "irlpip2: TEST-ABSTURZ\n"); raise(SIGKILL); } }

static inline GstClockTime mono_now(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (GstClockTime) ts.tv_sec * GST_SECOND + (GstClockTime) ts.tv_nsec;
}
/* "Systemuhr" aller Zeitstempel im Speicher = CLOCK_MONOTONIC (prozessuebergreifend gleich) */
static inline GstClockTime sys_now(void) { return mono_now(); }

static inline gsize align_up(gsize v, gsize a) { return (v + a - 1) / a * a; }

static void store_layout(gint nframes, gsize fbytes, guint64 *fh_off, guint64 *av_off, guint64 *ad_off, guint64 *fd_off, guint64 *total) {
  *fh_off = align_up(sizeof(Slot), g_page);
  *av_off = align_up(*fh_off + sizeof(FHdr) * (gsize) nframes, g_page);
  *ad_off = align_up(*av_off + A_RING, g_page);
  *fd_off = align_up(*ad_off + (gsize) A_RING * A_CH * 2, g_page);
  *total = *fd_off + (guint64) fbytes * (guint64) nframes;
}

static void store_bind(Store *st) {
  st->s = (Slot *) st->base;
  st->fh = (FHdr *) (st->base + st->s->fh_off);
  st->a_valid = st->base + st->s->av_off;
  st->a_data = (gint16 *) (st->base + st->s->ad_off);
  st->fdata = st->base + st->s->fd_off;
}

static void store_free(Store *st) {
  if (st->base) munmap(st->base, st->len);
  if (st->fd >= 0) close(st->fd);
  g_free(st);
}
static Store *store_ref(Store *st) { if (st) g_atomic_int_inc(&st->refs); return st; }
static void store_unref(Store *st) { if (st && g_atomic_int_dec_and_test(&st->refs)) store_free(st); }

static void slot_hdr_init(Slot *s, gint slot, gint nframes, gsize fbytes, gboolean shared) {
  /* Speicher ist frisch (nullgefuellt) */
  pthread_mutexattr_t a;
  pthread_mutexattr_init(&a);
  if (shared) pthread_mutexattr_setpshared(&a, PTHREAD_PROCESS_SHARED);
  pthread_mutexattr_setrobust(&a, PTHREAD_MUTEX_ROBUST);
  pthread_mutex_init(&s->mtx, &a);
  pthread_mutexattr_destroy(&a);
  s->version = SHM_VERSION; s->hdr_bytes = sizeof(Slot);
  s->nframes = nframes; s->slot_no = slot; s->fbytes = fbytes;
  store_layout(nframes, fbytes, &s->fh_off, &s->av_off, &s->ad_off, &s->fd_off, &s->total);
  s->writing = -1;
  s->writer_token = g_token; s->writer_pid = getpid(); s->writer_gen = 1; s->writer_beat = mono_now();
  __atomic_store_n(&s->magic, SHM_MAGIC, __ATOMIC_RELEASE);
}

/* Neuen Speicher anlegen. path = NULL: anonym im Prozess. Sonst Datei: erst unter tmp-Namen vollstaendig anlegen, dann
 * per rename atomar sichtbar machen (ein Leser sieht nie eine halb eingerichtete Datei). */
static Store *store_create(const gchar *path, gint slot, gint nframes, gsize fbytes) {
  fbytes = align_up(fbytes, g_page);
  nframes = CLAMP(nframes, 8, FMAX);
  guint64 fh_off, av_off, ad_off, fd_off, total;
  store_layout(nframes, fbytes, &fh_off, &av_off, &ad_off, &fd_off, &total);
  Store *st = g_new0(Store, 1);
  st->refs = 1; st->slot = slot; st->fd = -1; st->my_rd = -1; st->shared = path != NULL; st->len = total;
  if (!path) {
    st->base = mmap(NULL, total, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (st->base == MAP_FAILED) { GST_ERROR("Platz %d: kein Speicher (%" G_GUINT64_FORMAT " Bytes)", slot, total); g_free(st); return NULL; }
    slot_hdr_init((Slot *) st->base, slot, nframes, fbytes, FALSE);
    store_bind(st);
    return st;
  }
  gchar *tmp = g_strdup_printf("%s.tmp.%d", path, (gint) getpid());
  unlink(tmp);
  gint fd = open(tmp, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0660);
  if (fd < 0) { GST_ERROR("Platz %d: %s nicht anlegbar: %s", slot, tmp, g_strerror(errno)); g_free(tmp); g_free(st); return NULL; }
  gint e = 0;
  if (ftruncate(fd, (off_t) total) != 0 || (e = posix_fallocate(fd, 0, (off_t) fd_off)) != 0) {   /* Kopf+Ton sicher belegt: nie SIGBUS */
    GST_ERROR("Platz %d: %s: kein Platz (%s)", slot, tmp, g_strerror(e ? e : errno));
    close(fd); unlink(tmp); g_free(tmp); g_free(st); return NULL;
  }
  st->base = mmap(NULL, total, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (st->base == MAP_FAILED) { GST_ERROR("Platz %d: mmap %s: %s", slot, tmp, g_strerror(errno)); close(fd); unlink(tmp); g_free(tmp); g_free(st); return NULL; }
  st->fd = fd;
  slot_hdr_init((Slot *) st->base, slot, nframes, fbytes, TRUE);
  store_bind(st);
  struct stat sb;
  if (rename(tmp, path) != 0 || fstat(fd, &sb) != 0) {
    GST_ERROR("Platz %d: %s nicht umbenennbar: %s", slot, path, g_strerror(errno));
    unlink(tmp); g_free(tmp); store_free(st); return NULL;
  }
  st->dev = sb.st_dev; st->ino = sb.st_ino;
  GST_INFO("Platz %d: Speicher %s angelegt (%d Faecher je %" G_GSIZE_FORMAT " Bytes, virtuell %.0f MB)", slot, path, nframes, fbytes, total / 1e6);
  g_free(tmp);
  return st;
}

/* Vorhandene Datei einblenden (Leser, oder Schreiber nach Neustart). NULL, wenn nicht vorhanden/ungueltig. */
static Store *store_open(const gchar *path, gint slot) {
  gint fd = open(path, O_RDWR | O_CLOEXEC);
  if (fd < 0) return NULL;
  struct stat sb;
  if (fstat(fd, &sb) != 0 || (gsize) sb.st_size < sizeof(Slot)) { close(fd); return NULL; }
  guint8 *base = mmap(NULL, sb.st_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (base == MAP_FAILED) { close(fd); return NULL; }
  Slot *s = (Slot *) base;
  gboolean ok = __atomic_load_n(&s->magic, __ATOMIC_ACQUIRE) == SHM_MAGIC && s->version == SHM_VERSION && s->hdr_bytes == sizeof(Slot) &&
                s->slot_no == slot && s->nframes >= 8 && s->nframes <= FMAX && s->fbytes >= g_page && s->fbytes % g_page == 0 &&
                s->total == (guint64) sb.st_size;
  if (ok) {
    guint64 fh, av, ad, fdo, tot;
    store_layout(s->nframes, s->fbytes, &fh, &av, &ad, &fdo, &tot);
    ok = fh == s->fh_off && av == s->av_off && ad == s->ad_off && fdo == s->fd_off && tot == s->total;
  }
  if (!ok) { GST_WARNING("Platz %d: %s ist kein gueltiger irlpip2-Speicher (andere Version?)", slot, path); munmap(base, sb.st_size); close(fd); return NULL; }
  Store *st = g_new0(Store, 1);
  st->refs = 1; st->slot = slot; st->shared = TRUE; st->fd = fd; st->dev = sb.st_dev; st->ino = sb.st_ino; st->base = base; st->len = sb.st_size; st->my_rd = -1;
  store_bind(st);
  return st;
}

static gchar *shm_path(const gchar *shm, gint slot) {
  if (!shm || !*shm) return NULL;
  return strchr(shm, '/') ? g_strdup_printf("%s-slot%d", shm, slot) : g_strdup_printf(SHM_DIR "%s-slot%d", shm, slot);
}

static TabEnt *tab_find_locked(const gchar *path, gint slot) {
  TabEnt *fr = NULL;
  for (gint i = 0; i < NTAB; i++) {
    TabEnt *e = &g_tab[i];
    if (!e->used) { if (!fr) fr = e; continue; }
    if (e->slot == slot && g_strcmp0(e->path, path) == 0) return e;
  }
  if (fr) { fr->used = TRUE; fr->slot = slot; fr->path = g_strdup(path); fr->st = NULL; fr->last_check = 0; }
  return fr;
}

/* Aktuellen Speicher eines Platzes holen (eigene Referenz, mit store_unref freigeben) oder NULL.
 * Schreiber (writer=TRUE): legt an bzw. uebernimmt eine passende vorhandene Datei; zu kleine Faecher -> neu anlegen.
 * Leser: blendet die Datei ein, sobald es sie gibt, und folgt einer neu angelegten Datei (andere Inode). */
static Store *store_get(const gchar *path, gint slot, gsize need, gint nframes, gboolean writer) {
  if (slot < 0 || slot >= NSLOTS) return NULL;
  if (nframes <= 0) nframes = DEF_FRAMES;
  g_mutex_lock(&g_lock);
  TabEnt *e = tab_find_locked(path, slot);
  if (!e) { g_mutex_unlock(&g_lock); return NULL; }
  Store *cur = e->st;
  const GstClockTime now = mono_now();
  if (writer) {
    gboolean ok = cur && !cur->s->dead && !cur->broken && cur->s->fbytes >= need;
    if (ok && path && now >= e->last_check + WRITER_CHECK_NS) {      /* Datei geloescht/ersetzt (z. B. vom Dienst)? */
      struct stat sb;
      e->last_check = now;
      if (stat(path, &sb) != 0 || sb.st_ino != cur->ino || sb.st_dev != cur->dev) ok = FALSE;
    }
    if (!ok) {
      Store *nw = NULL;
      if (path && (!cur || !cur->broken)) {
        nw = store_open(path, slot);                                 /* Neustart des Zubringers: Datei weiterverwenden */
        if (nw && (nw->s->fbytes < need || nw->s->nframes != CLAMP(nframes, 8, FMAX) || (cur && nw->ino == cur->ino))) { store_unref(nw); nw = NULL; }
      }
      if (!nw) nw = store_create(path, slot, nframes, MAX(need, DEF_FBYTES));
      if (nw) {
        if (cur) { cur->s->dead = 1; store_unref(cur); }
        e->st = cur = nw;
        e->last_check = now;
      }
    }
  } else if (path && now >= e->last_check + STORE_POLL_NS) {
    struct stat sb;
    e->last_check = now;
    if (stat(path, &sb) != 0) {
      if (cur && errno == ENOENT) { store_unref(cur); e->st = cur = NULL; }   /* Datei entfernt -> Speicher freigeben */
    } else if (!cur || sb.st_ino != cur->ino || sb.st_dev != cur->dev) {
      Store *nw = store_open(path, slot);
      if (nw) { if (cur) store_unref(cur); e->st = cur = nw; }
    }
  }
  Store *r = store_ref(cur);
  g_mutex_unlock(&g_lock);
  return r;
}

/* ---- Ring-Helfer (alle unter der Sperre des Speichers) ---- */

static void ring_pop_locked(Store *st) {
  Slot *s = st->s;
  gint i = s->ring[s->rd];
  if (i >= 0 && i < FMAX) s->inring[i] = 0;
  s->ring[s->rd] = -1;
  s->rd = (s->rd + 1) % FMAX;
  s->cnt--;
}

static void slot_clear_video_locked(Store *st) {
  Slot *s = st->s;
  if (s->cnt > FMAX) s->cnt = 0;
  while (s->cnt) ring_pop_locked(st);
  s->rd = 0;
  memset(s->inring, 0, sizeof s->inring);
}

static void slot_clear_audio_locked(Store *st) {
  memset(st->a_valid, 0, A_RING);
  st->s->a_have = FALSE;
}

/* Kamera neu: Ringe leeren, Zuordnung neu erfassen */
static void slot_reset_locked(Store *st) {
  slot_clear_video_locked(st);
  slot_clear_audio_locked(st);
  st->s->map_valid = FALSE;
  st->s->v_have_pts = st->s->a_have_pts = FALSE;
}

static gboolean store_lock(Store *st) {
  if (st->broken) return FALSE;
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  ts.tv_nsec += LOCK_TIMEOUT_MS * 1000000L;
  if (ts.tv_nsec >= 1000000000L) { ts.tv_sec += ts.tv_nsec / 1000000000L; ts.tv_nsec %= 1000000000L; }
  gint r = pthread_mutex_timedlock(&st->s->mtx, &ts);
  if (r == EOWNERDEAD) {
    /* Ein Prozess ist mit der Sperre gestorben (kill -9): Sperre konsistent machen, Platz sicherheitshalber neu */
    pthread_mutex_consistent(&st->s->mtx);
    GST_WARNING("Platz %d: Sperre eines beendeten Prozesses uebernommen - Platz wird neu erfasst", st->slot);
    slot_reset_locked(st);
    st->s->writing = -1;
    st->s->resets++;
    return TRUE;
  }
  if (r == 0) return TRUE;
  if (r == ENOTRECOVERABLE) st->broken = TRUE;
  GST_WARNING("Platz %d: Sperre nicht erhalten (%s)", st->slot, g_strerror(r));
  return FALSE;
}
static inline void store_unlock(Store *st) { pthread_mutex_unlock(&st->s->mtx); }

static gboolean pid_gone(gint32 pid) { return pid > 0 && kill(pid, 0) != 0 && errno == ESRCH; }

/* Schreiber-Rolle sichern. Fremder Schreiber, der noch lebt und liefert -> FALSE (Daten verwerfen, nie Ping-Pong).
 * Uebernahme (alter Zubringer tot/still): Generation +1, Platz zuruecksetzen -> Zuordnung wird neu gelernt. */
static gboolean writer_claim_locked(Store *st, GstClockTime now) {
  Slot *s = st->s;
  if (s->writer_token == g_token) { s->writer_beat = now; return TRUE; }
  if (s->writer_token && now <= s->writer_beat + WRITER_TAKEOVER_NS && !pid_gone(s->writer_pid)) { s->foreign++; return FALSE; }
  gboolean had = s->writer_token != 0;
  s->writer_token = g_token; s->writer_pid = getpid(); s->writer_gen++; s->writing = -1; s->writer_beat = now;
  if (had) {
    slot_reset_locked(st);
    s->resets++;
    GST_INFO("Platz %d: neuer Zubringer (Generation %u), Zuordnung wird neu gelernt", st->slot, s->writer_gen);
  }
  return TRUE;
}

static gboolean reader_dead(const ReaderEnt *r, GstClockTime now) {
  if (!r->token) return TRUE;
  if (r->token == g_token) return FALSE;
  return now > r->beat + READER_DEAD_NS || pid_gone(r->pid);
}

/* Leser-Eintrag dieses Prozesses (fuer Pins) finden oder belegen; bei Neubelegung eigene Pins neu eintragen. */
static gboolean reader_entry_locked(Store *st, GstClockTime now) {
  Slot *s = st->s;
  if (st->my_rd >= 0 && s->rdr[st->my_rd].token == g_token) { s->rdr[st->my_rd].beat = now; return TRUE; }
  gint pick = -1;
  for (gint i = 0; i < MAX_READERS && pick < 0; i++) if (s->rdr[i].token == g_token) pick = i;
  for (gint i = 0; i < MAX_READERS && pick < 0; i++) if (reader_dead(&s->rdr[i], now)) pick = i;
  if (pick < 0) { GST_WARNING("Platz %d: mehr als %d Ausgabe-Prozesse", st->slot, MAX_READERS); return FALSE; }
  ReaderEnt *r = &s->rdr[pick];
  memset(r, 0, sizeof *r);
  r->token = g_token; r->pid = getpid(); r->beat = now;
  for (gint i = 0; i < s->nframes; i++) if (st->lref[i]) r->pins[i / 64] |= 1ULL << (i % 64);
  st->my_rd = pick;
  return TRUE;
}

static void pin_locked(Store *st, gint i, GstClockTime now) {
  if (i < 0 || i >= st->s->nframes) return;
  if (st->lref[i]++ == 0 && reader_entry_locked(st, now)) st->s->rdr[st->my_rd].pins[i / 64] |= 1ULL << (i % 64);
}
static void unpin_locked(Store *st, gint i) {
  if (i < 0 || i >= st->s->nframes || !st->lref[i]) return;
  if (--st->lref[i] == 0 && st->my_rd >= 0 && st->s->rdr[st->my_rd].token == g_token) st->s->rdr[st->my_rd].pins[i / 64] &= ~(1ULL << (i % 64));
}

static void pins_collect_locked(Store *st, GstClockTime now, gboolean reap, guint64 *pins) {
  memset(pins, 0, sizeof(guint64) * PIN_WORDS);
  for (gint r = 0; r < MAX_READERS; r++) {
    ReaderEnt *e = &st->s->rdr[r];
    if (!e->token) continue;
    if (reap && reader_dead(e, now)) { memset(e, 0, sizeof *e); continue; }   /* Ausgabe-Prozess beendet: Pins frei */
    for (gint w = 0; w < PIN_WORDS; w++) pins[w] |= e->pins[w];
  }
}
#define PINNED(p, i) (((p)[(i) / 64] >> ((i) % 64)) & 1)

/* Freies Fach fuer das naechste Bild (niedrigster Index -> nur benoetigte Seiten werden belegt). Ring voll -> aeltestes
 * Bild opfern. -1: kein Fach (alle gepinnt/kein Platz). */
static gint store_alloc_locked(Store *st, GstClockTime now) {
  Slot *s = st->s;
  guint64 pins[PIN_WORDS];
  gboolean reap = now >= st->last_reap + 500 * GST_MSECOND;
  if (reap) st->last_reap = now;
  pins_collect_locked(st, now, reap, pins);
  if (s->bad_retry && now >= s->bad_retry) { memset(s->bad, 0, sizeof s->bad); s->bad_retry = 0; }
  for (;;) {
    for (gint i = 0; i < s->nframes; i++)
      if (!s->inring[i] && !s->bad[i] && i != s->writing && !PINNED(pins, i)) return i;
    if (!s->cnt) return -1;
    ring_pop_locked(st);
  }
}

/* Seiten eines freien Fachs an das System zurueckgeben */
static void store_release(Store *st, gint i) {
  guint8 *p = FDATA(st, i);
  const gsize n = MIN(align_up(st->s->touched[i], g_page), st->s->fbytes);
  if (st->shared) {
    /* Fach i beginnt bei i * fbytes (wie FDATA) - NICHT i * n: n ist nur der benutzte Teil, mit i * n traf das Loch
     * bei hohen Fachnummern ein fremdes, noch im Ring liegendes Bild (live 24.10.: genullte = gruene Hauptbilder) */
    if (fallocate(st->fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, (off_t) (st->s->fd_off + (guint64) i * st->s->fbytes), (off_t) n) != 0) madvise(p, n, MADV_REMOVE);
  } else madvise(p, n, MADV_DONTNEED);
  st->s->touched[i] = 0;
}

/* Hoechstens POOL_KEEP freie, belegte Faecher behalten (Wiederverwendung), den Rest freigeben */
static void store_trim_locked(Store *st, GstClockTime now) {
  Slot *s = st->s;
  guint64 pins[PIN_WORDS];
  pins_collect_locked(st, now, FALSE, pins);
  gint keep = 0;
  for (gint i = 0; i < s->nframes; i++) {
    if (!s->touched[i] || s->inring[i] || i == s->writing || PINNED(pins, i)) continue;
    if (++keep > POOL_KEEP) store_release(st, i);
  }
}

/* Alle Speicher dieses Prozesses loslassen (letzte Element-Instanz weg). Dateien bleiben bestehen. */
static void tab_free_all_locked(void) {
  for (gint i = 0; i < NTAB; i++) {
    if (g_tab[i].used) { store_unref(g_tab[i].st); g_free(g_tab[i].path); }
    memset(&g_tab[i], 0, sizeof g_tab[i]);
  }
  g_audio_slot = -1; g_silence_ms = 0;
}

static void inst_ref(void) { g_mutex_lock(&g_lock); g_instances++; g_mutex_unlock(&g_lock); }
static void inst_unref(void) {
  g_mutex_lock(&g_lock);
  if (--g_instances == 0) tab_free_all_locked();
  g_mutex_unlock(&g_lock);
}

/* Drift schaetzen (bei jedem abgeschlossenen Eimer): Eimer in drei Zeitabschnitte teilen, je Abschnitt das Minimum.
 * Gerade durch Minimum 1 und 3; sie gilt nur, wenn (a) |Steigung| <= DRIFT_MAX und (b) in JEDEM Abschnitt mind. zwei
 * Eimer-Minima auf der Geraden liegen (+-2 ms). So wird echte Uhrdrift (glatte untere Huellkurve) erkannt, aber eine
 * Verschlechterung des Netzes (Sprung der Laufzeit, zufaellige Minima bei starkem Jitter) nie als Drift gedeutet.
 * Danach: Ziel = niedrigste drift-bereinigte Ankunft im Fenster (untere Huellkurve, vergisst nichts innerhalb 120 s). */
static void drift_estimate_locked(Slot *s, GstClockTime now) {
  const gint n = CLAMP(s->bk_n, 0, NBKT);
  if (n >= 6) {
    gint64 t[NBKT], m[NBKT];
    for (gint i = 0; i < n; i++) { gint k = (s->bk_i - n + i + NBKT) % NBKT; t[i] = s->bk_t[k]; m[i] = s->bk_m[k]; }
    gint seg[4] = { 0, n / 3, 2 * n / 3, n }, im[3];
    for (gint g = 0; g < 3; g++) { im[g] = seg[g]; for (gint i = seg[g]; i < seg[g + 1]; i++) if (m[i] < m[im[g]]) im[g] = i; }
    gint64 span = t[im[2]] - t[im[0]];
    if (span >= (gint64) DRIFT_MIN_SPAN_NS) {
      gdouble bn = (gdouble) (m[im[2]] - m[im[0]]) / (gdouble) span;
      gboolean ok = fabs(bn) <= DRIFT_MAX;
      for (gint g = 0; g < 3 && ok; g++) {
        gint on = 0;
        for (gint i = seg[g]; i < seg[g + 1]; i++) {
          gdouble pred = m[im[0]] + bn * (gdouble) (t[i] - t[im[0]]);
          if (fabs(m[i] - pred) <= DRIFT_TOL_NS) on++;
        }
        if (on < 2) ok = FALSE;
      }
      if (ok) s->drift = bn;
    }
  }
  /* Ziel = min ueber Eimer von m_i + drift * (now - t_i), dazu der laufende Eimer */
  gint64 tg = s->cur_m == G_MAXINT64 ? G_MAXINT64 : s->cur_m + (gint64) (s->drift * (gdouble) ((gint64) now - s->cur_t));
  for (gint i = 0; i < n; i++) {
    gint k = (s->bk_i - n + i + NBKT) % NBKT;
    gint64 v = s->bk_m[k] + (gint64) (s->drift * (gdouble) ((gint64) now - s->bk_t[k]));
    if (v < tg) tg = v;
  }
  if (tg != G_MAXINT64) s->tgt = tg;
}

/* Zeitzuordnung fortschreiben (bei jedem Bild; Ton nur solange kein Bild da ist).
 * Sprung (pts zurueck > 100 ms, vor > 3 s, > 3 s keine Daten) -> Ringe leeren, neu erfassen.
 * off = untere Huellkurve von (Ankunft - pts):
 *   Lernphase (15 s): jedes neue Minimum gilt sofort (nur nach unten).
 *   danach: off laeuft mit der geschaetzten Drift mit und wird hoechstens 1 ms/s zum Ziel (untere Huellkurve,
 *   drift-bereinigt, 120 s) korrigiert. Ohne Drift-Nachweis steigt off nie (schlechter werdendes Netz != Drift). */
static void map_update_locked(Store *st, GstClockTime pts, GstClockTime now, gboolean video) {
  Slot *s = st->s;
  if (!GST_CLOCK_TIME_IS_VALID(pts)) return;
  if (s->map_valid) {
    gboolean own = video || !s->map_video;           /* Ton setzt eine Bild-Zuordnung nie zurueck */
    GstClockTime lp = video ? s->v_last_pts : s->a_last_pts;
    gboolean hp = video ? s->v_have_pts : s->a_have_pts;
    gboolean jump = FALSE;
    if (own && hp && (pts + JUMP_BACK_NS < lp || pts > lp + JUMP_FWD_NS)) jump = TRUE;
    if (own && now > s->last_arr + NODATA_NS) jump = TRUE;
    if (jump) {
      GST_INFO("Platz %d: Zeitsprung (pts %" GST_TIME_FORMAT " nach %" GST_TIME_FORMAT "), Zuordnung neu", st->slot, GST_TIME_ARGS(pts), GST_TIME_ARGS(lp));
      slot_reset_locked(st);
      s->resets++;
    }
  }
  gint64 d = (gint64) now - (gint64) pts;
  if (!s->map_valid || (video && !s->map_video)) {
    s->off = s->tgt = d;
    s->learn_until = now + LEARN_NS;
    s->map_valid = TRUE;
    s->map_video = video;
    s->epoch++;
    s->bk_n = s->bk_i = 0; s->cur_m = d; s->cur_t = (gint64) now; s->bstart = now; s->last_slew = now;
    s->drift = 0; s->bias = 0;
    if (video) s->pin_ns = 0;
  } else if (video || !s->map_video) {
    const gdouble dt = (gdouble) (now - s->last_slew);
    s->off += (gint64) (s->drift * dt);
    s->tgt += (gint64) (s->drift * dt);
    if (d < s->cur_m) { s->cur_m = d; s->cur_t = (gint64) now; }
    if (d < s->tgt) s->tgt = d;
    if (now < s->learn_until) { if (d < s->off) s->off = d; }
    else {
      gint64 step = (gint64) (dt * SLEW_PPM / 1e6);
      s->off += CLAMP(s->tgt - s->off, -step, step);
    }
    s->last_slew = now;
    if (now >= s->bstart + GST_SECOND) {            /* Eimer abschliessen */
      if (s->cur_m != G_MAXINT64) {
        s->bk_i = CLAMP(s->bk_i, 0, NBKT - 1);
        s->bk_t[s->bk_i] = s->cur_t; s->bk_m[s->bk_i] = s->cur_m;
        s->bk_i = (s->bk_i + 1) % NBKT; if (s->bk_n < NBKT) s->bk_n++;
      }
      s->cur_m = G_MAXINT64; s->cur_t = (gint64) now;
      s->bstart = now;
      drift_estimate_locked(s, now);
    }
  }
  if (video) {
    if (s->v_have_pts && pts > s->v_last_pts) {
      gint64 dd = (gint64) (pts - s->v_last_pts);
      if (dd > 5 * GST_MSECOND && dd < 200 * GST_MSECOND) s->pin_ns = s->pin_ns ? (s->pin_ns * 31 + dd) / 32 : dd;
    }
    s->v_last_pts = pts; s->v_have_pts = TRUE; s->v_last_arr = now;
  } else {
    s->a_last_pts = pts; s->a_have_pts = TRUE; s->a_last_arr = now;
  }
  s->last_arr = now;
}

/* Alte Bilder entfernen: Kopf weg, solange auch das naechste Bild aelter als das Ring-Zeitfenster ist */
static void evict_locked(Store *st, GstClockTime now) {
  Slot *s = st->s;
  if (s->cnt > (guint) s->nframes) slot_clear_video_locked(st);   /* Schutz gegen unsinnige Werte */
  while (s->cnt > (guint) MAX(s->nframes - 2, 1)) ring_pop_locked(st);
  if (!s->map_valid) return;
  const GstClockTime cap = s->cap_ns ? s->cap_ns : (DEF_LATENCY_MS + 500) * GST_MSECOND;
  gint64 lim = (gint64) now - (gint64) cap;
  while (s->cnt >= 2) {
    gint nx = s->ring[(s->rd + 1) % FMAX];
    if (nx < 0 || nx >= s->nframes) { slot_clear_video_locked(st); break; }
    if ((gint64) st->fh[nx].pts + s->off + s->bias >= lim) break;
    ring_pop_locked(st);
  }
}

/* ======================================================================================== Steuerdatei */

typedef struct {
  gboolean used;
  gint slot, x, y, w, h, cl, cr, ct, cb, border, alpha;
  guint8 bY, bU, bV;
} LayerCfg;

typedef struct {
  gint latency_ms, audio;
  gint delay_ms[NSLOTS];
  LayerCfg layer[NLAYERS];
} Config;

typedef struct {
  gchar *path;
  GstClockTime last_check;
  gint64 mtime_ns, ino, size;
  guint64 reloads;
} ConfPoll;

static void rgb_to_yuv(guint32 rgb, guint8 *Y, guint8 *U, guint8 *V) {
  gint r = (rgb >> 16) & 255, g = (rgb >> 8) & 255, b = rgb & 255;
  gint y = (66 * r + 129 * g + 25 * b + 128) / 256 + 16;
  gint u = (-38 * r - 74 * g + 112 * b + 128) / 256 + 128;
  gint v = (112 * r - 94 * g - 18 * b + 128) / 256 + 128;
  *Y = CLAMP(y, 16, 235); *U = CLAMP(u, 16, 240); *V = CLAMP(v, 16, 240);
}

static void config_default(Config *c, gint W, gint H) {
  memset(c, 0, sizeof *c);
  c->latency_ms = DEF_LATENCY_MS; c->audio = 0;
  c->layer[0] = (LayerCfg) { TRUE, 0, 0, 0, W, H, 0, 0, 0, 0, 0, 100, 235, 128, 128 };
}

/* Liest die Steuerdatei. Rueckgabe FALSE: Datei nicht lesbar (alte Einstellungen bleiben). */
static gboolean config_parse(const gchar *path, Config *out, gint W, gint H) {
  gchar *txt = NULL;
  if (!path || !g_file_get_contents(path, &txt, NULL, NULL)) return FALSE;
  Config c;
  config_default(&c, W, H);
  gboolean any_layer = FALSE;
  LayerCfg layers[NLAYERS];
  memset(layers, 0, sizeof layers);
  gchar **lines = g_strsplit(txt, "\n", -1);
  for (gchar **l = lines; *l; l++) {
    gchar *hash = strchr(*l, '#');
    if (hash) *hash = 0;
    gchar *s = g_strstrip(*l);
    if (!*s) continue;
    gchar **tok = g_strsplit_set(s, " \t", -1);
    gint layer = -1, slot = -1, delay = -1;
    LayerCfg lc = { TRUE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 100, 235, 128, 128 };
    for (gchar **t = tok; *t; t++) {
      gchar *eq = strchr(*t, '=');
      if (!eq) continue;
      *eq = 0;
      const gchar *k = *t, *v = eq + 1;
      gint iv = atoi(v);
      if (!strcmp(k, "latency")) c.latency_ms = CLAMP(iv, 500, 3000);
      else if (!strcmp(k, "audio")) c.audio = (iv >= 0 && iv < NSLOTS) ? iv : -1;
      else if (!strcmp(k, "layer")) layer = iv;
      else if (!strcmp(k, "slot")) slot = iv;
      else if (!strcmp(k, "delay")) delay = CLAMP(iv, 0, 3000);
      else if (!strcmp(k, "x")) lc.x = iv & ~1;
      else if (!strcmp(k, "y")) lc.y = iv & ~1;
      else if (!strcmp(k, "w")) lc.w = MAX(iv, 0) & ~1;
      else if (!strcmp(k, "h")) lc.h = MAX(iv, 0) & ~1;
      else if (!strcmp(k, "cl")) lc.cl = CLAMP(iv, 0, 450);
      else if (!strcmp(k, "cr")) lc.cr = CLAMP(iv, 0, 450);
      else if (!strcmp(k, "ct")) lc.ct = CLAMP(iv, 0, 450);
      else if (!strcmp(k, "cb")) lc.cb = CLAMP(iv, 0, 450);
      else if (!strcmp(k, "border")) lc.border = CLAMP(iv, 0, 64);
      else if (!strcmp(k, "alpha")) lc.alpha = CLAMP(iv, 0, 100);
      else if (!strcmp(k, "color")) rgb_to_yuv((guint32) strtoul(v, NULL, 16), &lc.bY, &lc.bU, &lc.bV);
    }
    g_strfreev(tok);
    if (layer >= 0 && layer < NLAYERS) {
      any_layer = TRUE;
      if (slot >= 0 && slot < NSLOTS && lc.w > 0 && lc.h > 0) { lc.slot = slot; layers[layer] = lc; }
    } else if (slot >= 0 && slot < NSLOTS && delay >= 0) c.delay_ms[slot] = delay;
  }
  g_strfreev(lines);
  g_free(txt);
  if (any_layer) memcpy(c.layer, layers, sizeof layers);
  *out = c;
  return TRUE;
}

/* Alle 200 ms mtime/Inode/Groesse pruefen; bei Aenderung neu lesen. TRUE = neu geladen. */
static gboolean config_poll(ConfPoll *p, Config *c, gint W, gint H, gboolean force) {
  GstClockTime now = sys_now();
  if (!force && now < p->last_check + CONF_POLL_NS) return FALSE;
  p->last_check = now;
  if (!p->path) return FALSE;
  struct stat st;
  if (g_stat(p->path, &st) != 0) return FALSE;
  gint64 m = (gint64) st.st_mtim.tv_sec * 1000000000LL + st.st_mtim.tv_nsec;
  if (!force && m == p->mtime_ns && (gint64) st.st_ino == p->ino && (gint64) st.st_size == p->size) return FALSE;
  if (!config_parse(p->path, c, W, H)) return FALSE;
  p->mtime_ns = m; p->ino = st.st_ino; p->size = st.st_size; p->reloads++;
  return TRUE;
}

/* ======================================================================================== Uhr-Helfer der Ausgaenge */

typedef struct {
  GstClockID id;
  gboolean flushing, started;
  GstClockTime rt0;                         /* Laufzeit beim Start */
  guint64 n;                                /* Zaehler (Bilder bzw. Samples) */
  guint64 late;
} Ticker;

/* Wartet bis zur Uhrzeit base+wake_rt. Rueckgabe: GST_FLOW_OK oder FLUSHING; *t_sys = Systemuhrzeit zu base+at_rt,
 * *late_ns = Verspaetung beim Aufwachen. */
static GstFlowReturn ticker_wait(GstElement *el, Ticker *tk, GstClockTime wake_rt, GstClockTime at_rt, GstClockTime *t_sys, GstClockTimeDiff *late_ns) {
  GstClock *clock = gst_element_get_clock(el);
  GstClockTime base = gst_element_get_base_time(el);
  if (!clock) { clock = gst_object_ref(g_sysclock); }
  GST_OBJECT_LOCK(el);
  if (tk->flushing) { GST_OBJECT_UNLOCK(el); gst_object_unref(clock); return GST_FLOW_FLUSHING; }
  tk->id = gst_clock_new_single_shot_id(clock, base + wake_rt);
  GstClockID id = tk->id;
  GST_OBJECT_UNLOCK(el);
  GstClockTimeDiff jit = 0;
  GstClockReturn r = gst_clock_id_wait(id, &jit);
  GST_OBJECT_LOCK(el);
  gst_clock_id_unref(id);
  tk->id = NULL;
  GST_OBJECT_UNLOCK(el);
  if (r == GST_CLOCK_UNSCHEDULED) { gst_object_unref(clock); return GST_FLOW_FLUSHING; }
  *late_ns = r == GST_CLOCK_EARLY ? MAX(jit, 0) : 0;
  if (clock == g_sysclock) *t_sys = base + at_rt + g_sys2mono;   /* in CLOCK_MONOTONIC (Zeitbasis der Speicher) */
  else {                                    /* fremde Pipeline-Uhr: auf CLOCK_MONOTONIC umrechnen */
    GstClockTime c = gst_clock_get_time(clock), s = sys_now();
    *t_sys = s - (c - (base + at_rt));
  }
  gst_object_unref(clock);
  return GST_FLOW_OK;
}

static GstClockTime ticker_start(GstElement *el, Ticker *tk) {
  if (!tk->started) {
    GstClock *clock = gst_element_get_clock(el);
    GstClockTime base = gst_element_get_base_time(el), now = clock ? gst_clock_get_time(clock) : 0;
    tk->rt0 = (clock && now > base) ? now - base : 0;
    tk->n = 0; tk->started = TRUE;
    if (clock) gst_object_unref(clock);
  }
  return tk->rt0;
}

static void ticker_unlock(GstElement *el, Ticker *tk) {
  GST_OBJECT_LOCK(el);
  tk->flushing = TRUE;
  if (tk->id) gst_clock_id_unschedule(tk->id);
  GST_OBJECT_UNLOCK(el);
}
static void ticker_unlock_stop(GstElement *el, Ticker *tk) { GST_OBJECT_LOCK(el); tk->flushing = FALSE; GST_OBJECT_UNLOCK(el); }

/* Positionsabfrage an den Eingaengen beantworten wir NICHT: Die Position einer Pipeline ist das Maximum ueber alle
 * Senken; belacoder beendet den Stream, wenn sie 2-4 s steht. Ein stehender Kamera-Eingang darf das nie ausloesen. */
static GstElementClass *vsink_el_parent, *asink_el_parent;
static gboolean sink_element_query(GstElement *el, GstQuery *q, GstElementClass *parent) {
  if (GST_QUERY_TYPE(q) == GST_QUERY_POSITION || GST_QUERY_TYPE(q) == GST_QUERY_DURATION) return FALSE;
  if (GST_QUERY_TYPE(q) == GST_QUERY_LATENCY) { gst_query_set_latency(q, FALSE, 0, GST_CLOCK_TIME_NONE); return TRUE; }   /* s. sink_query */
  return parent->query(el, q);
}

/* Latenz an den Eingaengen: nicht live melden. Die Eingaenge synchronisieren nicht (sync=false); ihre Zweige duerfen
 * die Pipeline-Latenz (und damit die Wartezeit von Muxer/appsink) nicht beeinflussen. */
static GstBaseSinkClass *vsink_bs_parent, *asink_bs_parent;
static gboolean sink_query(GstBaseSink *bs, GstQuery *q, GstBaseSinkClass *parent) {
  if (GST_QUERY_TYPE(q) == GST_QUERY_LATENCY) { gst_query_set_latency(q, FALSE, 0, GST_CLOCK_TIME_NONE); return TRUE; }
  return parent->query(bs, q);
}
static gboolean vsink_query(GstBaseSink *bs, GstQuery *q) { return sink_query(bs, q, vsink_bs_parent); }
static gboolean asink_query(GstBaseSink *bs, GstQuery *q) { return sink_query(bs, q, asink_bs_parent); }

/* Gemeinsamer Anfang beider Senken (Eigenschaften slot, shm, ring-frames) */
typedef struct {
  GstBaseSink parent;
  gint slot;
  gchar *shm;                              /* NULL/"": im Prozess; sonst Name/Pfad-Praefix des gemeinsamen Speichers */
  gint frames;                             /* Bildfaecher je Platz (0 = Standard DEF_FRAMES) */
  gchar *path;                             /* aus shm + slot beim Start */
  gint64 last_warn;
} SinkCommon;

enum { PROP_SLOT = 1, PROP_SHM, PROP_FRAMES };

static void sink_common_props(GObjectClass *go) {
  g_object_class_install_property(go, PROP_SLOT, g_param_spec_int("slot", "slot", "Kamera-Platz (0..3)", 0, NSLOTS - 1, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_SHM, g_param_spec_string("shm", "shm",
      "Gemeinsamer Speicher fuer Zubringer-Prozesse: Name (Datei /dev/shm/<name>-slotN) oder Pfad-Praefix mit '/'; leer = im Prozess", NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_FRAMES, g_param_spec_int("ring-frames", "ring-frames",
      "Bildfaecher je Platz beim Anlegen (0 = 203 = 6,5 s bei 30 fps; belegt wird nur, was latency+delay+500 ms brauchen)", 0, FMAX, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
}
static void sink_set_property(GObject *o, guint id, const GValue *v, GParamSpec *p) {
  SinkCommon *c = (SinkCommon *) o;
  switch (id) {
    case PROP_SLOT: c->slot = g_value_get_int(v); break;
    case PROP_SHM: g_free(c->shm); c->shm = g_value_dup_string(v); break;
    case PROP_FRAMES: c->frames = g_value_get_int(v); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
  }
}
static void sink_get_property(GObject *o, guint id, GValue *v, GParamSpec *p) {
  SinkCommon *c = (SinkCommon *) o;
  switch (id) {
    case PROP_SLOT: g_value_set_int(v, c->slot); break;
    case PROP_SHM: g_value_set_string(v, c->shm); break;
    case PROP_FRAMES: g_value_set_int(v, c->frames); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
  }
}
static gboolean sink_start(GstBaseSink *bs) {
  SinkCommon *c = (SinkCommon *) bs;
  g_free(c->path);
  c->path = shm_path(c->shm, c->slot);
  return TRUE;
}
static Store *sink_store(SinkCommon *c, gsize need) {
  Store *st = store_get(c->path, c->slot, need, c->frames, TRUE);
  if (!st && g_get_monotonic_time() > c->last_warn + 5000000) {
    c->last_warn = g_get_monotonic_time();
    GST_ELEMENT_WARNING(c, RESOURCE, OPEN_WRITE, ("Platz %d: kein Speicher (%s)", c->slot, c->path ? c->path : "im Prozess"), (NULL));
  }
  return st;
}
static void sink_finalize_common(SinkCommon *c) { g_free(c->shm); g_free(c->path); }

/* EOS einer Kamera verwerfen (sonst endet die ganze Ausgabe); der Platz gilt als weg. */
static GstPadProbeReturn sink_eos_probe(GstPad *pad, GstPadProbeInfo *info, gpointer data) {
  GstEvent *ev = GST_PAD_PROBE_INFO_EVENT(info);
  if (ev && GST_EVENT_TYPE(ev) == GST_EVENT_EOS) {
    GstObject *parent = gst_pad_get_parent(pad);
    gint slot = -1;
    if (parent) {
      SinkCommon *c = (SinkCommon *) parent;
      slot = c->slot;
      Store *st = store_get(c->path, c->slot, 0, c->frames, FALSE);
      if (st && store_lock(st)) {
        if (st->s->writer_token == g_token) { if (GPOINTER_TO_INT(data)) st->s->eos_v = TRUE; else st->s->eos_a = TRUE; }
        store_unlock(st);
      }
      store_unref(st);
      gst_object_unref(parent);
    }
    GST_INFO_OBJECT(pad, "EOS von Platz %d verworfen", slot);
    return GST_PAD_PROBE_DROP;
  }
  return GST_PAD_PROBE_OK;
}

/* ======================================================================================== irlpip2vsink */

typedef struct { SinkCommon c; GstVideoInfo info; gboolean have_info; } IrlPip2VSink;
typedef struct { GstBaseSinkClass parent_class; } IrlPip2VSinkClass;
G_DEFINE_TYPE(IrlPip2VSink, irl_pip2_vsink, GST_TYPE_BASE_SINK)

static GstStaticPadTemplate vsink_tmpl = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS("video/x-raw, format=(string)NV12"));

static inline gsize nv12_bytes(gint w, gint h) { return (gsize) w * h + (gsize) ((w + 1) & ~1) * ((h + 1) / 2); }

static gboolean vsink_set_caps(GstBaseSink *bs, GstCaps *caps) {
  IrlPip2VSink *self = (IrlPip2VSink *) bs;
  GstVideoInfo info;
  if (!gst_video_info_from_caps(&info, caps)) return FALSE;
  self->info = info; self->have_info = TRUE;
  const gint w = GST_VIDEO_INFO_WIDTH(&info), h = GST_VIDEO_INFO_HEIGHT(&info);
  Store *st = sink_store(&self->c, nv12_bytes(w, h));      /* legt den Speicher passend zur Bildgroesse an */
  if (st && store_lock(st)) {
    if (writer_claim_locked(st, sys_now())) { st->s->w = w; st->s->h = h; }
    store_unlock(st);
  }
  store_unref(st);
  GST_INFO_OBJECT(self, "Platz %d: %dx%d%s%s", self->c.slot, w, h, self->c.path ? " -> " : "", self->c.path ? self->c.path : "");
  return TRUE;
}

static GstFlowReturn vsink_render(GstBaseSink *bs, GstBuffer *buf) {
  IrlPip2VSink *self = (IrlPip2VSink *) bs;
  if (!self->have_info) return GST_FLOW_OK;
  GstVideoFrame vf;
  if (!gst_video_frame_map(&vf, &self->info, buf, GST_MAP_READ)) return GST_FLOW_OK;
  const gint w = GST_VIDEO_FRAME_WIDTH(&vf), h = GST_VIDEO_FRAME_HEIGHT(&vf), uvs = (w + 1) & ~1, ch = (h + 1) / 2;
  const gsize size = nv12_bytes(w, h);
  GstClockTime pts = GST_BUFFER_PTS(buf);
  const GstClockTime now = sys_now();
  Store *st = sink_store(&self->c, size);
  if (!st) { gst_video_frame_unmap(&vf); return GST_FLOW_OK; }
  gint idx = -1;
  guint32 gen = 0;
  if (store_lock(st)) {
    Slot *s = st->s;
    if (writer_claim_locked(st, now)) {
      if (!GST_CLOCK_TIME_IS_VALID(pts)) pts = s->map_valid ? (GstClockTime) MAX((gint64) now - s->off, 0) : now;
      map_update_locked(st, pts, now, TRUE);
      test_die(&g_die_lock);
      s->in++; s->eos_v = FALSE; s->w = w; s->h = h;
      idx = store_alloc_locked(st, now);
      if (idx >= 0) { s->writing = idx; gen = s->writer_gen; }
    }
    store_unlock(st);
  }
  /* Dateiseiten des Fachs vorab sicher belegen: bei vollem /dev/shm Fehler statt SIGBUS beim Kopieren */
  const gsize need_pg = align_up(size, g_page);
  if (idx >= 0 && st->shared && st->s->touched[idx] < need_pg &&
      posix_fallocate(st->fd, (off_t) (st->s->fd_off + (guint64) idx * st->s->fbytes), (off_t) need_pg) != 0) {
    if (store_lock(st)) {
      if (st->s->writer_gen == gen) { st->s->bad[idx] = 1; st->s->nospace++; if (st->s->writing == idx) st->s->writing = -1; if (!st->s->bad_retry) st->s->bad_retry = now + BAD_RETRY_NS; }
      store_unlock(st);
    }
    if (g_get_monotonic_time() > self->c.last_warn + 5000000) {
      self->c.last_warn = g_get_monotonic_time();
      GST_ELEMENT_WARNING(self, RESOURCE, NO_SPACE_LEFT, ("Platz %d: gemeinsamer Speicher voll (%s)", self->c.slot, self->c.path), (NULL));
    }
    idx = -1;
  }
  if (idx >= 0) {
    /* Einmalige Kopie aus dem (fuer die CPU evtl. langsamen) Decoder-Speicher in das freie Fach, Zeile fuer Zeile */
    guint8 *dst = FDATA(st, idx);
    gint64 t0 = g_get_monotonic_time();
    const guint8 *py = GST_VIDEO_FRAME_PLANE_DATA(&vf, 0), *puv = GST_VIDEO_FRAME_PLANE_DATA(&vf, 1);
    const gint sy = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, 0), suv = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, 1);
    if (g_die_copy == 1) { memcpy(dst, py, (gsize) w * (h / 2)); test_die(&g_die_copy); }   /* halbes Bild, dann tot */
    else test_die(&g_die_copy);
    if (sy == w) memcpy(dst, py, (gsize) w * h);
    else for (gint i = 0; i < h; i++) memcpy(dst + (gsize) i * w, py + (gsize) i * sy, w);
    guint8 *duv = dst + (gsize) w * h;
    if (suv == uvs) memcpy(duv, puv, (gsize) uvs * ch);
    else for (gint i = 0; i < ch; i++) memcpy(duv + (gsize) i * uvs, puv + (gsize) i * suv, uvs);
    gint64 dt = g_get_monotonic_time() - t0;
    FHdr *fh = &st->fh[idx];
    fh->w = w; fh->h = h; fh->uvs = uvs; fh->size = size; fh->pts = pts; fh->arr = now;
    if (store_lock(st)) {                   /* erst jetzt sichtbar: unter der Sperre in den Ring haengen */
      Slot *s = st->s;
      if (s->writer_token == g_token && s->writer_gen == gen && s->writing == idx) {
        s->writing = -1; s->touched[idx] = MAX(s->touched[idx], (guint32) need_pg);
        s->copy_us_sum += dt; s->copy_n++; if ((guint64) dt > s->copy_us_max) s->copy_us_max = dt;
        if (s->cnt && st->fh[s->ring[(s->rd + s->cnt - 1) % FMAX]].pts >= pts) s->dup++;   /* nicht aufsteigend: verwerfen */
        else {
          if (s->have_target && (gint64) pts + s->off + s->bias <= s->last_target) s->late++;
          s->ring[(s->rd + s->cnt) % FMAX] = idx; s->inring[idx] = 1; s->cnt++;
        }
        evict_locked(st, now);
        store_trim_locked(st, now);
      }
      store_unlock(st);
    }
  }
  gst_video_frame_unmap(&vf);
  store_unref(st);
  return GST_FLOW_OK;
}

static gboolean vsink_el_query(GstElement *el, GstQuery *q) { return sink_element_query(el, q, vsink_el_parent); }
static void vsink_finalize(GObject *o) { sink_finalize_common((SinkCommon *) o); G_OBJECT_CLASS(irl_pip2_vsink_parent_class)->finalize(o); inst_unref(); }

static void irl_pip2_vsink_class_init(IrlPip2VSinkClass *k) {
  GObjectClass *go = G_OBJECT_CLASS(k);
  GstElementClass *el = GST_ELEMENT_CLASS(k);
  GstBaseSinkClass *bs = GST_BASE_SINK_CLASS(k);
  go->set_property = sink_set_property; go->get_property = sink_get_property; go->finalize = vsink_finalize;
  sink_common_props(go);
  gst_element_class_add_static_pad_template(el, &vsink_tmpl);
  gst_element_class_set_static_metadata(el, "IRL PiP2 Bild-Eingang", "Sink/Video", "Legt NV12-Kamerabilder mit Zeitzuordnung im Ring ab (im Prozess oder gemeinsamer Speicher)", "IRL Streamer OS");
  vsink_el_parent = GST_ELEMENT_CLASS(irl_pip2_vsink_parent_class);
  el->query = vsink_el_query;
  vsink_bs_parent = GST_BASE_SINK_CLASS(irl_pip2_vsink_parent_class);
  bs->set_caps = vsink_set_caps; bs->render = vsink_render; bs->query = vsink_query; bs->start = sink_start;
}
static void irl_pip2_vsink_init(IrlPip2VSink *self) {
  inst_ref();
  gst_base_sink_set_sync(GST_BASE_SINK(self), FALSE);
  gst_base_sink_set_async_enabled(GST_BASE_SINK(self), FALSE);
  gst_base_sink_set_qos_enabled(GST_BASE_SINK(self), FALSE);
  gst_base_sink_set_last_sample_enabled(GST_BASE_SINK(self), FALSE);   /* keinen Decoder-Puffer festhalten */
  gst_pad_add_probe(GST_BASE_SINK_PAD(self), GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM, sink_eos_probe, GINT_TO_POINTER(1), NULL);
}

/* ======================================================================================== irlpip2asink */

typedef struct { SinkCommon c; } IrlPip2ASink;
typedef struct { GstBaseSinkClass parent_class; } IrlPip2ASinkClass;
G_DEFINE_TYPE(IrlPip2ASink, irl_pip2_asink, GST_TYPE_BASE_SINK)

static GstStaticPadTemplate asink_tmpl = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("audio/x-raw, format=(string)S16LE, rate=(int)48000, channels=(int)2, layout=(string)interleaved"));

/* Bereich [a, b) im Tonring als ungueltig (Stille) markieren */
static void aring_invalidate(Store *st, gint64 a, gint64 b) {
  if (b - a >= A_RING) { memset(st->a_valid, 0, A_RING); return; }
  for (gint64 i = a; i < b;) {
    gint64 p = ((i % A_RING) + A_RING) % A_RING, n = MIN(b - i, A_RING - p);
    memset(st->a_valid + p, 0, n);
    i += n;
  }
}

static GstFlowReturn asink_render(GstBaseSink *bs, GstBuffer *buf) {
  IrlPip2ASink *self = (IrlPip2ASink *) bs;
  GstMapInfo mi;
  if (!gst_buffer_map(buf, &mi, GST_MAP_READ)) return GST_FLOW_OK;
  gint64 n = mi.size / (A_CH * 2);
  GstClockTime pts = GST_BUFFER_PTS(buf), now = sys_now();
  Store *st = sink_store(&self->c, 0);
  if (st && store_lock(st)) {
    Slot *s = st->s;
    if (writer_claim_locked(st, now)) {
      map_update_locked(st, pts, now, FALSE);
      s->a_in++; s->eos_a = FALSE;
      gint64 s0 = GST_CLOCK_TIME_IS_VALID(pts) ? (gint64) gst_util_uint64_scale_round(pts, A_RATE, GST_SECOND) : (s->a_have ? s->a_end : 0);
      if (s->a_have) {
        gint64 diff = s0 - s->a_end;
        if (ABS(diff) <= A_TOL) s0 = s->a_end;                      /* lueckenlos weiterschreiben */
        else if (diff > 0) aring_invalidate(st, s->a_end, s0);       /* Luecke = Stille */
        else if (diff < -A_JUMP_BACK) slot_clear_audio_locked(st);   /* Ruecksprung: Tonring neu */
      }
      if (n > A_RING) { s0 += n - A_RING; n = A_RING; }
      const gint16 *src = (const gint16 *) mi.data + (mi.size / (A_CH * 2) - n) * A_CH;
      for (gint64 i = 0; i < n;) {
        gint64 p = (s0 + i) % A_RING, c = MIN(n - i, A_RING - p);
        if (p < 0) { p += A_RING; c = MIN(n - i, A_RING - p); }
        memcpy(st->a_data + p * A_CH, src + i * A_CH, c * A_CH * 2);
        memset(st->a_valid + p, 1, c);
        i += c;
      }
      if (!s->a_have || s0 + n > s->a_end) s->a_end = s0 + n;
      s->a_have = TRUE;
    }
    store_unlock(st);
  }
  store_unref(st);
  gst_buffer_unmap(buf, &mi);
  return GST_FLOW_OK;
}

static gboolean asink_el_query(GstElement *el, GstQuery *q) { return sink_element_query(el, q, asink_el_parent); }
static void asink_finalize(GObject *o) { sink_finalize_common((SinkCommon *) o); G_OBJECT_CLASS(irl_pip2_asink_parent_class)->finalize(o); inst_unref(); }

static void irl_pip2_asink_class_init(IrlPip2ASinkClass *k) {
  GObjectClass *go = G_OBJECT_CLASS(k);
  GstElementClass *el = GST_ELEMENT_CLASS(k);
  GstBaseSinkClass *bs = GST_BASE_SINK_CLASS(k);
  go->set_property = sink_set_property; go->get_property = sink_get_property; go->finalize = asink_finalize;
  sink_common_props(go);
  gst_element_class_add_static_pad_template(el, &asink_tmpl);
  gst_element_class_set_static_metadata(el, "IRL PiP2 Ton-Eingang", "Sink/Audio", "Legt Kamera-Ton mit Zeitzuordnung im Ring ab (im Prozess oder gemeinsamer Speicher)", "IRL Streamer OS");
  asink_el_parent = GST_ELEMENT_CLASS(irl_pip2_asink_parent_class);
  el->query = asink_el_query;
  asink_bs_parent = GST_BASE_SINK_CLASS(irl_pip2_asink_parent_class);
  bs->render = asink_render; bs->query = asink_query; bs->start = sink_start;
}
static void irl_pip2_asink_init(IrlPip2ASink *self) {
  inst_ref();
  gst_base_sink_set_sync(GST_BASE_SINK(self), FALSE);
  gst_base_sink_set_async_enabled(GST_BASE_SINK(self), FALSE);
  gst_base_sink_set_qos_enabled(GST_BASE_SINK(self), FALSE);
  gst_base_sink_set_last_sample_enabled(GST_BASE_SINK(self), FALSE);
  gst_pad_add_probe(GST_BASE_SINK_PAD(self), GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM, sink_eos_probe, GINT_TO_POINTER(0), NULL);
}

/* ======================================================================================== Skalierer (NV12, C) */

/* Trennbarer Filter: je Zielpunkt Startindex + T Gewichte (8 Bit, Summe 256). Verkleinern = Flaechenmittel,
 * Vergroessern = bilinear, gleich gross = Kopie. 8-Bit-Gewichte erlauben 16-Bit-Summen in der Vertikalen
 * (255 * 256 passt in uint16) -> der Compiler vektorisiert (NEON umull/umlal, SSE pmullw). */
typedef struct {
  gint n_src, n_dst, taps;
  gint32 *start;
  gint16 *ntap;
  guint16 *w;
} Filt;

static void filt_free(Filt *f) { g_free(f->start); g_free(f->ntap); g_free(f->w); memset(f, 0, sizeof *f); }

static void filt_make(Filt *f, gint ns, gint nd) {
  if (f->n_src == ns && f->n_dst == nd && f->w) return;
  filt_free(f);
  f->n_src = ns; f->n_dst = nd;
  gdouble sc = (gdouble) ns / nd;
  f->taps = ns == nd ? 1 : ns > nd ? MIN((gint) ceil(sc) + 1, 63) : 2;
  f->start = g_new0(gint32, nd); f->ntap = g_new0(gint16, nd); f->w = g_new0(guint16, (gsize) nd * f->taps);
  gdouble wt[64];
  for (gint j = 0; j < nd; j++) {
    gint st = 0, nt = 0;
    if (ns == nd) { st = j; nt = 1; wt[0] = 1.0; }
    else if (ns > nd) {                         /* Flaechenmittel */
      gdouble a = j * sc, b = (j + 1) * sc;
      st = (gint) floor(a);
      for (gint k = st; k < ns && k < b && nt < f->taps; k++) { gdouble ov = MIN(b, k + 1.0) - MAX(a, (gdouble) k); wt[nt++] = ov > 0 ? ov / sc : 0; }
    } else {                                    /* bilinear, Mitten ausgerichtet */
      gdouble x = (j + 0.5) * sc - 0.5;
      gint x0 = (gint) floor(x);
      gdouble fr = x - x0;
      if (x0 < 0) { x0 = 0; fr = 0; }
      if (x0 >= ns - 1) { x0 = ns - 1; fr = 0; }
      st = x0; wt[0] = 1.0 - fr; nt = 1;
      if (fr > 0) { wt[1] = fr; nt = 2; }
    }
    guint16 *w = f->w + (gsize) j * f->taps;
    gint sum = 0, imax = 0;
    for (gint k = 0; k < nt; k++) { gint q = (gint) lround(wt[k] * 256.0); w[k] = q; sum += q; if (q > w[imax]) imax = k; }
    w[imax] += 256 - sum;
    while (nt > 1 && w[nt - 1] == 0) nt--;
    /* Index nie hinter das Ende (Gewichte 0 haben keinen Einfluss, Zwischenspeicher ist aufgefuellt) */
    f->start[j] = st; f->ntap[j] = nt;
  }
}

typedef struct {
  gint key[4];                                 /* Ausschnitt w,h, Ziel w,h */
  Filt hy, vy, hc, vc;
  gboolean ok;
  gint cx0, cy0;                               /* Ausschnitt links/oben in Bildpunkten */
} ScalePlan;

/* Zwischenspeicher je Rechenfaden (max. 8192 Bildpunkte breit) */
#define SCRATCH_W (8192 + 256)
typedef struct { guint16 tmp[SCRATCH_W]; guint8 row[SCRATCH_W]; } Scratch;

static void plan_free(ScalePlan *p) { filt_free(&p->hy); filt_free(&p->vy); filt_free(&p->hc); filt_free(&p->vc); memset(p, 0, sizeof *p); }

static void plan_make(ScalePlan *p, gint cw, gint chh, gint dw, gint dh) {
  if (p->key[0] == cw && p->key[1] == chh && p->key[2] == dw && p->key[3] == dh && p->hy.w) return;
  plan_free(p);
  p->key[0] = cw; p->key[1] = chh; p->key[2] = dw; p->key[3] = dh;
  filt_make(&p->hy, cw, dw); filt_make(&p->vy, chh, dh);
  filt_make(&p->hc, cw / 2, dw / 2); filt_make(&p->vc, (chh + 1) / 2, dh / 2);
}

/* Vertikal: gewichtete Summe von nt Quellzeilen (zeilenweise, sequentiell, vektorisierbar) */
static inline void vpass(const guint8 *src, gsize sstride, gint n, const guint16 *vw, gint nt, guint16 *restrict acc) {
  const guint8 *restrict s0 = src;
  const guint16 w0 = vw[0];
  for (gint x = 0; x < n; x++) acc[x] = (guint16) (w0 * s0[x]);
  for (gint k = 1; k < nt; k++) {
    const guint8 *restrict sk = src + (gsize) k * sstride;
    const guint16 wk = vw[k];
    for (gint x = 0; x < n; x++) acc[x] = (guint16) (acc[x] + wk * sk[x]);
  }
}

/* Eine Ebene skalieren. ch = 1 (Y) oder 2 (UV verschraenkt). src zeigt auf die linke obere Ecke des Ausschnitts,
 * dst auf die linke obere Ecke des Zielrechtecks; nur Zeilen r0..r1 und Spalten c0..c1 (sichtbar) werden geschrieben. */
static void scale_plane(const guint8 *src, gint sstride, const gint ch, const Filt *hf, const Filt *vf, guint16 *tmpbuf, guint8 *rowbuf,
                        guint8 *dst, gint dstride, gint r0, gint r1, gint c0, gint c1, gint a256) {
  const gint n = hf->n_src * ch, ht = hf->taps;
  const gboolean hid = hf->n_src == hf->n_dst;
  gint last_st = -1, last_w = -1, last_nt = -1;
  for (gint i = r0; i < r1; i++) {
    guint8 *d = dst + (gsize) i * dstride + (gsize) c0 * ch;
    const gint st = vf->start[i], nt = vf->ntap[i];
    const guint16 *vw = vf->w + (gsize) i * vf->taps;
    const guint8 *out;
    if (hid && nt == 1) out = src + (gsize) st * sstride + (gsize) c0 * ch;   /* reine Zeilenkopie */
    else {
      if (!(st == last_st && nt == last_nt && vw[0] == last_w && nt == 1)) {
        vpass(src + (gsize) st * sstride, sstride, n, vw, nt, tmpbuf);
        last_st = st; last_nt = nt; last_w = vw[0];
      }
      guint8 *restrict row = rowbuf;
      const guint16 *restrict tmp = tmpbuf;
      if (hid) for (gint x = c0 * ch; x < c1 * ch; x++) row[x - c0 * ch] = (guint8) ((tmp[x] + 128) >> 8);
      else if (ht == 2) {                       /* bilinear: 2 Gewichte */
        const gint32 *hs = hf->start;
        const guint16 *hw = hf->w;
        if (ch == 1) for (gint j = c0; j < c1; j++) {
          const guint16 *t = tmp + hs[j];
          row[j - c0] = (guint8) ((hw[2 * j] * (guint32) t[0] + hw[2 * j + 1] * (guint32) t[1] + 32768) >> 16);
        } else for (gint j = c0; j < c1; j++) {
          const guint16 *t = tmp + 2 * hs[j];
          const guint32 w0 = hw[2 * j], w1 = hw[2 * j + 1];
          row[2 * (j - c0)] = (guint8) ((w0 * t[0] + w1 * t[2] + 32768) >> 16);
          row[2 * (j - c0) + 1] = (guint8) ((w0 * t[1] + w1 * t[3] + 32768) >> 16);
        }
      } else if (ch == 1) {                     /* Flaechenmittel */
        for (gint j = c0; j < c1; j++) {
          const guint16 *hw = hf->w + (gsize) j * ht;
          const guint16 *t = tmp + hf->start[j];
          guint32 sum = 32768;
          for (gint k = 0; k < hf->ntap[j]; k++) sum += (guint32) hw[k] * t[k];
          row[j - c0] = (guint8) (sum >> 16);
        }
      } else {
        for (gint j = c0; j < c1; j++) {
          const guint16 *hw = hf->w + (gsize) j * ht;
          const guint16 *t = tmp + 2 * hf->start[j];
          guint32 su = 32768, sv = 32768;
          for (gint k = 0; k < hf->ntap[j]; k++) { su += (guint32) hw[k] * t[2 * k]; sv += (guint32) hw[k] * t[2 * k + 1]; }
          row[2 * (j - c0)] = (guint8) (su >> 16);
          row[2 * (j - c0) + 1] = (guint8) (sv >> 16);
        }
      }
      out = row;
    }
    const gint len = (c1 - c0) * ch;
    if (a256 >= 256) memcpy(d, out, len);
    else for (gint x = 0; x < len; x++) d[x] = (guint8) ((out[x] * a256 + d[x] * (256 - a256) + 128) >> 8);
  }
}

/* Rechteck fuellen, auf das Band [by0, by1) beschnitten */
static void fill_rect_b(GstVideoFrame *f, gint x0, gint y0, gint x1, gint y1, guint8 Y, guint8 U, guint8 V, gint by0, gint by1) {
  const gint W = GST_VIDEO_FRAME_WIDTH(f), H = GST_VIDEO_FRAME_HEIGHT(f);
  x0 = MAX(0, x0) & ~1; y0 = MAX(by0, MAX(0, y0)) & ~1; x1 = MIN(W, x1) & ~1; y1 = MIN(by1, MIN(H, y1)) & ~1;
  if (x1 <= x0 || y1 <= y0) return;
  guint8 *py = GST_VIDEO_FRAME_PLANE_DATA(f, 0), *puv = GST_VIDEO_FRAME_PLANE_DATA(f, 1);
  const gint sy = GST_VIDEO_FRAME_PLANE_STRIDE(f, 0), suv = GST_VIDEO_FRAME_PLANE_STRIDE(f, 1);
  for (gint r = y0; r < y1; r++) memset(py + (gsize) r * sy + x0, Y, x1 - x0);
  if (U == V) { for (gint r = y0 / 2; r < y1 / 2; r++) memset(puv + (gsize) r * suv + x0, U, x1 - x0); }
  else for (gint r = y0 / 2; r < y1 / 2; r++) { guint8 *d = puv + (gsize) r * suv + x0; for (gint c = 0; c < x1 - x0; c += 2) { d[c] = U; d[c + 1] = V; } }
}

/* Ebene vorbereiten (im Haupt-Faden): Ausschnitt (Promille) bestimmen und Filter anlegen */
static void layer_prepare(const Frame *img, const LayerCfg *L, ScalePlan *p) {
  gint cx0 = (img->w * L->cl / 1000) & ~1, cx1 = (img->w - img->w * L->cr / 1000) & ~1;
  gint cy0 = (img->h * L->ct / 1000) & ~1, cy1 = (img->h - img->h * L->cb / 1000) & ~1;
  if (cx1 - cx0 < 2) { cx0 = 0; cx1 = img->w & ~1; }
  if (cy1 - cy0 < 2) { cy0 = 0; cy1 = img->h & ~1; }
  if (!(cx1 - cx0 >= 2 && cy1 - cy0 >= 2 && L->w >= 2 && L->h >= 2 && L->alpha > 0)) { p->ok = FALSE; return; }
  plan_make(p, cx1 - cx0, cy1 - cy0, L->w, L->h);      /* setzt bei neuem Filter den Plan zurueck -> ok erst danach */
  p->cx0 = cx0; p->cy0 = cy0; p->ok = TRUE;
}

/* Ein Kamerabild als Ebene zeichnen: skalierter Ausschnitt an x,y,w,h, nur Ausgabezeilen [by0, by1) */
static void layer_draw_band(GstVideoFrame *of, const Frame *img, const LayerCfg *L, const ScalePlan *p, Scratch *sc, gint by0, gint by1) {
  if (!p->ok) return;
  const gint W = GST_VIDEO_FRAME_WIDTH(of), H = GST_VIDEO_FRAME_HEIGHT(of);
  const gint r0 = MAX(MAX(0, -L->y), by0 - L->y), r1 = MIN(MIN(L->h, H - L->y), by1 - L->y);
  const gint c0 = MAX(0, -L->x), c1 = MIN(L->w, W - L->x);
  if (r1 <= r0 || c1 <= c0) return;
  const gint a256 = L->alpha * 256 / 100;
  guint8 *py = GST_VIDEO_FRAME_PLANE_DATA(of, 0), *puv = GST_VIDEO_FRAME_PLANE_DATA(of, 1);
  const gint sy = GST_VIDEO_FRAME_PLANE_STRIDE(of, 0), suv = GST_VIDEO_FRAME_PLANE_STRIDE(of, 1);
  const guint8 *srcy = img->data + (gsize) p->cy0 * img->w + p->cx0;
  const guint8 *srcuv = img->data + (gsize) img->w * img->h + (gsize) (p->cy0 / 2) * img->uvs + p->cx0;
  /* Zeiger auf die (evtl. negative) Rechteck-Ecke nur ueber Zeilen/Spalten im sichtbaren Bereich verwendet */
  scale_plane(srcy, img->w, 1, &p->hy, &p->vy, sc->tmp, sc->row, py + (gssize) L->y * sy + L->x, sy, r0, r1, c0, c1, a256);
  scale_plane(srcuv, img->uvs, 2, &p->hc, &p->vc, sc->tmp, sc->row, puv + (gssize) (L->y / 2) * suv + L->x, suv, r0 / 2, (r1 + 1) / 2, c0 / 2, (c1 + 1) / 2, a256);
}

/* ======================================================================================== irlpip2vsrc */

typedef struct {
  Store *st;                                   /* Speicher des gezeigten Bilds (eigene Referenz) */
  gint idx;                                    /* gezeigtes Fach (von diesem Prozess gepinnt) */
  Frame view;                                  /* Sicht auf das gezeigte Bild */
  gint64 cur_ref;
  guint epoch;
  gint late_run;                               /* aufeinanderfolgende "knapp zu spaet"-Wahlen */
  gboolean have, stale;
} VSel;

typedef struct {
  GstPushSrc parent;
  gint width, height, fps_n, fps_d, extra_latency_ms;
  gchar *shm;                                  /* gemeinsamer Speicher der Zubringer (leer = im Prozess) */
  gchar *paths[NSLOTS];
  GstClockTime cap_ns;                         /* Ring-Zeitfenster latency + max delay + 500 ms */
  GstVideoInfo info;
  ConfPoll cp;
  Config cfg;
  Ticker tk;
  VSel sel[NSLOTS];
  ScalePlan plan[NLAYERS];
  gint nthreads;                               /* Rechenfaeden fuer das Mischen (Baender), Eigenschaft */
  gint nactive;                                /* beim Start uebernommen */
  GThreadPool *pool;
  Scratch *scratch[MAX_THREADS];
  GMutex band_lock;
  GCond band_cond;
  gint band_pending;
  guint64 ticks, late_ticks, skipped_ticks;
  guint64 comp_us_sum, comp_n, comp_us_max;
  GstClockTime last_stats;
} IrlPip2VSrc;
typedef struct { GstPushSrcClass parent_class; } IrlPip2VSrcClass;
G_DEFINE_TYPE(IrlPip2VSrc, irl_pip2_vsrc, GST_TYPE_PUSH_SRC)

enum { PROP_CONFIG = 1, PROP_WIDTH, PROP_HEIGHT, PROP_FPS, PROP_EXTRA_LAT, PROP_STATS, PROP_THREADS, PROP_SRC_SHM };

static GstStaticPadTemplate vsrc_tmpl = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format=(string)NV12, width=(int)[2,8192], height=(int)[2,8192], framerate=(fraction)[1/1,120/1]"));

static GstCaps *vsrc_get_caps(GstBaseSrc *bs, GstCaps *filter) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) bs;
  GstCaps *c = gst_caps_new_simple("video/x-raw", "format", G_TYPE_STRING, "NV12", "width", G_TYPE_INT, self->width, "height", G_TYPE_INT, self->height,
                                   "framerate", GST_TYPE_FRACTION, self->fps_n, self->fps_d, "pixel-aspect-ratio", GST_TYPE_FRACTION, 1, 1,
                                   "interlace-mode", G_TYPE_STRING, "progressive", NULL);
  if (filter) { GstCaps *i = gst_caps_intersect_full(filter, c, GST_CAPS_INTERSECT_FIRST); gst_caps_unref(c); c = i; }
  return c;
}

static gboolean vsrc_set_caps(GstBaseSrc *bs, GstCaps *caps) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) bs;
  if (!gst_video_info_from_caps(&self->info, caps)) return FALSE;
  gst_base_src_set_blocksize(bs, GST_VIDEO_INFO_SIZE(&self->info));
  return TRUE;
}

static gboolean vsrc_decide_allocation(GstBaseSrc *bs, GstQuery *q) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) bs;
  GstBufferPool *pool = NULL;
  guint size, min, max;
  GstCaps *caps;
  gboolean update;
  gst_query_parse_allocation(q, &caps, NULL);
  if (gst_query_get_n_allocation_pools(q) > 0) {
    gst_query_parse_nth_allocation_pool(q, 0, &pool, &size, &min, &max);
    size = MAX(size, (guint) GST_VIDEO_INFO_SIZE(&self->info));
    update = TRUE;
  } else { size = GST_VIDEO_INFO_SIZE(&self->info); min = max = 0; update = FALSE; }
  min = MAX(min, 4u);
  if (max && max < min) max = min;
  if (!pool) pool = gst_video_buffer_pool_new();
  GstStructure *config = gst_buffer_pool_get_config(pool);
  gst_buffer_pool_config_set_params(config, caps, size, min, max);
  if (gst_query_find_allocation_meta(q, GST_VIDEO_META_API_TYPE, NULL)) gst_buffer_pool_config_add_option(config, GST_BUFFER_POOL_OPTION_VIDEO_META);
  if (!gst_buffer_pool_set_config(pool, config)) {
    config = gst_buffer_pool_get_config(pool);
    if (!gst_buffer_pool_config_validate_params(config, caps, size, min, max) || !gst_buffer_pool_set_config(pool, config)) GST_WARNING_OBJECT(self, "Pufferpool-Einstellung abgelehnt");
  }
  if (update) gst_query_set_nth_allocation_pool(q, 0, pool, size, min, max);
  else gst_query_add_allocation_pool(q, pool, size, min, max);
  gst_object_unref(pool);
  return GST_BASE_SRC_CLASS(irl_pip2_vsrc_parent_class)->decide_allocation(bs, q);
}

static GstClockTime vsrc_period(IrlPip2VSrc *self) { return gst_util_uint64_scale_int(GST_SECOND, self->fps_d, self->fps_n); }

/* Latenz: Live-Quelle, ein Bild wird am ENDE seines Intervalls ausgeliefert -> min = 1 Bilddauer (+ extra-latency).
 * max = unbegrenzt (GST_CLOCK_TIME_NONE): Bild- und Tonquelle haben verschiedene min (33,3 / 21,3 ms); meldeten beide
 * max = min, ergaebe sich im Muxer (Aggregator) max < min = "Impossible to configure latency".
 * Der Mischpuffer (latency=1500 der Steuerdatei) steckt bereits in der Bild-/Tonauswahl (Inhalt 1,5 s alt, PTS = jetzt)
 * und wird bewusst NICHT als min gemeldet: sonst warteten Muxer/appsink zusaetzlich 1,5 s und die Warteschlangen
 * (queue max-size-time 1 s) liefen voll. Wer ihn melden will: extra-latency=1500 an beiden Quellen. */
static gboolean vsrc_query(GstBaseSrc *bs, GstQuery *q) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) bs;
  if (GST_QUERY_TYPE(q) == GST_QUERY_LATENCY) {
    GstClockTime lat = vsrc_period(self) + (GstClockTime) self->extra_latency_ms * GST_MSECOND;
    gst_query_set_latency(q, TRUE, lat, GST_CLOCK_TIME_NONE);
    return TRUE;
  }
  return GST_BASE_SRC_CLASS(irl_pip2_vsrc_parent_class)->query(bs, q);
}

static void vsrc_publish_cfg(IrlPip2VSrc *self) {
  gint md = 0;
  for (gint i = 0; i < NSLOTS; i++) md = MAX(md, self->cfg.delay_ms[i]);
  self->cap_ns = (GstClockTime) (self->cfg.latency_ms + md + 500) * GST_MSECOND;   /* wird je Takt in die Speicher geschrieben */
}

/* Gezeigtes Bild eines Platzes loslassen (Pin + Speicher-Referenz) */
static void vsel_drop(VSel *v) {
  if (v->st) {
    if (v->have && store_lock(v->st)) { unpin_locked(v->st, v->idx); store_unlock(v->st); }
    store_unref(v->st);
  }
  memset(v, 0, sizeof *v);
}

static void vsrc_write_stats(IrlPip2VSrc *self, GstClockTime now) {
  if (!self->cp.path) return;
  GString *g = g_string_new(NULL);
  guint64 copy_sum = 0, copy_n = 0, copy_max = 0;
  g_string_append_printf(g, "{\"ticks\":%" G_GUINT64_FORMAT ",\"late_ticks\":%" G_GUINT64_FORMAT ",\"slots\":[", self->ticks, self->late_ticks);
  for (gint i = 0; i < NSLOTS; i++) {
    Store *st = store_get(self->paths[i], i, 0, 0, FALSE);
    Slot z;
    memset(&z, 0, sizeof z);
    Slot *s = &z;
    gint64 fill = 0, lat = -1;
    gboolean wr = FALSE;
    guint ring = 0;
    if (st && store_lock(st)) {
      s = st->s;
      if (s->cnt && s->have_target) fill = ((gint64) st->fh[s->ring[(s->rd + s->cnt - 1) % FMAX]].pts + s->off + s->bias - s->last_target) / (gint64) GST_MSECOND;
      if (s->lat_n) lat = s->lat_sum / (gint64) s->lat_n / (gint64) GST_MSECOND;
      wr = s->writer_token && now <= s->writer_beat + 2 * GST_SECOND;
      ring = s->cnt;
      copy_sum += s->copy_us_sum; copy_n += s->copy_n; copy_max = MAX(copy_max, s->copy_us_max);
      s->copy_us_sum = s->copy_n = s->copy_us_max = 0;
      s->lat_sum = 0; s->lat_n = 0;
    }
    gint64 age = s->v_last_arr ? (gint64) (now - s->v_last_arr) / (gint64) GST_MSECOND : -1;
    g_string_append_printf(g, "%s{\"w\":%d,\"h\":%d,\"in\":%" G_GUINT64_FORMAT ",\"shown\":%" G_GUINT64_FORMAT ",\"repeated\":%" G_GUINT64_FORMAT
                           ",\"skipped\":%" G_GUINT64_FORMAT ",\"late\":%" G_GUINT64_FORMAT ",\"fill_ms\":%" G_GINT64_FORMAT ",\"off_ms\":%" G_GINT64_FORMAT
                           ",\"drift_ppm\":%.1f,\"age_ms\":%" G_GINT64_FORMAT ",\"resets\":%" G_GUINT64_FORMAT ",\"a_in\":%" G_GUINT64_FORMAT ",\"a_underrun_ms\":%.0f"
                           ",\"phase_ms\":%.1f,\"ring\":%u,\"stale\":%s,\"eos\":%s,\"lat_ms\":%" G_GINT64_FORMAT ",\"writer\":%s,\"wgen\":%u,\"nospace\":%" G_GUINT64_FORMAT
                           ",\"foreign\":%" G_GUINT64_FORMAT "}",
                           i ? "," : "", s->w, s->h, s->in, s->shown, s->repeated, s->skipped, s->late, fill, s->map_valid ? s->off / (gint64) GST_MSECOND : 0,
                           s->drift * 1e6, age, s->resets, s->a_in, s->a_underrun_ms, s->bias / 1e6, ring, self->sel[i].stale ? "true" : "false",
                           (s->eos_v || s->eos_a) ? "true" : "false", lat, wr ? "true" : "false", s->writer_gen, s->nospace, s->foreign);
    if (s != &z) store_unlock(st);
    store_unref(st);
  }
  g_mutex_lock(&g_lock);
  gint aslot = g_audio_slot;
  gdouble sil = g_silence_ms;
  g_mutex_unlock(&g_lock);
  g_string_append_printf(g, "],\"audio\":{\"slot\":%d,\"silence_ms\":%.0f},\"latency_ms\":%d,\"reloads\":%" G_GUINT64_FORMAT
                         ",\"compose_us_avg\":%" G_GUINT64_FORMAT ",\"compose_us_max\":%" G_GUINT64_FORMAT ",\"copy_us_avg\":%" G_GUINT64_FORMAT ",\"copy_us_max\":%" G_GUINT64_FORMAT
                         ",\"shm\":%s}\n",
                         aslot, sil, self->cfg.latency_ms, self->cp.reloads, self->comp_n ? self->comp_us_sum / self->comp_n : 0, self->comp_us_max,
                         copy_n ? copy_sum / copy_n : 0, copy_max, self->paths[0] ? "true" : "false");
  self->comp_us_sum = self->comp_n = self->comp_us_max = 0;
  gchar *path = g_strconcat(self->cp.path, ".stats", NULL), *tmp = g_strconcat(path, ".tmp", NULL);
  if (g_file_set_contents(tmp, g->str, -1, NULL)) g_rename(tmp, path);
  g_free(path); g_free(tmp);
  g_string_free(g, TRUE);
}

/* Faelliges Bild eines Platzes zum Takt waehlen (unter der Sperre von st). target = Soll-ref, t = Takt-Uhrzeit. */
static void vsrc_select_locked(IrlPip2VSrc *self, gint si, Store *st, gint64 target, GstClockTime t, GstClockTime now) {
  Slot *s = st->s;
  VSel *v = &self->sel[si];
  s->cap_ns = self->cap_ns;
  evict_locked(st, now);
  s->last_target = target; s->have_target = TRUE;
  if (s->map_valid && s->cnt) {
    const gint64 off = s->off + s->bias, pin = s->pin_ns ? s->pin_ns : 33333333;
    const gboolean same = v->have && v->epoch == s->epoch;
    gint fidx = -1, cidx = -1;
    for (guint i = 0; i < s->cnt; i++) {
      const FHdr *f = &st->fh[s->ring[(s->rd + i) % FMAX]];
      if ((gint64) f->pts + off <= target) fidx = i;
      if (cidx < 0 && (!same || f->pts > v->view.pts)) cidx = i;
      if ((gint64) f->pts + off > target + STICK_NS && cidx >= 0) break;
    }
    gint choose = -1;
    /* Bevorzugt das naechste noch nicht gezeigte Bild, wenn es knapp (+-2 ms) um das Soll liegt: verhindert durch
     * ms-gerundete Zeitstempel ausgeloeste Doppel-/Fehlbilder bei gleichem Takt */
    if (same && cidx >= 0) {
      gint64 rc = (gint64) st->fh[s->ring[(s->rd + cidx) % FMAX]].pts + off;
      if (rc > target - pin && rc <= target + STICK_NS) { choose = cidx; v->late_run = 0; }
      else if (rc > target - pin - STICK_NS && rc <= target - pin && v->late_run < LATE_RUN_MAX) { choose = cidx; v->late_run++; }
      else v->late_run = 0;                     /* dauerhaft ein Bild hinterher -> einmal aufholen */
    }
    if (choose < 0) choose = fidx;
    if (choose >= 0) {
      const gint fi = s->ring[(s->rd + choose) % FMAX];
      const FHdr *f = &st->fh[fi];
      if (same && fi == v->idx && f->pts == v->view.pts) s->repeated++;
      else {
        if (same && cidx >= 0 && choose > cidx) s->skipped += choose - cidx;
        s->shown++;
        pin_locked(st, fi, now);                /* Fach bleibt unangetastet, solange es gezeigt wird */
        if (v->have) unpin_locked(st, v->idx);
        if (!v->st) v->st = store_ref(st);
        v->idx = fi; v->have = TRUE; v->epoch = s->epoch;
        v->view.w = f->w; v->view.h = f->h; v->view.uvs = f->uvs; v->view.size = f->size; v->view.pts = f->pts; v->view.data = FDATA(st, fi);
        s->lat_sum += (gint64) t - (gint64) f->arr; s->lat_n++;   /* Ankunft -> Ausgabe (prozessuebergreifend) */
      }
    } else if (v->have) s->repeated++;
  } else if (v->have) s->repeated++;
  if (v->have && v->epoch == s->epoch && s->map_valid) v->cur_ref = (gint64) v->view.pts + s->off + s->bias;
  v->stale = !v->have || target - v->cur_ref > (gint64) STALE_NS;
}

/* Ein Band [by0, by1) des Ausgabebilds zeichnen: Hintergrund, alle Ebenen in Reihenfolge, Rahmen.
 * Baender sind unabhaengig (jede Ausgabezeile gehoert genau einem Band) -> parallel ohne Sperren. */
typedef struct {
  IrlPip2VSrc *self;
  GstVideoFrame *of;
  Frame **img;
  gboolean covered;
  gint by0, by1;
  Scratch *sc;
} Band;

static void compose_band(Band *b) {
  IrlPip2VSrc *self = b->self;
  const Config *c = &self->cfg;
  GstVideoFrame *of = b->of;
  const gint W = GST_VIDEO_FRAME_WIDTH(of), H = GST_VIDEO_FRAME_HEIGHT(of);
  if (!b->covered) fill_rect_b(of, 0, 0, W, H, 16, 128, 128, b->by0, b->by1);
  for (gint l = 0; l < NLAYERS; l++) {
    const LayerCfg *L = &c->layer[l];
    if (!L->used) continue;
    Frame *f = b->img[L->slot];
    if (!f) { if (l == 0) fill_rect_b(of, L->x, L->y, L->x + L->w, L->y + L->h, 16, 128, 128, b->by0, b->by1); continue; }
    layer_draw_band(of, f, L, &self->plan[l], b->sc, b->by0, b->by1);
    if (L->border > 0) {
      const gint bw = (L->border + 1) & ~1, x = L->x, y = L->y, w = L->w, h = L->h;
      fill_rect_b(of, x - bw, y - bw, x + w + bw, y, L->bY, L->bU, L->bV, b->by0, b->by1);
      fill_rect_b(of, x - bw, y + h, x + w + bw, y + h + bw, L->bY, L->bU, L->bV, b->by0, b->by1);
      fill_rect_b(of, x - bw, y, x, y + h, L->bY, L->bU, L->bV, b->by0, b->by1);
      fill_rect_b(of, x + w, y, x + w + bw, y + h, L->bY, L->bU, L->bV, b->by0, b->by1);
    }
  }
}

static void band_worker(gpointer data, gpointer user) {
  Band *b = data;
  IrlPip2VSrc *self = b->self;
  compose_band(b);
  g_mutex_lock(&self->band_lock);
  if (--self->band_pending == 0) g_cond_signal(&self->band_cond);
  g_mutex_unlock(&self->band_lock);
}

static void vsrc_compose(IrlPip2VSrc *self, GstBuffer *buf, GstClockTime t) {
  const Config *c = &self->cfg;
  const gint64 half = (gint64) vsrc_period(self) / 2;
  GstClockTime now = sys_now();
  guint used = 0;
  for (gint l = 0; l < NLAYERS; l++) if (c->layer[l].used) used |= 1u << c->layer[l].slot;
  Frame *img[NSLOTS] = { 0 };
  for (gint si = 0; si < NSLOTS; si++) {
    VSel *v = &self->sel[si];
    Store *st = store_get(self->paths[si], si, 0, 0, FALSE);
    if (v->st && v->st != st) vsel_drop(v);    /* Zubringer hat einen neuen Speicher angelegt (oder Datei entfernt) */
    if (!st) { v->stale = TRUE; continue; }
    if (store_lock(st)) {
      if (!(used & (1u << si))) {              /* Platz nicht sichtbar: Ring trotzdem kurz halten */
        st->s->cap_ns = self->cap_ns;
        evict_locked(st, now);
      } else {
        gint64 target = (gint64) t - (gint64) c->latency_ms * GST_MSECOND - (gint64) c->delay_ms[si] * GST_MSECOND + half;
        vsrc_select_locked(self, si, st, target, t, now);
        if (si == 0) test_die(&g_die_rlock);
      }
      store_unlock(st);
    }                                          /* Sperre nicht erhalten: letzte Wahl bleibt */
    store_unref(st);
    if ((used & (1u << si)) && !v->stale && v->have) img[si] = &v->view;
  }

  for (gint l = 0; l < NLAYERS; l++) if (c->layer[l].used && img[c->layer[l].slot]) layer_prepare(img[c->layer[l].slot], &c->layer[l], &self->plan[l]);
  GstVideoFrame of;
  if (gst_video_frame_map(&of, &self->info, buf, GST_MAP_WRITE)) {
    const gint W = GST_VIDEO_FRAME_WIDTH(&of), H = GST_VIDEO_FRAME_HEIGHT(&of);
    const LayerCfg *l0 = &c->layer[0];
    gboolean covered = l0->used && l0->x <= 0 && l0->y <= 0 && l0->x + l0->w >= W && l0->y + l0->h >= H && (l0->alpha >= 100 || !img[l0->slot]);
    const gint nb = self->pool ? self->nactive : 1;
    const gint bh = ((H + nb - 1) / nb + 1) & ~1;
    Band bands[MAX_THREADS];
    for (gint i = 0; i < nb; i++) bands[i] = (Band) { self, &of, img, covered, MIN(i * bh, H), MIN((i + 1) * bh, H), self->scratch[i] };
    if (nb > 1) {
      g_mutex_lock(&self->band_lock);
      self->band_pending = nb - 1;
      g_mutex_unlock(&self->band_lock);
      for (gint i = 1; i < nb; i++) g_thread_pool_push(self->pool, &bands[i], NULL);
    }
    compose_band(&bands[0]);
    if (nb > 1) {
      g_mutex_lock(&self->band_lock);
      while (self->band_pending > 0) g_cond_wait(&self->band_cond, &self->band_lock);
      g_mutex_unlock(&self->band_lock);
    }
    gst_video_frame_unmap(&of);
  }
}

static GstFlowReturn vsrc_fill(GstPushSrc *ps, GstBuffer *buf) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) ps;
  GstElement *el = GST_ELEMENT(ps);
  if (config_poll(&self->cp, &self->cfg, self->width, self->height, FALSE)) {
    vsrc_publish_cfg(self);
    GST_INFO_OBJECT(self, "Steuerdatei neu geladen (latency=%d ms, audio=%d)", self->cfg.latency_ms, self->cfg.audio);
  }
  GstClockTime rt0 = ticker_start(el, &self->tk);
  const gint64 fn = self->fps_n, fd = self->fps_d;
  GstClockTime pts = rt0 + gst_util_uint64_scale(self->tk.n, GST_SECOND * fd, fn);
  GstClockTime next = rt0 + gst_util_uint64_scale(self->tk.n + 1, GST_SECOND * fd, fn);
  GstClockTime t_sys;
  GstClockTimeDiff late;
  GstFlowReturn r = ticker_wait(el, &self->tk, next, pts, &t_sys, &late);
  if (r != GST_FLOW_OK) return r;
  gboolean discont = self->tk.n == 0;
  if (late > 0) {
    self->late_ticks++;
    if (late > (GstClockTimeDiff) GST_SECOND) {      /* weit zurueck (z. B. angehalten): Bilder ueberspringen, Takt bleibt im Raster */
      guint64 skip = gst_util_uint64_scale(late, fn, GST_SECOND * fd);
      self->tk.n += skip; self->skipped_ticks += skip; discont = TRUE;
      pts = rt0 + gst_util_uint64_scale(self->tk.n, GST_SECOND * fd, fn);
      next = rt0 + gst_util_uint64_scale(self->tk.n + 1, GST_SECOND * fd, fn);
      t_sys += gst_util_uint64_scale(skip, GST_SECOND * fd, fn);
      GST_WARNING_OBJECT(self, "%" G_GUINT64_FORMAT " Takte uebersprungen (%" GST_STIME_FORMAT " zu spaet)", skip, GST_STIME_ARGS(late));
    }
  }
  gint64 t0 = g_get_monotonic_time();
  vsrc_compose(self, buf, t_sys);
  guint64 dt = g_get_monotonic_time() - t0;
  self->comp_us_sum += dt; self->comp_n++; self->comp_us_max = MAX(self->comp_us_max, dt);
  GST_BUFFER_PTS(buf) = pts; GST_BUFFER_DTS(buf) = GST_CLOCK_TIME_NONE;
  GST_BUFFER_DURATION(buf) = next - pts;
  GST_BUFFER_OFFSET(buf) = self->tk.n; GST_BUFFER_OFFSET_END(buf) = self->tk.n + 1;
  if (discont) GST_BUFFER_FLAG_SET(buf, GST_BUFFER_FLAG_DISCONT);
  self->tk.n++; self->ticks++;
  GstClockTime now = sys_now();
  if (now >= self->last_stats + GST_SECOND) { self->last_stats = now; vsrc_write_stats(self, now); }
  return GST_FLOW_OK;
}

static gboolean vsrc_start(GstBaseSrc *bs) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) bs;
  self->tk.started = FALSE; self->tk.flushing = FALSE;
  for (gint i = 0; i < NSLOTS; i++) { g_free(self->paths[i]); self->paths[i] = shm_path(self->shm, i); }
  config_default(&self->cfg, self->width, self->height);
  config_poll(&self->cp, &self->cfg, self->width, self->height, TRUE);
  vsrc_publish_cfg(self);
  self->nactive = CLAMP(self->nthreads, 1, MAX_THREADS);
  for (gint i = 0; i < self->nactive; i++) if (!self->scratch[i]) self->scratch[i] = g_new0(Scratch, 1);
  if (self->nactive > 1 && !self->pool) self->pool = g_thread_pool_new(band_worker, NULL, self->nactive - 1, TRUE, NULL);
  return TRUE;
}
static gboolean vsrc_stop(GstBaseSrc *bs) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) bs;
  for (gint i = 0; i < NSLOTS; i++) vsel_drop(&self->sel[i]);
  for (gint l = 0; l < NLAYERS; l++) plan_free(&self->plan[l]);
  if (self->pool) { g_thread_pool_free(self->pool, FALSE, TRUE); self->pool = NULL; }
  return TRUE;
}
static gboolean vsrc_unlock(GstBaseSrc *bs) { ticker_unlock(GST_ELEMENT(bs), &((IrlPip2VSrc *) bs)->tk); return TRUE; }
static gboolean vsrc_unlock_stop(GstBaseSrc *bs) { ticker_unlock_stop(GST_ELEMENT(bs), &((IrlPip2VSrc *) bs)->tk); return TRUE; }

static void vsrc_set_property(GObject *o, guint id, const GValue *v, GParamSpec *p) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) o;
  switch (id) {
    case PROP_CONFIG: g_free(self->cp.path); self->cp.path = g_value_dup_string(v); self->cp.mtime_ns = 0; break;
    case PROP_WIDTH: self->width = g_value_get_int(v) & ~1; break;
    case PROP_HEIGHT: self->height = g_value_get_int(v) & ~1; break;
    case PROP_FPS: self->fps_n = gst_value_get_fraction_numerator(v); self->fps_d = gst_value_get_fraction_denominator(v); break;
    case PROP_EXTRA_LAT: self->extra_latency_ms = g_value_get_int(v); break;
    case PROP_THREADS: self->nthreads = g_value_get_int(v); break;   /* wirkt beim naechsten Start */
    case PROP_SRC_SHM: g_free(self->shm); self->shm = g_value_dup_string(v); break;   /* wirkt beim naechsten Start */
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
  }
}
static void vsrc_get_property(GObject *o, guint id, GValue *v, GParamSpec *p) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) o;
  switch (id) {
    case PROP_CONFIG: g_value_set_string(v, self->cp.path); break;
    case PROP_WIDTH: g_value_set_int(v, self->width); break;
    case PROP_HEIGHT: g_value_set_int(v, self->height); break;
    case PROP_FPS: gst_value_set_fraction(v, self->fps_n, self->fps_d); break;
    case PROP_EXTRA_LAT: g_value_set_int(v, self->extra_latency_ms); break;
    case PROP_THREADS: g_value_set_int(v, self->nthreads); break;
    case PROP_SRC_SHM: g_value_set_string(v, self->shm); break;
    case PROP_STATS: {
      gchar *s = g_strdup_printf("ticks=%" G_GUINT64_FORMAT " late_ticks=%" G_GUINT64_FORMAT " skipped_ticks=%" G_GUINT64_FORMAT, self->ticks, self->late_ticks, self->skipped_ticks);
      g_value_take_string(v, s);
      break;
    }
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
  }
}
static void vsrc_finalize(GObject *o) {
  IrlPip2VSrc *self = (IrlPip2VSrc *) o;
  g_free(self->cp.path);
  g_free(self->shm);
  for (gint i = 0; i < NSLOTS; i++) g_free(self->paths[i]);
  for (gint i = 0; i < NSLOTS; i++) vsel_drop(&self->sel[i]);
  for (gint l = 0; l < NLAYERS; l++) plan_free(&self->plan[l]);
  for (gint i = 0; i < MAX_THREADS; i++) g_free(self->scratch[i]);
  g_mutex_clear(&self->band_lock); g_cond_clear(&self->band_cond);
  G_OBJECT_CLASS(irl_pip2_vsrc_parent_class)->finalize(o);
  inst_unref();
}

static void irl_pip2_vsrc_class_init(IrlPip2VSrcClass *k) {
  GObjectClass *go = G_OBJECT_CLASS(k);
  GstElementClass *el = GST_ELEMENT_CLASS(k);
  GstBaseSrcClass *bs = GST_BASE_SRC_CLASS(k);
  GstPushSrcClass *ps = GST_PUSH_SRC_CLASS(k);
  go->set_property = vsrc_set_property; go->get_property = vsrc_get_property; go->finalize = vsrc_finalize;
  g_object_class_install_property(go, PROP_CONFIG, g_param_spec_string("config-file", "config-file", "Steuerdatei (live, alle 200 ms geprueft); Statistik nach <datei>.stats", NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_WIDTH, g_param_spec_int("width", "width", "Ausgabebreite", 16, 8192, 1920, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_HEIGHT, g_param_spec_int("height", "height", "Ausgabehoehe", 16, 8192, 1080, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_FPS, gst_param_spec_fraction("framerate", "framerate", "Ausgabetakt", 1, 1, 120, 1, 30, 1, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_EXTRA_LAT, g_param_spec_int("extra-latency", "extra-latency", "Zusaetzlich gemeldete Latenz in ms (Standard 0: nur 1 Bilddauer)", 0, 5000, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_STATS, g_param_spec_string("stats", "stats", "Zaehler (nur lesen)", NULL, G_PARAM_READABLE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_SRC_SHM, g_param_spec_string("shm", "shm", "Gemeinsamer Speicher der Zubringer-Prozesse (wie shm der Senken); leer = Kameras in derselben Pipeline", NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_THREADS, g_param_spec_int("threads", "threads", "Rechenfaeden fuer das Mischen (Bild in Baendern)", 1, MAX_THREADS, 2, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  gst_element_class_add_static_pad_template(el, &vsrc_tmpl);
  gst_element_class_set_static_metadata(el, "IRL PiP2 Bild-Ausgang", "Source/Video", "Fest getakteter Bild-in-Bild-Mischer (NV12) aus den irlpip2vsink-Ringen", "IRL Streamer OS");
  bs->get_caps = vsrc_get_caps; bs->set_caps = vsrc_set_caps; bs->decide_allocation = vsrc_decide_allocation; bs->query = vsrc_query;
  bs->start = vsrc_start; bs->stop = vsrc_stop; bs->unlock = vsrc_unlock; bs->unlock_stop = vsrc_unlock_stop;
  ps->fill = vsrc_fill;
}
static void irl_pip2_vsrc_init(IrlPip2VSrc *self) {
  inst_ref();
  self->width = 1920; self->height = 1080; self->fps_n = 30; self->fps_d = 1; self->nthreads = 2;
  g_mutex_init(&self->band_lock); g_cond_init(&self->band_cond);
  config_default(&self->cfg, self->width, self->height);
  gst_base_src_set_live(GST_BASE_SRC(self), TRUE);
  gst_base_src_set_format(GST_BASE_SRC(self), GST_FORMAT_TIME);
  gst_base_src_set_do_timestamp(GST_BASE_SRC(self), FALSE);
}

/* ======================================================================================== irlpip2asrc */

typedef struct {
  GstPushSrc parent;
  gint extra_latency_ms;
  gchar *shm;                                  /* gemeinsamer Speicher der Zubringer (leer = im Prozess) */
  gchar *paths[NSLOTS];
  Store *cur_store;                            /* Speicher, zu dem cursor gehoert (eigene Referenz) */
  ConfPoll cp;
  Config cfg;
  Ticker tk;
  gint cur, pending;                           /* aktiver Ton-Platz, Ziel beim Umschalten */
  gint sw_state, sw_gain;                      /* 0 normal/einblenden, 1 ausblenden, 2 Stille bis Pufferende */
  gboolean jump_pending, cur_valid;
  gdouble cursor;                              /* Lesezeiger (Kamera-Sample-Index) */
  guint cur_epoch;
  gint since_inv;                              /* Samples seit letzter Luecke */
  gdouble last_l, last_r;                      /* Entknacksen bei unvorhersehbarem Abriss */
  gint decay;
  guint8 vflag[A_SPB * 2 + RAMP + 16];
  gint16 nxt[A_SPB * 2 + RAMP + 16];
} IrlPip2ASrc;
typedef struct { GstPushSrcClass parent_class; } IrlPip2ASrcClass;
G_DEFINE_TYPE(IrlPip2ASrc, irl_pip2_asrc, GST_TYPE_PUSH_SRC)

static GstStaticPadTemplate asrc_tmpl = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("audio/x-raw, format=(string)S16LE, rate=(int)48000, channels=(int)2, layout=(string)interleaved, channel-mask=(bitmask)0x3"));

static gboolean asrc_query(GstBaseSrc *bs, GstQuery *q) {
  IrlPip2ASrc *self = (IrlPip2ASrc *) bs;
  if (GST_QUERY_TYPE(q) == GST_QUERY_LATENCY) {
    GstClockTime lat = gst_util_uint64_scale_int(A_SPB, GST_SECOND, A_RATE) + (GstClockTime) self->extra_latency_ms * GST_MSECOND;
    gst_query_set_latency(q, TRUE, lat, GST_CLOCK_TIME_NONE);
    return TRUE;
  }
  return GST_BASE_SRC_CLASS(irl_pip2_asrc_parent_class)->query(bs, q);
}

static inline gboolean aval(const Store *st, gint64 i) {
  const Slot *s = st->s;
  return s->a_have && i < s->a_end && i >= s->a_end - A_RING && i >= 0 && st->a_valid[i % A_RING];
}

/* Fehlende Samples im Fenster jedes Platzes zaehlen (Statistik a_underrun_ms) */
static void asrc_underrun_stats(IrlPip2ASrc *self, GstClockTime t) {
  for (gint si = 0; si < NSLOTS; si++) {
    Store *st = store_get(self->paths[si], si, 0, 0, FALSE);
    if (st && store_lock(st)) {
      Slot *s = st->s;
      if (s->a_in && s->map_valid) {
        gint64 i0 = (gint64) floor(((gdouble) t - self->cfg.latency_ms * 1e6 - self->cfg.delay_ms[si] * 1e6 - (gdouble) (s->off + s->bias)) * A_RATE / 1e9);
        gint miss = 0;
        for (gint i = 0; i < A_SPB; i++) if (!aval(st, i0 + i)) miss++;
        s->a_underrun_ms += miss * 1000.0 / A_RATE;
      }
      store_unlock(st);
    }
    store_unref(st);
  }
}

static GstFlowReturn asrc_fill(GstPushSrc *ps, GstBuffer *buf) {
  IrlPip2ASrc *self = (IrlPip2ASrc *) ps;
  GstElement *el = GST_ELEMENT(ps);
  config_poll(&self->cp, &self->cfg, 1920, 1080, FALSE);
  GstClockTime rt0 = ticker_start(el, &self->tk);
  GstClockTime pts = rt0 + gst_util_uint64_scale_int(self->tk.n, GST_SECOND, A_RATE);
  GstClockTime end = rt0 + gst_util_uint64_scale_int(self->tk.n + A_SPB, GST_SECOND, A_RATE);
  GstClockTime t;
  GstClockTimeDiff late;
  GstFlowReturn r = ticker_wait(el, &self->tk, end, pts, &t, &late);
  if (r != GST_FLOW_OK) return r;
  gboolean discont = self->tk.n == 0;
  if (late > (GstClockTimeDiff) GST_SECOND) {        /* weit zurueck: Raster beibehalten, ueberspringen */
    guint64 skip = gst_util_uint64_scale(late, A_RATE, GST_SECOND) / A_SPB * A_SPB;
    self->tk.n += skip; discont = TRUE; t += gst_util_uint64_scale_int(skip, GST_SECOND, A_RATE);
    pts = rt0 + gst_util_uint64_scale_int(self->tk.n, GST_SECOND, A_RATE);
    end = rt0 + gst_util_uint64_scale_int(self->tk.n + A_SPB, GST_SECOND, A_RATE);
  }
  gint want = self->cfg.audio;
  if (want != self->cur && self->sw_state == 0) { self->sw_state = 1; self->pending = want; self->jump_pending = FALSE; }

  GstMapInfo mi;
  if (!gst_buffer_map(buf, &mi, GST_MAP_WRITE)) return GST_FLOW_ERROR;
  gint16 *out = (gint16 *) mi.data;
  gint silent = 0;
  asrc_underrun_stats(self, t);
  Store *st = self->cur >= 0 ? store_get(self->paths[self->cur], self->cur, 0, 0, FALSE) : NULL;
  if (st != self->cur_store) {                 /* anderer/neuer Speicher: alte Leseposition sinnlos */
    store_unref(self->cur_store);
    self->cur_store = store_ref(st);
    self->cur_valid = FALSE;
  }
  const gboolean locked = st && store_lock(st);
  Slot *s = locked ? st->s : NULL;
  gboolean have = s && s->map_valid && s->a_have;
  gdouble step = 1.0;
  if (have) {
    gdouble desired = ((gdouble) t - self->cfg.latency_ms * 1e6 - self->cfg.delay_ms[self->cur] * 1e6 - (gdouble) (s->off + s->bias)) * A_RATE / 1e9;
    if (self->cur_valid && s->epoch != self->cur_epoch) self->cur_valid = FALSE;   /* Kamera neu: alte Position sinnlos */
    if (!self->cur_valid) { self->cursor = floor(desired); self->cur_valid = TRUE; self->cur_epoch = s->epoch; self->since_inv = 0; }
    gdouble err = desired - self->cursor;
    if (fabs(err) > A_RESYNC) { if (self->sw_state == 0) { self->sw_state = 1; self->pending = self->cur; self->jump_pending = TRUE; } }
    else if (self->sw_state == 0) step = (A_SPB + CLAMP(err, -A_CORR_MAX, A_CORR_MAX)) / (gdouble) A_SPB;
  } else self->cur_valid = FALSE;
  /* Gueltigkeit + Abstand zur naechsten Luecke (Vorausschau 25 ms) fuer weiches Aus-/Einblenden an Luecken */
  const gint64 base = (gint64) floor(self->cursor);
  const gint span = (gint) ceil(A_SPB * step) + 2 + RAMP;
  if (have) {
    for (gint k = 0; k < span; k++) self->vflag[k] = aval(st, base + k) && aval(st, base + k + 1);
    gint d = RAMP;
    for (gint k = span - 1; k >= 0; k--) { d = self->vflag[k] ? MIN(d + 1, RAMP) : 0; self->nxt[k] = d; }
  }
  for (gint i = 0; i < A_SPB; i++) {
    gdouble l = 0, rr = 0, env = 0;
    if (have && self->sw_state != 2) {
      gdouble pos = self->cursor + i * step;
      gint64 ip = (gint64) floor(pos);
      gint k = (gint) (ip - base);
      if (k >= 0 && k < span && self->vflag[k]) {
        gdouble fr = pos - ip;
        const gint16 *a = st->a_data + (ip % A_RING) * A_CH, *b = st->a_data + ((ip + 1) % A_RING) * A_CH;
        l = a[0] + (b[0] - a[0]) * fr; rr = a[1] + (b[1] - a[1]) * fr;
        env = MIN(MIN(self->since_inv, self->nxt[k]), RAMP) / (gdouble) RAMP;
        self->since_inv++;
      } else self->since_inv = 0;
    }
    gdouble g = env * self->sw_gain / (gdouble) RAMP;
    gdouble ol = l * g, orr = rr * g;
    if (g == 0) {
      /* unvorhergesehener Abriss: letzten Wert linear auslaufen lassen (kein Knacken) */
      if (self->decay > 0) { ol = self->last_l * self->decay / RAMP; orr = self->last_r * self->decay / RAMP; self->decay--; }
      else silent++;
    } else { self->last_l = ol; self->last_r = orr; self->decay = (gint) (RAMP * g); }
    out[2 * i] = (gint16) CLAMP(lrint(ol), -32768, 32767);
    out[2 * i + 1] = (gint16) CLAMP(lrint(orr), -32768, 32767);
    if (self->sw_state == 1) {
      if (self->sw_gain > 0) self->sw_gain--;
      if (self->sw_gain == 0) {
        self->cur = self->pending; self->cur_valid = FALSE; self->sw_state = 2; self->jump_pending = FALSE;
        self->decay = 0;
      }
    } else if (self->sw_state == 0 && self->sw_gain < RAMP) self->sw_gain++;
  }
  if (self->sw_state == 2) self->sw_state = 0;
  if (self->cur_valid) self->cursor += A_SPB * step;
  if (locked) store_unlock(st);
  store_unref(st);
  g_mutex_lock(&g_lock);
  g_silence_ms += silent * 1000.0 / A_RATE;
  g_audio_slot = self->cur;
  g_mutex_unlock(&g_lock);
  gst_buffer_unmap(buf, &mi);
  gst_buffer_set_size(buf, A_SPB * A_CH * 2);
  GST_BUFFER_PTS(buf) = pts; GST_BUFFER_DTS(buf) = GST_CLOCK_TIME_NONE; GST_BUFFER_DURATION(buf) = end - pts;
  GST_BUFFER_OFFSET(buf) = self->tk.n; GST_BUFFER_OFFSET_END(buf) = self->tk.n + A_SPB;
  if (discont) GST_BUFFER_FLAG_SET(buf, GST_BUFFER_FLAG_DISCONT);
  self->tk.n += A_SPB;
  return GST_FLOW_OK;
}

static gboolean asrc_start(GstBaseSrc *bs) {
  IrlPip2ASrc *self = (IrlPip2ASrc *) bs;
  self->tk.started = FALSE; self->tk.flushing = FALSE;
  for (gint i = 0; i < NSLOTS; i++) { g_free(self->paths[i]); self->paths[i] = shm_path(self->shm, i); }
  config_default(&self->cfg, 1920, 1080);
  config_poll(&self->cp, &self->cfg, 1920, 1080, TRUE);
  self->cur = self->cfg.audio; self->sw_state = 0; self->sw_gain = RAMP; self->cur_valid = FALSE; self->since_inv = 0; self->decay = 0;
  return TRUE;
}
static gboolean asrc_stop(GstBaseSrc *bs) {
  IrlPip2ASrc *self = (IrlPip2ASrc *) bs;
  store_unref(self->cur_store); self->cur_store = NULL; self->cur_valid = FALSE;
  return TRUE;
}
static gboolean asrc_unlock(GstBaseSrc *bs) { ticker_unlock(GST_ELEMENT(bs), &((IrlPip2ASrc *) bs)->tk); return TRUE; }
static gboolean asrc_unlock_stop(GstBaseSrc *bs) { ticker_unlock_stop(GST_ELEMENT(bs), &((IrlPip2ASrc *) bs)->tk); return TRUE; }

static void asrc_set_property(GObject *o, guint id, const GValue *v, GParamSpec *p) {
  IrlPip2ASrc *self = (IrlPip2ASrc *) o;
  if (id == PROP_CONFIG) { g_free(self->cp.path); self->cp.path = g_value_dup_string(v); self->cp.mtime_ns = 0; }
  else if (id == PROP_EXTRA_LAT) self->extra_latency_ms = g_value_get_int(v);
  else if (id == PROP_SRC_SHM) { g_free(self->shm); self->shm = g_value_dup_string(v); }
  else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void asrc_get_property(GObject *o, guint id, GValue *v, GParamSpec *p) {
  IrlPip2ASrc *self = (IrlPip2ASrc *) o;
  if (id == PROP_CONFIG) g_value_set_string(v, self->cp.path);
  else if (id == PROP_EXTRA_LAT) g_value_set_int(v, self->extra_latency_ms);
  else if (id == PROP_SRC_SHM) g_value_set_string(v, self->shm);
  else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void asrc_finalize(GObject *o) {
  IrlPip2ASrc *self = (IrlPip2ASrc *) o;
  g_free(self->cp.path); g_free(self->shm);
  for (gint i = 0; i < NSLOTS; i++) g_free(self->paths[i]);
  store_unref(self->cur_store);
  G_OBJECT_CLASS(irl_pip2_asrc_parent_class)->finalize(o);
  inst_unref();
}

static void irl_pip2_asrc_class_init(IrlPip2ASrcClass *k) {
  GObjectClass *go = G_OBJECT_CLASS(k);
  GstElementClass *el = GST_ELEMENT_CLASS(k);
  GstBaseSrcClass *bs = GST_BASE_SRC_CLASS(k);
  GstPushSrcClass *ps = GST_PUSH_SRC_CLASS(k);
  go->set_property = asrc_set_property; go->get_property = asrc_get_property; go->finalize = asrc_finalize;
  g_object_class_install_property(go, PROP_CONFIG, g_param_spec_string("config-file", "config-file", "Steuerdatei (audio=, delay, latency)", NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_EXTRA_LAT, g_param_spec_int("extra-latency", "extra-latency", "Zusaetzlich gemeldete Latenz in ms (Standard 0: nur 1 Pufferdauer)", 0, 5000, 0, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  g_object_class_install_property(go, PROP_SRC_SHM, g_param_spec_string("shm", "shm", "Gemeinsamer Speicher der Zubringer-Prozesse (wie shm der Senken); leer = Kameras in derselben Pipeline", NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  gst_element_class_add_static_pad_template(el, &asrc_tmpl);
  gst_element_class_set_static_metadata(el, "IRL PiP2 Ton-Ausgang", "Source/Audio", "Lueckenloser Ton des gewaehlten Kamera-Platzes (Stille, Rampen)", "IRL Streamer OS");
  bs->query = asrc_query; bs->start = asrc_start; bs->stop = asrc_stop; bs->unlock = asrc_unlock; bs->unlock_stop = asrc_unlock_stop;
  ps->fill = asrc_fill;
}
static void irl_pip2_asrc_init(IrlPip2ASrc *self) {
  inst_ref();
  self->cur = 0; self->sw_gain = RAMP;
  gst_base_src_set_live(GST_BASE_SRC(self), TRUE);
  gst_base_src_set_format(GST_BASE_SRC(self), GST_FORMAT_TIME);
  gst_base_src_set_do_timestamp(GST_BASE_SRC(self), FALSE);
  gst_base_src_set_blocksize(GST_BASE_SRC(self), A_SPB * A_CH * 2);
  GstCaps *c = gst_static_pad_template_get_caps(&asrc_tmpl);
  gst_pad_use_fixed_caps(GST_BASE_SRC_PAD(self));
  gst_caps_unref(c);
}

/* ======================================================================================== Plugin */

static gboolean plugin_init(GstPlugin *plugin) {
  GST_DEBUG_CATEGORY_INIT(irlpip2_debug, "irlpip2", 0, "IRL PiP 2");
  if (!g_sysclock) {
    g_sysclock = gst_system_clock_obtain();                        /* bleibt fuer die Prozesslaufzeit */
    test_die_init();
    long pg = sysconf(_SC_PAGESIZE);
    g_page = pg > 0 ? (gsize) pg : 4096;
    while (!g_token) g_token = ((guint64) g_random_int() << 32 | g_random_int()) ^ (guint64) getpid();
    /* Zeitbasis pruefen: GstSystemClock (Standard MONOTONIC) gegen CLOCK_MONOTONIC */
    GstClockTime a = gst_clock_get_time(g_sysclock), m = mono_now(), b = gst_clock_get_time(g_sysclock);
    g_sys2mono = (gint64) m - (gint64) (a / 2 + b / 2);
    if (ABS(g_sys2mono) < (gint64) GST_MSECOND) g_sys2mono = 0;
    else GST_WARNING("GstSystemClock weicht von CLOCK_MONOTONIC um %" G_GINT64_FORMAT " ns ab - wird herausgerechnet", g_sys2mono);
  }
  return gst_element_register(plugin, "irlpip2vsink", GST_RANK_NONE, irl_pip2_vsink_get_type()) &&
         gst_element_register(plugin, "irlpip2asink", GST_RANK_NONE, irl_pip2_asink_get_type()) &&
         gst_element_register(plugin, "irlpip2vsrc", GST_RANK_NONE, irl_pip2_vsrc_get_type()) &&
         gst_element_register(plugin, "irlpip2asrc", GST_RANK_NONE, irl_pip2_asrc_get_type());
}

#ifndef PACKAGE
#define PACKAGE "irlpip2"
#endif
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, irlpip2, "IRL Streamer OS Bild-in-Bild 2.0", plugin_init, "2.0.0", "MIT/X11", "irl-pip2", "https://irlstreameros.de")
