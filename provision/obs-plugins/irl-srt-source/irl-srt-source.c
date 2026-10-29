/*
 * IRL SRT Source - OBS-Quelle fuer IRL-Livestreams (IRL Streamer OS)
 *
 * Problem der Standard-Medienquelle (ffmpeg_source) bei IRL-Streams:
 *  - Kommen nach Paketverlust Luecken/Zeitspruenge in den Audio-Zeitstempeln an,
 *    puffert OBS Audio bis zum Maximum (~1 s) und startet dann den Ton der
 *    Quelle neu ("audio is lagging ... Restarting source audio") -> "Roboterton".
 *  - Abhilfe war bisher das Neuladen der Quelle -> schwarzes Bild bis zum
 *    naechsten Keyframe.
 *
 * Diese Quelle:
 *  - liest und dekodiert in einem eigenen Thread; bei Abbruch stiller
 *    Neuaufbau, das letzte Bild bleibt stehen (nie schwarz).
 *  - spielt nach einer EIGENEN, nie anhaltenden Wiedergabeuhr ab, die mit
 *    festem Abstand (Polster) hinter den neuesten empfangenen Daten herlaeuft.
 *    Ton geht lueckenlos mit Systemzeit-Zeitstempeln an OBS; fehlen Daten,
 *    kommt Stille (kein Zeitsprung -> kein Roboterton), das Bild bleibt fuer
 *    genau die Dauer der Luecke stehen.
 *  - haelt den Abstand per Tempo-Anpassung von max. +-0,5 % (unhoerbar);
 *    groessere Abweichungen (Datenschwall, Ausfall) werden durch Neu-Ausrichten
 *    der Uhr korrigiert. Bild und Ton haengen an derselben Uhr -> synchron.
 *
 * Statistik: alle 60 s ins OBS-Log ("[IRL SRT Source ...]") und als JSON nach
 * /tmp/irl-srt-source-<Name>.json. IRL_SRT_DEBUG=1 loggt Einzelereignisse.
 *
 * Nur stabile libobs-C-API (obs_source_info, obs_source_output_video/_audio),
 * damit die Quelle ueber OBS-Updates hinweg funktioniert.
 */
#include <obs-module.h>
#include <util/threading.h>
#include <util/platform.h>
#include <util/bmem.h>

#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libavutil/imgutils.h>
#include <libswresample/swresample.h>
#include <libswscale/swscale.h>

#include "hwdec.h"

#include <math.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("irl-srt-source", "de-DE")

