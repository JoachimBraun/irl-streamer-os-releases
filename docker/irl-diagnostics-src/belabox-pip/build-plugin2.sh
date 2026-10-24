#!/bin/sh
# Baut libgstirlpip2-aarch64.so (IRL-PiP 2.0) AUF DER BOX (RK3588, GStreamer 1.20, aarch64) - analog build-plugin.sh. Auf der Box muss die Datei als libgstirlpip2.so installiert werden (GStreamer 1.20 leitet das Einstiegssymbol aus dem Dateinamen ab; irl-pip-setup.sh macht das).
# Voraussetzung (einmalig, auf einer Bau-Box, nicht auf Kunden-Boxen):
#   apt install gcc pkg-config libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
#   sh build-aarch64.sh            -> libgstirlpip2-aarch64.so + SHA256SUMS
#   BENCH=1 sh build-aarch64.sh    -> zusaetzlich irlpip2-scalebench-aarch64 (Mischkosten messen: ./irlpip2-scalebench-aarch64 200)
#   TEST=1 sh build-aarch64.sh     -> zusaetzlich Test-Plugin libgstirlpip2test-aarch64.so + irlpip2-check-aarch64
# Benutzen: GStreamer 1.20 laedt das Plugin nur unter dem Namen libgstirlpip2.so (Einstiegssymbol aus dem Dateinamen):
#   cp libgstirlpip2-aarch64.so <dir>/libgstirlpip2.so; GST_PLUGIN_PATH=<dir> gst-inspect-1.0 irlpip2
# -pthread: robuste prozessuebergreifende Mutexe (shm-Betrieb) liegen bei glibc < 2.34 in libpthread
# Nur GStreamer-API bis 1.8 benutzt (getestet mit 1.26 in der x86-Sandbox, Box hat 1.20.3).
set -e
cd "$(dirname "$0")"
# Cortex-A76 (grosse Kerne des RK3588) als Ziel, laeuft auch auf den A55-Kernen (ARMv8.2-A)
CPU="-march=armv8.2-a -mtune=cortex-a76"
# shellcheck disable=SC2046
gcc -shared -fPIC -O3 -pthread $CPU -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers gstirlpip2.c -o libgstirlpip2-aarch64.so \
    $(pkg-config --cflags --libs gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-base-1.0) -lm
if [ "${BENCH:-0}" = 1 ]; then
  # shellcheck disable=SC2046
  gcc -O3 $CPU -Wno-unused-function test/scalebench.c -o irlpip2-scalebench-aarch64 \
      $(pkg-config --cflags --libs gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-base-1.0) -lm
fi
if [ "${TEST:-0}" = 1 ]; then
  # shellcheck disable=SC2046
  gcc -shared -fPIC -O2 $CPU test/gstirlpip2jitter.c -o libgstirlpip2test-aarch64.so $(pkg-config --cflags --libs gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-base-1.0) -lm
  # shellcheck disable=SC2046
  gcc -O2 $CPU test/irlpip2-check.c -o irlpip2-check-aarch64 $(pkg-config --cflags --libs gstreamer-app-1.0 gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-base-1.0) -lm
fi
# SHA256SUMS gilt fuer das ganze Paket (V1.108-Plugin + 2.0) - alle vier Eintraege neu schreiben:
sha256sum libgstirlpip-aarch64.so gstirlpip.c libgstirlpip2-aarch64.so gstirlpip2.c > SHA256SUMS
echo "gebaut: $(cut -c1-16 SHA256SUMS | tr '\n' ' ')"
