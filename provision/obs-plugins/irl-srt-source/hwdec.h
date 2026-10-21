/*
 * Hardware-Dekodierung fuer die IRL SRT Quelle - herstellerneutral.
 *
 *   Nvidia  -> CUDA/NVDEC  (proprietaerer Treiber, /dev/nvidia0)
 *   Intel   -> VAAPI       (intel-media-va-driver, /dev/dri/renderD*)
 *   AMD     -> VAAPI       (mesa-va-drivers,       /dev/dri/renderD*)
 *   sonst / Fehler -> Software (wie bisher)
 *
 * Bewusst nur die allgemeine ffmpeg-hwdevice-API (keine herstellerspezifischen
 * Bibliotheken zur Bauzeit): das Plugin baut und laeuft auf jedem Geraet,
 * die passende Beschleunigung wird erst zur Laufzeit gesucht.
 *
 * Unterstuetzt die GPU das Format nicht (z.B. HEVC 10 Bit auf alter Karte),
 * faellt ffmpeg beim Oeffnen selbst auf Software zurueck (get_format bekommt
 * dann nur noch Software-Formate angeboten); das erkennt hwdec_is_hw_frame().
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <libavcodec/avcodec.h>

enum hwdec_mode {
	HWDEC_AUTO = 0, /* Nvidia -> Intel/AMD -> Software */
	HWDEC_OFF = 1,  /* nur Software */
};

/* Versucht vor avcodec_open2() eine Hardware-Beschleunigung an ctx zu haengen.
 * true = angehaengt (desc beschreibt sie, z.B. "VAAPI Intel renderD128"),
 * false = Software (desc = "Software"). ctx->opaque wird belegt. */
bool hwdec_attach(AVCodecContext *ctx, const AVCodec *codec, enum hwdec_mode mode, char *desc, size_t desc_len);

/* Liegt das Bild im Grafikspeicher (Hardware-Format)? */
bool hwdec_is_hw_frame(const AVFrame *f);

/* Hardware-Bild in normalen Speicher (NV12/P010) holen; fr wird ersetzt.
 * tmp ist ein wiederverwendbarer Hilfsrahmen. 0 = ok, <0 = Fehler. */
int hwdec_download(AVFrame *fr, AVFrame *tmp);
