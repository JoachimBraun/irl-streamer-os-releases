#include "hwdec.h"

#include <libavutil/hwcontext.h>
#include <libavutil/pixdesc.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static enum AVPixelFormat get_hw_format(AVCodecContext *ctx, const enum AVPixelFormat *fmts)
{
	enum AVPixelFormat want = (enum AVPixelFormat)(intptr_t)ctx->opaque;
	for (const enum AVPixelFormat *p = fmts; *p != AV_PIX_FMT_NONE; p++)
		if (*p == want)
			return *p;
	/* GPU kann dieses Format nicht (oder Initialisierung scheiterte) ->
	 * erstes Software-Format: Dekodierung laeuft in Software weiter */
	for (const enum AVPixelFormat *p = fmts; *p != AV_PIX_FMT_NONE; p++) {
		const AVPixFmtDescriptor *d = av_pix_fmt_desc_get(*p);
		if (d && !(d->flags & AV_PIX_FMT_FLAG_HWACCEL))
			return *p;
	}
	return AV_PIX_FMT_NONE;
}

static enum AVPixelFormat codec_hw_fmt(const AVCodec *codec, enum AVHWDeviceType type)
{
	for (int i = 0;; i++) {
		const AVCodecHWConfig *cfg = avcodec_get_hw_config(codec, i);
		if (!cfg)
			return AV_PIX_FMT_NONE;
		if ((cfg->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX) && cfg->device_type == type)
			return cfg->pix_fmt;
	}
}

static const char *vendor_name(const char *render_node)
{
	/* /dev/dri/renderD128 -> /sys/class/drm/renderD128/device/vendor */
	char path[128];
	const char *base = strrchr(render_node, '/');
	snprintf(path, sizeof(path), "/sys/class/drm/%s/device/vendor", base ? base + 1 : render_node);
	FILE *f = fopen(path, "r");
	if (!f)
		return "?";
	char v[16] = {0};
	if (!fgets(v, sizeof(v), f))
		v[0] = 0;
	fclose(f);
	if (strncmp(v, "0x8086", 6) == 0)
		return "Intel";
	if (strncmp(v, "0x1002", 6) == 0)
		return "AMD";
	if (strncmp(v, "0x10de", 6) == 0)
		return "Nvidia";
	return "?";
}

static bool try_device(AVCodecContext *ctx, const AVCodec *codec, enum AVHWDeviceType type, const char *dev)
{
	enum AVPixelFormat fmt = codec_hw_fmt(codec, type);
	if (fmt == AV_PIX_FMT_NONE)
		return false; /* ffmpeg kann diesen Codec auf diesem Weg nicht */
	AVBufferRef *ref = NULL;
	if (av_hwdevice_ctx_create(&ref, type, dev, NULL, 0) < 0)
		return false; /* kein Treiber / kein Zugriff / keine Karte */
	ctx->hw_device_ctx = ref; /* ctx uebernimmt die Referenz */
	ctx->opaque = (void *)(intptr_t)fmt;
	ctx->get_format = get_hw_format;
	/* ein Dekodier-Thread genuegt der GPU; zwei Reservebilder fuer unsere Kopie */
	ctx->thread_count = 1;
	ctx->extra_hw_frames = 2;
	return true;
}

bool hwdec_attach(AVCodecContext *ctx, const AVCodec *codec, enum hwdec_mode mode, char *desc, size_t desc_len)
{
	snprintf(desc, desc_len, "Software");
	if (mode == HWDEC_OFF || !codec)
		return false;

	/* 1) Nvidia: nur versuchen, wenn der proprietaere Treiber geladen ist */
	if (access("/dev/nvidia0", F_OK) == 0 && try_device(ctx, codec, AV_HWDEVICE_TYPE_CUDA, NULL)) {
		snprintf(desc, desc_len, "CUDA/NVDEC Nvidia");
		return true;
	}

	/* 2) Intel / AMD (und Nvidia mit VAAPI-Bruecke): erste nutzbare Render-Node */
	for (int n = 128; n < 136; n++) {
		char node[32];
		snprintf(node, sizeof(node), "/dev/dri/renderD%d", n);
		if (access(node, R_OK | W_OK) != 0)
			continue;
		if (try_device(ctx, codec, AV_HWDEVICE_TYPE_VAAPI, node)) {
			snprintf(desc, desc_len, "VAAPI %s renderD%d", vendor_name(node), n);
			return true;
		}
	}
	return false;
}

bool hwdec_is_hw_frame(const AVFrame *f)
{
	const AVPixFmtDescriptor *d = av_pix_fmt_desc_get((enum AVPixelFormat)f->format);
	return d && (d->flags & AV_PIX_FMT_FLAG_HWACCEL);
}

int hwdec_download(AVFrame *fr, AVFrame *tmp)
{
	av_frame_unref(tmp);
	int ret = av_hwframe_transfer_data(tmp, fr, 0);
	if (ret < 0)
		return ret;
	ret = av_frame_copy_props(tmp, fr);
	if (ret < 0)
		return ret;
	av_frame_unref(fr);
	av_frame_move_ref(fr, tmp);
	return 0;
}