#define SRC_LOG(lvl, s, fmt, ...) blog(lvl, "[IRL SRT Source '%s'] " fmt, obs_source_get_name((s)->source), ##__VA_ARGS__)
static bool g_debug;
#define DBG(s, fmt, ...)                                                   \
	do {                                                               \
		if (g_debug)                                               \
			SRC_LOG(LOG_INFO, s, "DBG " fmt, ##__VA_ARGS__); \
	} while (0)

#define OUT_RATE 48000
#define OUT_CH 2
#define RING_SECONDS 12
#define RING_CAP (OUT_RATE * RING_SECONDS)
#define TICK_NS 10000000ULL /* 10 ms Ausgabetakt */
#define MAX_RATE_DEV 0.005  /* max. +-0,5 % Tempo */
#define VQ_MAX 360          /* max. Videobilder in der Warteschlange (~12 s bei 30 fps) */
#define NO_PTS INT64_MIN

/* ------------------------------------------------------------------ */
/* Audio-Ringpuffer (float, planar, Stereo, 48 kHz), Medienzeit-indexiert */
/* ------------------------------------------------------------------ */
struct aring {
	float *buf[OUT_CH];
	size_t r, w, n;
	int64_t head_pts_us; /* Medienzeit des Samples an Position r */
};

static void aring_init(struct aring *a)
{
	for (int c = 0; c < OUT_CH; c++)
		a->buf[c] = bzalloc(sizeof(float) * RING_CAP);
	a->r = a->w = a->n = 0;
	a->head_pts_us = NO_PTS;
}

static void aring_free(struct aring *a)
{
	for (int c = 0; c < OUT_CH; c++)
		bfree(a->buf[c]);
}

static void aring_clear(struct aring *a)
{
	a->r = a->w = a->n = 0;
	a->head_pts_us = NO_PTS;
}

static inline int64_t aring_end_pts(const struct aring *a)
{
	return a->head_pts_us == NO_PTS ? NO_PTS : a->head_pts_us + (int64_t)a->n * 1000000 / OUT_RATE;
}

static void aring_consume(struct aring *a, size_t frames)
{
	if (frames > a->n)
		frames = a->n;
	a->r = (a->r + frames) % RING_CAP;
	a->n -= frames;
	if (a->head_pts_us != NO_PTS)
		a->head_pts_us += (int64_t)frames * 1000000 / OUT_RATE;
}

static void aring_push(struct aring *a, float **data, size_t frames)
{
	if (frames > RING_CAP)
		frames = RING_CAP;
	if (a->n + frames > RING_CAP)
		aring_consume(a, a->n + frames - RING_CAP);
	for (size_t i = 0; i < frames; i++) {
		for (int c = 0; c < OUT_CH; c++)
			a->buf[c][a->w] = data ? data[c][i] : 0.0f;
		a->w = (a->w + 1) % RING_CAP;
	}
	a->n += frames;
}

static inline float aring_at(const struct aring *a, int c, size_t off)
{
	return a->buf[c][(a->r + off) % RING_CAP];
}

/* ------------------------------------------------------------------ */
/* Video-Warteschlange                                                 */
/* ------------------------------------------------------------------ */
struct vq {
	AVFrame *f[VQ_MAX];
	int64_t pts_us[VQ_MAX];
	int head, count;
};

static void vq_pop_free(struct vq *q)
{
	av_frame_free(&q->f[q->head]);
	q->head = (q->head + 1) % VQ_MAX;
	q->count--;
}

static void vq_clear(struct vq *q)
{
	while (q->count)
		vq_pop_free(q);
	q->head = 0;
}

static inline int64_t vq_newest(const struct vq *q)
{
	return q->count ? q->pts_us[(q->head + q->count - 1) % VQ_MAX] : NO_PTS;
}

/* ------------------------------------------------------------------ */
/* Quelle                                                              */
/* ------------------------------------------------------------------ */
struct irl_src {
	obs_source_t *source;

	/* Einstellungen */
	char *url;
	int buffer_ms;
	int reconnect_ms;
	long long reconnect_nonce;
	int hw_mode;            /* enum hwdec_mode */
	volatile bool hw_failed; /* Hardware-Dekodierung fuer diese Quelle aufgegeben */
	int hw_failed_codec;     /* ... nur fuer diesen Codec (AV_CODEC_ID_NONE = fuer alle) */
	char decoder[96];        /* aktuelle Bild-Dekodierung, fuer Log/Statistik */

	/* Threads */
	pthread_t read_thread, out_thread;
	bool threads_running;
	volatile bool stop;
	volatile bool reopen;

	pthread_mutex_t mtx; /* schuetzt alles darunter */
	struct aring ring;
	struct vq vq;
	bool has_audio, has_video;
	int64_t newest_video_us; /* neueste empfangene Bild-Medienzeit */
	uint64_t last_data_ns;   /* Zeitpunkt der letzten empfangenen Daten */
	bool resync;             /* neue Zeitbasis -> Uhr neu ausrichten */

	/* Wiedergabeuhr (nur out_thread schreibt) */
	bool clock_valid;
	int64_t clock_us; /* Medienzeit, die gerade ausgegeben wird */
	double frac;
	double rate;
	int64_t lag_min_us; /* tiefster Abstand im Fenster */
	uint64_t lag_win_start_ns;
	uint64_t behind_since_ns;

	/* Statistik */
	uint64_t st_silence_ms, st_rebuffers, st_reconnects, st_kf_waits, st_gaps, st_vdrops, st_jumps, st_vout;
	int st_lag_min_ms, st_lag_max_ms;
	uint64_t st_last_log_ns, st_last_json_ns;
	int dbg_lag_ms, dbg_vq;
	char *status;
	bool logged_disconnect;
	uint64_t st_static, last_sum, st_corrupt, st_dec_err;
};

static int interrupt_cb(void *opaque)
{
	struct irl_src *s = opaque;
	return s->stop || s->reopen;
}

static void set_status(struct irl_src *s, const char *st)
{
	pthread_mutex_lock(&s->mtx);
	if (!s->status || strcmp(s->status, st) != 0) {
		bool was_disc = s->status && strncmp(s->status, "getrennt", 8) == 0;
		bool is_disc = strncmp(st, "getrennt", 8) == 0;
		bool log = true;
		if (strcmp(st, "verbinde") == 0 && was_disc)
			log = false; /* Neuversuch im Sekundentakt nicht jedes Mal loggen */
		if (is_disc && s->logged_disconnect)
			log = false;
		if (log)
			SRC_LOG(LOG_INFO, s, "Status: %s", st);
		if (is_disc)
			s->logged_disconnect = true;
		if (strcmp(st, "verbunden") == 0)
			s->logged_disconnect = false;
		bfree(s->status);
		s->status = bstrdup(st);
	}
	pthread_mutex_unlock(&s->mtx);
}

/* ------------------------------------------------------------------ */
/* Lese-/Dekodier-Thread                                               */
/* ------------------------------------------------------------------ */
static enum video_format av_to_obs_fmt(int f)
{
	switch (f) {
	case AV_PIX_FMT_YUV420P:
	case AV_PIX_FMT_YUVJ420P:
		return VIDEO_FORMAT_I420;
	case AV_PIX_FMT_NV12:
		return VIDEO_FORMAT_NV12;
	case AV_PIX_FMT_YUV420P10LE:
		return VIDEO_FORMAT_I010;
	case AV_PIX_FMT_P010LE:
		return VIDEO_FORMAT_P010;
	default:
		return VIDEO_FORMAT_NONE;
	}
}

/* Review 29.10.2026 (B6): passt der Resampler noch zum Ton-Frame? Wechselt der AAC-Strom
 * mitten in der Sitzung z.B. von Stereo auf Mono, las swr_convert sonst extended_data[1]
 * (NULL) -> Absturz von OBS waehrend der Sendung. */
static bool swr_matches(SwrContext *swr, const AVFrame *fr)
{
	AVChannelLayout in_l = {0};
	int64_t rate = 0;
	enum AVSampleFormat fmt = AV_SAMPLE_FMT_NONE;
	if (av_opt_get_chlayout(swr, "in_chlayout", 0, &in_l) < 0)
		return false;
	bool same = av_channel_layout_compare(&in_l, &fr->ch_layout) == 0;
	av_channel_layout_uninit(&in_l);
	if (av_opt_get_int(swr, "in_sample_rate", 0, &rate) < 0 || rate != fr->sample_rate)
		return false;
	if (av_opt_get_sample_fmt(swr, "in_sample_fmt", 0, &fmt) < 0 || (int)fmt != fr->format)
		return false;
	return same;
}

static void push_audio(struct irl_src *s, AVFrame *fr, SwrContext **swr, AVStream *st)
{
	if (fr->ch_layout.nb_channels <= 0 || fr->sample_rate <= 0 || !fr->extended_data)
		return;
	if (*swr && !swr_matches(*swr, fr)) {
		DBG(s, "Ton-Format geaendert (%d Kanaele, %d Hz) - Resampler neu", fr->ch_layout.nb_channels,
		    fr->sample_rate);
		swr_free(swr);
	}
	if (!*swr) {
		AVChannelLayout out_l = AV_CHANNEL_LAYOUT_STEREO;
		if (swr_alloc_set_opts2(swr, &out_l, AV_SAMPLE_FMT_FLTP, OUT_RATE, &fr->ch_layout, fr->format,
					fr->sample_rate, 0, NULL) < 0 ||
		    swr_init(*swr) < 0) {
			swr_free(swr);
			return;
		}
	}
	int max_out = swr_get_out_samples(*swr, fr->nb_samples);
	if (max_out <= 0)
		return;
	float *out[OUT_CH];
	for (int c = 0; c < OUT_CH; c++)
		out[c] = bmalloc(sizeof(float) * (size_t)max_out);
	int got = swr_convert(*swr, (uint8_t **)out, max_out, (const uint8_t **)fr->extended_data, fr->nb_samples);

	int64_t ts = fr->best_effort_timestamp != AV_NOPTS_VALUE ? fr->best_effort_timestamp : fr->pts;
	if (got > 0 && ts != AV_NOPTS_VALUE) {
		int64_t pts_us = av_rescale_q(ts, st->time_base, AV_TIME_BASE_Q);
		pthread_mutex_lock(&s->mtx);
		struct aring *a = &s->ring;
		int64_t end = aring_end_pts(a);
		if (end == NO_PTS) {
			a->head_pts_us = pts_us;
		} else {
			int64_t diff = pts_us - end;
			if (diff < -200000 || diff > 3000000) {
				DBG(s, "Audio-Zeitbasis-Sprung %lld ms", (long long)(diff / 1000));
				aring_clear(a);
				a->head_pts_us = pts_us;
				s->resync = true;
			} else if (diff > 25000) {
				/* Luecke (verlorene Pakete) -> mit Stille fuellen, Zeitachse bleibt korrekt */
				aring_push(a, NULL, (size_t)(diff * OUT_RATE / 1000000));
				s->st_gaps++;
				DBG(s, "Audio-Luecke %lld ms mit Stille gefuellt", (long long)(diff / 1000));
			}
		}
		aring_push(a, out, (size_t)got);
		s->last_data_ns = os_gettime_ns();
		pthread_mutex_unlock(&s->mtx);
	}
	for (int c = 0; c < OUT_CH; c++)
		bfree(out[c]);
}

static void push_video(struct irl_src *s, AVFrame *fr, AVStream *st)
{
	int64_t ts = fr->best_effort_timestamp != AV_NOPTS_VALUE ? fr->best_effort_timestamp : fr->pts;
	if (ts == AV_NOPTS_VALUE)
		return;
	AVFrame *c = av_frame_clone(fr);
	if (!c)
		return;
	int64_t pts_us = av_rescale_q(ts, st->time_base, AV_TIME_BASE_Q);
	pthread_mutex_lock(&s->mtx);
	struct vq *q = &s->vq;
	int64_t newest = vq_newest(q);
	if (newest != NO_PTS && (pts_us < newest - 2000000 || pts_us > newest + 3000000)) {
		DBG(s, "Video-Zeitbasis-Sprung %lld ms", (long long)((pts_us - newest) / 1000));
		vq_clear(q);
		s->newest_video_us = NO_PTS;
		s->resync = true;
	}
	if (q->count == VQ_MAX) {
		vq_pop_free(q);
		s->st_vdrops++;
	}
	int idx = (q->head + q->count) % VQ_MAX;
	q->f[idx] = c;
	q->pts_us[idx] = pts_us;
	q->count++;
	if (s->newest_video_us == NO_PTS || pts_us > s->newest_video_us)
		s->newest_video_us = pts_us;
	s->last_data_ns = os_gettime_ns();
	pthread_mutex_unlock(&s->mtx);
}

static AVCodecContext *open_decoder_ex(AVStream *st, bool try_hw, enum hwdec_mode mode, bool *hw_on, char *desc,
				       size_t desc_len)
{
	*hw_on = false;
	const AVCodec *codec = avcodec_find_decoder(st->codecpar->codec_id);
	if (!codec)
		return NULL;
	AVCodecContext *ctx = avcodec_alloc_context3(codec);
	if (!ctx)
		return NULL;
	if (avcodec_parameters_to_context(ctx, st->codecpar) < 0) {
		avcodec_free_context(&ctx);
		return NULL;
	}
	ctx->pkt_timebase = st->time_base;
	/* Software: Frame+Slice-Threads - schnell genug auch auf schwacher/
	 * ausgelasteter CPU; die wenigen Bilder Zusatzlatenz deckt das Polster ab */
	ctx->thread_count = st->codecpar->codec_type == AVMEDIA_TYPE_VIDEO ? 4 : 1;
	ctx->thread_type = FF_THREAD_FRAME | FF_THREAD_SLICE;
	if (try_hw)
		*hw_on = hwdec_attach(ctx, codec, mode, desc, desc_len);
	if (avcodec_open2(ctx, codec, NULL) < 0) {
		avcodec_free_context(&ctx);
		if (*hw_on) /* Hardware liess sich nicht oeffnen -> Software */
			return open_decoder_ex(st, false, HWDEC_OFF, hw_on, desc, desc_len);
		return NULL;
	}
	return ctx;
}

static AVCodecContext *open_decoder(AVStream *st)
{
	bool hw;
	char desc[8];
	return open_decoder_ex(st, false, HWDEC_OFF, &hw, desc, sizeof(desc));
}

/* Paket-Warteschlange zwischen Lesen und Dekodieren: das Lesen vom Socket
 * darf nie auf den Decoder warten, sonst laeuft der SRT-Empfangspuffer voll
 * und der Server verwirft Daten fuer uns (sichtbar als Standbild durch die
 * Fehlerverschleierung des Decoders). */
#define PQ_MAX 4096
struct dec_ctx {
	struct irl_src *s;
	AVFormatContext *fmt;
	AVCodecContext *vdec, *adec;
	int vi, ai;
	int64_t vframe_dur;
	AVPacket *q[PQ_MAX];
	int head, count;
	bool done;
	bool overflow_wait_kf;
	bool hw;          /* Video-Decoder mit GPU */
	int hw_dl_errors; /* fehlgeschlagene Kopien aus dem Grafikspeicher in Folge */
	pthread_mutex_t m;
	pthread_cond_t c;
};

static void *decode_thread(void *data)
{
	struct dec_ctx *d = data;
	struct irl_src *s = d->s;
	os_set_thread_name("irl-srt-decode");
	SwrContext *swr = NULL;
	AVFrame *fr = av_frame_alloc();
	AVFrame *hwtmp = av_frame_alloc();
	bool wait_kf = true;
	int64_t last_vdts = AV_NOPTS_VALUE;
	/* Seit wann kommen Videopakete, ohne dass ein Bild herauskommt? (0 = Bild kam) */
	uint64_t vpkts_without_frame_since = 0;
	uint64_t last_vpkt_ns = 0;
	bool vbroken_logged = false;

	for (;;) {
		pthread_mutex_lock(&d->m);
		while (!d->count && !d->done)
			pthread_cond_wait(&d->c, &d->m);
		if (!d->count && d->done) {
			pthread_mutex_unlock(&d->m);
			break;
		}
		AVPacket *pkt = d->q[d->head];
		d->head = (d->head + 1) % PQ_MAX;
		d->count--;
		if (d->overflow_wait_kf) {
			d->overflow_wait_kf = false;
			wait_kf = true;
		}
		pthread_mutex_unlock(&d->m);

		if (pkt->stream_index == d->vi && d->vdec) {
			if (pkt->flags & AV_PKT_FLAG_CORRUPT)
				s->st_corrupt++;
			/* lange Luecke im Video (> 1 s) -> bis zum Keyframe letztes Bild halten */
			if (last_vdts != AV_NOPTS_VALUE && pkt->dts != AV_NOPTS_VALUE && d->vframe_dur > 0 &&
			    pkt->dts - last_vdts > 30 * d->vframe_dur && !wait_kf) {
				wait_kf = true;
				s->st_kf_waits++;
				DBG(s, "Video-Luecke -> warte auf Keyframe");
			}
			if (pkt->dts != AV_NOPTS_VALUE)
				last_vdts = pkt->dts;
			/* Pakete kommen, aber seit 6 s kein einziges Bild (laenger als jeder
			 * Keyframe-Abstand; z.B. Sender hat das Videoformat gewechselt und
			 * die Verbindung blieb bestehen) -> Sitzung neu aufbauen */
			uint64_t tnow = os_gettime_ns();
			/* nach einer Paketpause (Ausfall) neu zaehlen */
			if (!vpkts_without_frame_since || tnow - last_vpkt_ns > 1000000000ULL)
				vpkts_without_frame_since = tnow;
			else if (!vbroken_logged && tnow - vpkts_without_frame_since > 6000000000ULL) {
				SRC_LOG(LOG_WARNING, s,
					"Bildstrom seit 6 s nicht dekodierbar - baue Verbindung neu auf "
					"(letztes Bild bleibt stehen)");
				vbroken_logged = true;
				s->reopen = true;
			}
			last_vpkt_ns = tnow;
			bool skip = false;
			if (wait_kf) {
				if (!(pkt->flags & AV_PKT_FLAG_KEY)) {
					skip = true;
				} else {
					avcodec_flush_buffers(d->vdec);
					wait_kf = false;
				}
			}
			if (!skip) {
				if (avcodec_send_packet(d->vdec, pkt) < 0) {
					s->st_dec_err++;
					DBG(s, "Dekodierfehler (Decoder verschleiert)");
				}
				while (avcodec_receive_frame(d->vdec, fr) >= 0) {
					if (fr->decode_error_flags)
						s->st_dec_err++;
					if (hwdec_is_hw_frame(fr)) {
						/* Bilder im Grafikspeicher NIE ungeholt weiterreichen */
						if (hwdec_download(fr, hwtmp) < 0) {
							av_frame_unref(fr);
							if (d->hw && ++d->hw_dl_errors >= 5) {
								SRC_LOG(LOG_WARNING, s,
									"Bild aus der GPU nicht lesbar - wechsle auf "
									"Software-Dekodierung (letztes Bild bleibt stehen)");
								s->hw_failed_codec = AV_CODEC_ID_NONE;
								s->hw_failed = true;
								s->reopen = true;
								d->hw = false; /* nur einmal melden */
							}
							continue;
						}
						d->hw_dl_errors = 0;
					} else if (d->hw) {
						/* GPU unterstuetzt dieses Format nicht, ffmpeg dekodiert
						 * bereits in Software (aber nur 1 Thread) -> Sitzung mit
						 * voller Software-Dekodierung neu aufbauen */
						SRC_LOG(LOG_INFO, s,
							"GPU kann dieses Videoformat nicht dekodieren - "
							"nutze Software-Dekodierung");
						s->hw_failed_codec = d->vdec->codec_id;
						s->hw_failed = true;
						s->reopen = true;
						d->hw = false; /* nur einmal melden */
					}
					vpkts_without_frame_since = 0;
					push_video(s, fr, d->fmt->streams[d->vi]);
					av_frame_unref(fr);
				}
			}
		} else if (pkt->stream_index == d->ai && d->adec) {
			if (avcodec_send_packet(d->adec, pkt) >= 0) {
				while (avcodec_receive_frame(d->adec, fr) >= 0) {
					push_audio(s, fr, &swr, d->fmt->streams[d->ai]);
					av_frame_unref(fr);
				}
			}
		}
		av_packet_free(&pkt);
	}
	swr_free(&swr);
	av_frame_free(&fr);
	av_frame_free(&hwtmp);
	return NULL;
}

static bool read_session(struct irl_src *s)
{
	AVFormatContext *fmt = avformat_alloc_context();
	AVCodecContext *vdec = NULL, *adec = NULL;
	AVPacket *pkt = av_packet_alloc();
	AVDictionary *opts = NULL;
	int vi = -1, ai = -1;
	bool got_data = false;
	struct dec_ctx *d = NULL;
	pthread_t dt;
	bool dt_running = false;

	fmt->interrupt_callback.callback = interrupt_cb;
	fmt->interrupt_callback.opaque = s;
	/* Daten-Timeout 15 s: kuerzere Ausfaelle ueberbrueckt die Verbindung selbst */
	av_dict_set(&opts, "timeout", "15000000", 0);
	av_dict_set(&opts, "rw_timeout", "15000000", 0);
	av_dict_set(&opts, "probesize", "500000", 0);
	av_dict_set(&opts, "analyzeduration", "700000", 0);
	/* KEIN +discardcorrupt: ein verworfenes Paket kostet beim HEVC-Decoder den
	 * Rest der GOP (bis 2 s Standbild, auf der Test-VM gemessen). Kurze
	 * Bildfehler verdeckt die Fehlerverschleierung des Decoders. */

	/* Adresse unter Sperre kopieren - irl_update() kann sie jederzeit ersetzen */
	pthread_mutex_lock(&s->mtx);
	char *url = bstrdup(s->url ? s->url : "");
	pthread_mutex_unlock(&s->mtx);

	set_status(s, "verbinde");
	int open_ret = avformat_open_input(&fmt, url, NULL, &opts);
	bfree(url);
	if (open_ret < 0) {
		fmt = NULL;
		goto out;
	}
	if (avformat_find_stream_info(fmt, NULL) < 0)
		goto out;

	vi = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1, NULL, 0);
	ai = av_find_best_stream(fmt, AVMEDIA_TYPE_AUDIO, -1, -1, NULL, 0);
	bool vhw = false;
	if (vi >= 0) {
		char desc[64];
		/* GPU nur auslassen, wenn sie genau mit diesem Codec (oder generell)
		 * gescheitert ist - schickt der Sender spaeter z.B. wieder HEVC statt
		 * eines exotischen Formats, wird die GPU erneut genutzt */
		int cid = fmt->streams[vi]->codecpar->codec_id;
		bool try_hw = !s->hw_failed || (s->hw_failed_codec != AV_CODEC_ID_NONE && s->hw_failed_codec != cid);
		vdec = open_decoder_ex(fmt->streams[vi], try_hw, (enum hwdec_mode)s->hw_mode, &vhw, desc,
				       sizeof(desc));
		if (vdec) {
			char full[96];
			snprintf(full, sizeof(full), "%.63s %.24s", vhw ? desc : "Software", vdec->codec->name);
			pthread_mutex_lock(&s->mtx);
			bool changed = strcmp(s->decoder, full) != 0;
			snprintf(s->decoder, sizeof(s->decoder), "%s", full);
			pthread_mutex_unlock(&s->mtx);
			if (changed)
				SRC_LOG(LOG_INFO, s, "Bild-Dekodierung: %s", full);
		}
	}
	if (ai >= 0)
		adec = open_decoder(fmt->streams[ai]);
	if (!vdec && !adec)
		goto out;

	pthread_mutex_lock(&s->mtx);
	if (s->has_audio != (adec != NULL) || s->has_video != (vdec != NULL)) {
		/* andere Spuren als vorher -> komplett neu beginnen */
		aring_clear(&s->ring);
		vq_clear(&s->vq);
		s->newest_video_us = NO_PTS;
		s->resync = true;
	}
	/* Sonst laeuft die Uhr einfach weiter: nach einer Neuverbindung zum selben
	 * Sender setzt die Zeitachse fort (Luecke = Stille + stehendes Bild);
	 * ein echter Zeitbasis-Wechsel wird in push_audio/push_video erkannt. */
	s->has_audio = adec != NULL;
	s->has_video = vdec != NULL;
	pthread_mutex_unlock(&s->mtx);

	d = bzalloc(sizeof(*d));
	d->s = s;
	d->fmt = fmt;
	d->vdec = vdec;
	d->adec = adec;
	d->vi = vi;
	d->ai = ai;
	d->hw = vhw;
	if (vi >= 0 && fmt->streams[vi]->avg_frame_rate.num)
		d->vframe_dur = av_rescale_q(1, av_inv_q(fmt->streams[vi]->avg_frame_rate), fmt->streams[vi]->time_base);
	pthread_mutex_init(&d->m, NULL);
	pthread_cond_init(&d->c, NULL);
	if (pthread_create(&dt, NULL, decode_thread, d) != 0)
		goto out;
	dt_running = true;
	set_status(s, "verbunden");

	while (!s->stop && !s->reopen) {
		int ret = av_read_frame(fmt, pkt);
		if (ret < 0) {
			char e[128];
			av_strerror(ret, e, sizeof(e));
			DBG(s, "Lesefehler: %s", e);
			break;
		}
		got_data = true;
		/* Sender hat Video-/Tonformat gewechselt, die Verbindung blieb aber
		 * bestehen (SRT-Server reicht neue Sendung weiter) -> neu aufbauen */
		if ((vdec && fmt->streams[vi]->codecpar->codec_id != vdec->codec_id) ||
		    (adec && fmt->streams[ai]->codecpar->codec_id != adec->codec_id)) {
			SRC_LOG(LOG_INFO, s, "Sender hat das Format gewechselt - baue Verbindung neu auf");
			av_packet_unref(pkt);
			break;
		}
		AVPacket *c = av_packet_clone(pkt);
		av_packet_unref(pkt);
		if (!c)
			continue;
		pthread_mutex_lock(&d->m);
		if (d->count == PQ_MAX) {
			/* Decoder kommt nicht hinterher (System ueberlastet): alles
			 * Wartende verwerfen und beim naechsten Keyframe weitermachen */
			while (d->count) {
				av_packet_free(&d->q[d->head]);
				d->head = (d->head + 1) % PQ_MAX;
				d->count--;
			}
			d->overflow_wait_kf = true;
			s->st_vdrops++;
		}
		d->q[(d->head + d->count) % PQ_MAX] = c;
		d->count++;
		pthread_cond_signal(&d->c);
		pthread_mutex_unlock(&d->m);
	}

out:
	if (dt_running) {
		pthread_mutex_lock(&d->m);
		d->done = true;
		pthread_cond_signal(&d->c);
		pthread_mutex_unlock(&d->m);
		pthread_join(dt, NULL);
	}
	if (d) {
		while (d->count) {
			av_packet_free(&d->q[d->head]);
			d->head = (d->head + 1) % PQ_MAX;
			d->count--;
		}
		pthread_mutex_destroy(&d->m);
		pthread_cond_destroy(&d->c);
		bfree(d);
	}
	set_status(s, "getrennt - letztes Bild bleibt, Ton still");
	av_dict_free(&opts);
	avcodec_free_context(&vdec);
	avcodec_free_context(&adec);
	if (fmt)
		avformat_close_input(&fmt);
	av_packet_free(&pkt);
	return got_data;
}

static void *read_thread(void *data)
{
	struct irl_src *s = data;
	os_set_thread_name("irl-srt-read");
	bool first = true;
	while (!s->stop) {
		if (s->reopen)
			s->reopen = false;
		pthread_mutex_lock(&s->mtx);
		bool have_url = s->url && *s->url;
		pthread_mutex_unlock(&s->mtx);
		if (!have_url) {
			os_sleep_ms(200);
			continue;
		}
		if (!first)
			s->st_reconnects++;
		first = false;
		/* hatte die Sitzung Daten (Abbruch mitten im Stream), sofort neu
		 * verbinden; sonst (kein Sender da) die eingestellte Pause */
		int wait_ms = read_session(s) ? 100 : s->reconnect_ms;
		for (int i = 0; i < wait_ms / 50 && !s->stop && !s->reopen; i++)
			os_sleep_ms(50);
	}
	return NULL;
}

/* ------------------------------------------------------------------ */
/* Ausgabe-Thread                                                      */
/* ------------------------------------------------------------------ */
static void output_frame(struct irl_src *s, AVFrame *f, uint64_t ts, struct SwsContext **sws, AVFrame **conv)
{
	enum video_format vf = av_to_obs_fmt(f->format);
	AVFrame *src = f;
	if (vf == VIDEO_FORMAT_NONE) {
		*sws = sws_getCachedContext(*sws, f->width, f->height, f->format, f->width, f->height, AV_PIX_FMT_YUV420P,
					    SWS_BILINEAR, NULL, NULL, NULL);
		if (!*sws)
			return;
		if (!*conv || (*conv)->width != f->width || (*conv)->height != f->height) {
			av_frame_free(conv);
			*conv = av_frame_alloc();
			(*conv)->format = AV_PIX_FMT_YUV420P;
			(*conv)->width = f->width;
			(*conv)->height = f->height;
			if (av_frame_get_buffer(*conv, 32) < 0) {
				av_frame_free(conv);
				return;
			}
		}
		sws_scale(*sws, (const uint8_t *const *)f->data, f->linesize, 0, f->height, (*conv)->data,
			  (*conv)->linesize);
		src = *conv;
		vf = VIDEO_FORMAT_I420;
	}
	struct obs_source_frame of = {0};
	for (int i = 0; i < MAX_AV_PLANES && i < AV_NUM_DATA_POINTERS; i++) {
		of.data[i] = src->data[i];
		of.linesize[i] = src->linesize[i] < 0 ? 0 : (uint32_t)src->linesize[i];
	}
	of.width = (uint32_t)src->width;
	of.height = (uint32_t)src->height;
	of.format = vf;
	of.timestamp = ts;
	of.full_range = f->color_range == AVCOL_RANGE_JPEG;
	enum video_colorspace cs = VIDEO_CS_709;
	if (f->colorspace == AVCOL_SPC_BT470BG || f->colorspace == AVCOL_SPC_SMPTE170M)
		cs = VIDEO_CS_601;
	else if (f->colorspace == AVCOL_SPC_BT2020_NCL)
		cs = f->color_trc == AVCOL_TRC_ARIB_STD_B67   ? VIDEO_CS_2100_HLG
		     : f->color_trc == AVCOL_TRC_SMPTE2084 ? VIDEO_CS_2100_PQ
							   : VIDEO_CS_709;
	video_format_get_parameters_for_format(cs, of.full_range ? VIDEO_RANGE_FULL : VIDEO_RANGE_PARTIAL, vf,
					       of.color_matrix, of.color_range_min, of.color_range_max);
	obs_source_output_video(s->source, &of);
}

static void write_stats(struct irl_src *s, uint64_t now)
{
	if (now - s->st_last_json_ns < 250000000ULL)
		return;
	s->st_last_json_ns = now;
	/* eine Datei je Quelle: /tmp/irl-srt-source-<Name>.json (Sonderzeichen -> _) */
	char fin[256], tmp[272], nm[128];
	const char *src_name = obs_source_get_name(s->source);
	size_t k = 0;
	for (const char *c = src_name ? src_name : "quelle"; *c && k < sizeof(nm) - 1; c++)
		nm[k++] = ((*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || (*c >= '0' && *c <= '9')) ? *c : '_';
	nm[k] = 0;
	snprintf(fin, sizeof(fin), "/tmp/irl-srt-source-%s.json", nm);
	snprintf(tmp, sizeof(tmp), "%s.tmp", fin);
	/* B6: status/decoder werden von anderen Threads unter mtx ersetzt (set_status gibt den
	 * alten String frei) -> hier unter mtx kopieren statt danach ungeschuetzt lesen. */
	char st_copy[128], dec_copy[96];
	pthread_mutex_lock(&s->mtx);
	snprintf(st_copy, sizeof(st_copy), "%s", s->status ? s->status : "");
	snprintf(dec_copy, sizeof(dec_copy), "%s", s->decoder);
	pthread_mutex_unlock(&s->mtx);
	FILE *f = fopen(tmp, "w");
	if (!f)
		return;
	fprintf(f,
		"{\"source\":\"%s\",\"status\":\"%s\",\"lag_ms\":%d,\"target_ms\":%d,\"rate\":%.4f,"
		"\"silence_ms\":%llu,\"rebuffers\":%llu,\"jumps\":%llu,\"gaps\":%llu,\"keyframe_waits\":%llu,"
		"\"reconnects\":%llu,\"video_out\":%llu,\"video_drops\":%llu,\"vq\":%d,\"static\":%llu,\"corrupt\":%llu,\"dec_err\":%llu,\"decoder\":\"%s\",\"ts\":%llu}\n",
		obs_source_get_name(s->source), st_copy, s->dbg_lag_ms, s->buffer_ms, s->rate,
		(unsigned long long)s->st_silence_ms, (unsigned long long)s->st_rebuffers,
		(unsigned long long)s->st_jumps, (unsigned long long)s->st_gaps, (unsigned long long)s->st_kf_waits,
		(unsigned long long)s->st_reconnects, (unsigned long long)s->st_vout, (unsigned long long)s->st_vdrops,
		s->dbg_vq, (unsigned long long)s->st_static, (unsigned long long)s->st_corrupt,
		(unsigned long long)s->st_dec_err, dec_copy, (unsigned long long)(now / 1000000));
	fclose(f);
	rename(tmp, fin);
}

/* Neuester Zeitpunkt, fuer den ALLE vorhandenen Spuren Daten haben (unter mtx). */
static int64_t data_edge_us(struct irl_src *s)
{
	int64_t a = s->has_audio ? aring_end_pts(&s->ring) : NO_PTS;
	int64_t v = s->has_video ? s->newest_video_us : NO_PTS;
	if (s->has_audio && a == NO_PTS)
		return NO_PTS;
	if (s->has_video && v == NO_PTS)
		return NO_PTS;
	if (a == NO_PTS)
		return v;
	if (v == NO_PTS)
		return a;
	return a < v ? a : v;
}

static void *out_thread(void *data)
{
	struct irl_src *s = data;
	os_set_thread_name("irl-srt-out");
	struct SwsContext *sws = NULL;
	AVFrame *conv = NULL;
	float *outbuf[OUT_CH];
	const size_t max_chunk = OUT_RATE / 10;
	for (int c = 0; c < OUT_CH; c++)
		outbuf[c] = bzalloc(sizeof(float) * max_chunk);

	uint64_t next = os_gettime_ns();
	uint64_t audio_ts = 0;
	s->st_last_log_ns = next;

	while (!s->stop) {
		next += TICK_NS;
		uint64_t now = os_gettime_ns();
		if (next + 200000000ULL < now)
			next = now; /* System hing -> nicht hinterherhetzen */
		os_sleepto_ns(next);
		now = os_gettime_ns();

		/* lueckenlose Audio-Zeitstempel im Takt der Systemuhr */
		if (!audio_ts || now > audio_ts + 500000000ULL || audio_ts > now + 500000000ULL)
			audio_ts = now;
		double owed = (double)(now + TICK_NS - audio_ts) * OUT_RATE / 1e9;
		size_t n = owed > 0 ? (size_t)owed : 0;
		if (n > max_chunk)
			n = max_chunk;

		pthread_mutex_lock(&s->mtx);
		struct aring *a = &s->ring;
		const int64_t D = (int64_t)s->buffer_ms * 1000;
		int64_t edge = data_edge_us(s);
		bool fresh = s->last_data_ns && now - s->last_data_ns < 300000000ULL;

		/* --- Uhr ausrichten ------------------------------------------------ */
		if (s->resync && edge != NO_PTS) {
			s->clock_valid = false;
			s->resync = false;
		}
		if (!s->clock_valid && edge != NO_PTS) {
			int64_t oldest = s->has_audio ? a->head_pts_us : (s->vq.count ? s->vq.pts_us[s->vq.head] : NO_PTS);
			if (oldest != NO_PTS && edge - oldest >= D) {
				s->clock_us = edge - D;
				s->clock_valid = true;
				s->rate = 1.0;
				s->frac = 0.0;
				s->lag_min_us = D;
				s->lag_win_start_ns = now;
				s->behind_since_ns = 0;
				SRC_LOG(LOG_INFO, s, "Wiedergabe laeuft (Polster %d ms)", s->buffer_ms);
			}
		}

		int64_t lag = (s->clock_valid && edge != NO_PTS) ? edge - s->clock_us : 0;
		if (s->clock_valid && edge != NO_PTS) {
			/* Daten kommen wieder, aber die Uhr ist ihnen davongelaufen
			 * (Nachlieferung nach Stau) -> neu ausrichten statt Dauerstille */
			if (lag < -150000 && fresh) {
				if (!s->behind_since_ns)
					s->behind_since_ns = now;
				/* 1 s abwarten: nach einem Ausfall kommt erst der Rueckstau
				 * (alte Daten) und Sekundenbruchteile spaeter ein Schwall bis
				 * zur Gegenwart - vorschnelles Zurueckstellen kostete Standbild */
				if (now - s->behind_since_ns > 1000000000ULL) {
					DBG(s, "Uhr %lld ms vor den Daten -> neu ausgerichtet", (long long)(-lag / 1000));
					s->clock_us = edge - D;
					s->st_rebuffers++;
					s->behind_since_ns = 0;
					s->lag_min_us = D;
					lag = D;
				}
			} else {
				s->behind_since_ns = 0;
			}
			/* deutlich zu viel Vorrat (Datenschwall) -> nach vorn springen */
			if (lag > D + 1500000) {
				DBG(s, "Vorrat %lld ms -> Sprung nach vorn", (long long)(lag / 1000));
				s->clock_us = edge - D;
				s->st_jumps++;
				lag = D;
				s->lag_min_us = D;
			}
			/* Tempo nach tiefstem Abstand der letzten 5 s (nur mit frischen Daten) */
			if (fresh) {
				if (lag < s->lag_min_us)
					s->lag_min_us = lag;
				if (now - s->lag_win_start_ns > 5000000000ULL) {
					double err = (double)(s->lag_min_us - D) / 1e6; /* Sekunden */
					s->rate = 1.0 + fmax(-MAX_RATE_DEV, fmin(MAX_RATE_DEV, err * 0.02));
					s->lag_win_start_ns = now;
					s->lag_min_us = lag;
				}
			}
		}

		/* --- Audio fuer [clock, clock + n*rate) ---------------------------- */
		if (s->has_audio && n) {
			size_t silent = 0;
			if (!s->clock_valid) {
				for (int c = 0; c < OUT_CH; c++)
					memset(outbuf[c], 0, sizeof(float) * n);
				silent = n;
			} else {
				/* zu alte Samples (Uhr ist weiter) verwerfen */
				if (a->head_pts_us != NO_PTS && a->n) {
					int64_t off = s->clock_us - a->head_pts_us;
					if (off > 20000) {
						aring_consume(a, (size_t)(off * OUT_RATE / 1000000));
						s->frac = 0.0;
					}
				}
				/* Daten beginnen erst nach der Uhr -> bis dahin Stille */
				size_t lead_silence = n;
				if (a->head_pts_us != NO_PTS && a->n) {
					int64_t off = a->head_pts_us - s->clock_us;
					lead_silence = off > 20000 ? (size_t)(off * OUT_RATE / 1000000) : 0;
				}
				if (lead_silence > n)
					lead_silence = n;
				for (int c = 0; c < OUT_CH; c++)
					memset(outbuf[c], 0, sizeof(float) * lead_silence);
				double pos = s->frac;
				size_t i = lead_silence;
				for (; i < n; i++) {
					size_t i0 = (size_t)pos;
					if (i0 + 1 >= a->n)
						break;
					float t = (float)(pos - (double)i0);
					for (int c = 0; c < OUT_CH; c++) {
						float v0 = aring_at(a, c, i0), v1 = aring_at(a, c, i0 + 1);
						outbuf[c][i] = v0 + (v1 - v0) * t;
					}
					pos += s->rate;
				}
				for (size_t j = i; j < n; j++)
					for (int c = 0; c < OUT_CH; c++)
						outbuf[c][j] = 0.0f;
				silent = lead_silence + (n - i);
				size_t used = (size_t)pos;
				s->frac = i < n ? 0.0 : pos - (double)used;
				aring_consume(a, used);
			}
			s->st_silence_ms += (uint64_t)silent * 1000 / OUT_RATE;
		}

		/* Uhr weiterdrehen - sie haelt NIE an */
		if (s->clock_valid)
			s->clock_us += (int64_t)((double)(n ? n : OUT_RATE / 100) * s->rate * 1e6 / OUT_RATE);

		/* --- Video: neuestes faelliges Bild -------------------------------- */
		AVFrame *show = NULL;
		if (s->clock_valid) {
			while (s->vq.count && s->vq.pts_us[s->vq.head] <= s->clock_us) {
				if (show)
					av_frame_free(&show);
				show = s->vq.f[s->vq.head];
				s->vq.f[s->vq.head] = NULL;
				s->vq.head = (s->vq.head + 1) % VQ_MAX;
				s->vq.count--;
			}
		} else {
			while (s->vq.count > VQ_MAX - 10)
				vq_pop_free(&s->vq);
		}

		s->dbg_lag_ms = (int)(lag / 1000);
		s->dbg_vq = s->vq.count;
		bool has_audio = s->has_audio;
		pthread_mutex_unlock(&s->mtx);

		if (has_audio && n) {
			struct obs_source_audio oa = {0};
			for (int c = 0; c < OUT_CH; c++)
				oa.data[c] = (const uint8_t *)outbuf[c];
			oa.frames = (uint32_t)n;
			oa.speakers = SPEAKERS_STEREO;
			oa.format = AUDIO_FORMAT_FLOAT_PLANAR;
			oa.samples_per_sec = OUT_RATE;
			oa.timestamp = audio_ts;
			obs_source_output_audio(s->source, &oa);
		}
		if (n)
			audio_ts += (uint64_t)n * 1000000000ULL / OUT_RATE;
		if (show) {
			if (g_debug && show->data[0]) {
				uint64_t sum = 0;
				for (int y = show->height / 4; y < show->height * 3 / 4; y += 16)
					for (int x = 0; x < show->width; x += 8)
						sum = sum * 31 + show->data[0][y * show->linesize[0] + x];
				if (sum == s->last_sum)
					s->st_static++;
				s->last_sum = sum;
			}
			output_frame(s, show, now, &sws, &conv);
			av_frame_free(&show);
			s->st_vout++;
		}

		int lag_ms = (int)(lag / 1000);
		if (lag_ms < s->st_lag_min_ms)
			s->st_lag_min_ms = lag_ms;
		if (lag_ms > s->st_lag_max_ms)
			s->st_lag_max_ms = lag_ms;
		write_stats(s, now);
		if (now - s->st_last_log_ns > 60000000000ULL) {
			s->st_last_log_ns = now;
			SRC_LOG(LOG_INFO, s,
				"Stat: Vorrat %d ms (min %d / max %d, Ziel %d), Tempo %.4f, Stille gesamt %llu ms, "
				"Neuausrichtungen %llu, Spruenge %llu, Luecken %llu, Keyframe-Warten %llu, Neuverbindungen %llu",
				lag_ms, s->st_lag_min_ms, s->st_lag_max_ms, s->buffer_ms, s->rate,
				(unsigned long long)s->st_silence_ms, (unsigned long long)s->st_rebuffers,
				(unsigned long long)s->st_jumps, (unsigned long long)s->st_gaps,
				(unsigned long long)s->st_kf_waits, (unsigned long long)s->st_reconnects);
			s->st_lag_min_ms = INT32_MAX;
			s->st_lag_max_ms = INT32_MIN;
		}
	}
	for (int c = 0; c < OUT_CH; c++)
		bfree(outbuf[c]);
	sws_freeContext(sws);
	av_frame_free(&conv);
	return NULL;
}

/* ------------------------------------------------------------------ */
/* OBS-Callbacks                                                       */
/* ------------------------------------------------------------------ */
static const char *irl_get_name(void *unused)
{
	UNUSED_PARAMETER(unused);
	return "IRL SRT Quelle (stabil)";
}

static void irl_update(void *data, obs_data_t *settings)
{
	struct irl_src *s = data;
	const char *url = obs_data_get_string(settings, "input");
	int buf = (int)obs_data_get_int(settings, "buffer_ms");
	int rc = (int)obs_data_get_int(settings, "reconnect_ms");
	long long nonce = obs_data_get_int(settings, "reconnect_nonce");
	int hw_mode = (int)obs_data_get_int(settings, "hw_decode") == HWDEC_OFF ? HWDEC_OFF : HWDEC_AUTO;
	/* Neuverbindung bei geaenderter Adresse oder auf ausdruecklichen Wunsch
	 * (Dashboard-Knopf "Quelle reparieren" setzt reconnect_nonce) */
	bool changed = !s->url || strcmp(s->url, url) != 0 || nonce != s->reconnect_nonce;
	s->reconnect_nonce = nonce;
	if (hw_mode != s->hw_mode || changed) {
		/* neue Wahl oder ausdrueckliche Neuverbindung -> GPU erneut versuchen */
		changed = changed || hw_mode != s->hw_mode;
		s->hw_mode = hw_mode;
		s->hw_failed = false;
	}
	pthread_mutex_lock(&s->mtx);
	bfree(s->url);
	s->url = bstrdup(url);
	s->buffer_ms = buf < 100 ? 100 : buf > 3000 ? 3000 : buf;
	s->reconnect_ms = rc < 100 ? 100 : rc;
	pthread_mutex_unlock(&s->mtx);
	/* Erneutes Speichern derselben Einstellungen (wie es der alte Auto-Fix
	 * tat) unterbricht nichts. Auch eine Neuverbindung haelt das letzte Bild. */
	if (changed && s->threads_running)
		s->reopen = true;
}

static void irl_defaults(obs_data_t *settings)
{
	obs_data_set_default_string(settings, "input", "");
	obs_data_set_default_int(settings, "buffer_ms", 1000);
	obs_data_set_default_int(settings, "reconnect_ms", 1000);
	obs_data_set_default_int(settings, "hw_decode", HWDEC_AUTO);
}

static obs_properties_t *irl_properties(void *unused)
{
	UNUSED_PARAMETER(unused);
	obs_properties_t *p = obs_properties_create();
	obs_properties_add_text(p, "input", "SRT-Adresse (z.B. srt://localhost:8282?streamid=...)", OBS_TEXT_DEFAULT);
	obs_property_t *b = obs_properties_add_int_slider(p, "buffer_ms", "Polster gegen Aussetzer (ms)", 100, 3000, 50);
	obs_property_set_long_description(b,
					  "Mehr = stabiler bei schlechtem Netz, aber Bild und Ton kommen entsprechend spaeter.");
	obs_properties_add_int(p, "reconnect_ms", "Wartezeit vor Neuverbindung (ms)", 100, 30000, 100);
	obs_property_t *h = obs_properties_add_list(p, "hw_decode", "Bild-Dekodierung", OBS_COMBO_TYPE_LIST,
						    OBS_COMBO_FORMAT_INT);
	obs_property_list_add_int(h, "Automatisch (Grafikkarte Nvidia/Intel/AMD, sonst Prozessor)", HWDEC_AUTO);
	obs_property_list_add_int(h, "Nur Prozessor (Software)", HWDEC_OFF);
	obs_property_set_long_description(h, "Die Grafikkarte entlastet den Prozessor deutlich. Klappt sie nicht, "
					     "wird automatisch auf den Prozessor gewechselt.");
	return p;
}

static void *irl_create(obs_data_t *settings, obs_source_t *source)
{
	struct irl_src *s = bzalloc(sizeof(*s));
	s->source = source;
	/* Wir takten selbst: OBS soll jedes Bild sofort zeigen und nicht nach
	 * eigenem Zeitstempel-Abgleich zurueckhalten (das gab auf der Test-VM
	 * Standbilder trotz gleichmaessiger 30 Bilder/s Ausgabe). */
	obs_source_set_async_unbuffered(source, true);
	pthread_mutex_init(&s->mtx, NULL);
	aring_init(&s->ring);
	s->rate = 1.0;
	s->newest_video_us = NO_PTS;
	s->st_lag_min_ms = INT32_MAX;
	s->st_lag_max_ms = INT32_MIN;
	irl_update(s, settings);
	s->reopen = false;
	if (pthread_create(&s->read_thread, NULL, read_thread, s) == 0) {
		if (pthread_create(&s->out_thread, NULL, out_thread, s) == 0) {
			s->threads_running = true;
		} else {
			s->stop = true;
			pthread_join(s->read_thread, NULL);
		}
	}
	return s;
}

static void irl_destroy(void *data)
{
	struct irl_src *s = data;
	s->stop = true;
	if (s->threads_running) {
		pthread_join(s->read_thread, NULL);
		pthread_join(s->out_thread, NULL);
	}
	vq_clear(&s->vq);
	aring_free(&s->ring);
	pthread_mutex_destroy(&s->mtx);
	bfree(s->url);
	bfree(s->status);
	bfree(s);
}

static struct obs_source_info irl_srt_source_info = {
	.id = "irl_srt_source",
	.type = OBS_SOURCE_TYPE_INPUT,
	.output_flags = OBS_SOURCE_ASYNC_VIDEO | OBS_SOURCE_AUDIO | OBS_SOURCE_DO_NOT_DUPLICATE,
	.get_name = irl_get_name,
	.create = irl_create,
	.destroy = irl_destroy,
	.update = irl_update,
	.get_defaults = irl_defaults,
	.get_properties = irl_properties,
	.icon_type = OBS_ICON_TYPE_MEDIA,
};

bool obs_module_load(void)
{
	const char *dbg = getenv("IRL_SRT_DEBUG");
	g_debug = dbg && *dbg == '1';
	avformat_network_init();
	obs_register_source(&irl_srt_source_info);
	blog(LOG_INFO, "[IRL SRT Source] geladen (Version " IRL_SRT_SOURCE_VERSION ", OBS %s, libavformat %s)",
	     obs_get_version_string(), AV_STRINGIFY(LIBAVFORMAT_VERSION));
	return true;
}

void obs_module_unload(void)
{
	avformat_network_deinit();
}
