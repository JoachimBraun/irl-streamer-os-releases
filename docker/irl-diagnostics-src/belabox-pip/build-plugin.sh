#!/bin/sh
# Baut libgstirlpip-aarch64.so NEU (auf einer BELABOX/Rock5B+ oder Orange Pi 5+ mit den Entwicklungspaketen):
#   apt install gstreamer1.0-tools libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev pkg-config gcc
#   sh build-plugin.sh   -> libgstirlpip-aarch64.so + SHA256SUMS
# Die fertig gebaute Datei liegt im Repo, damit auf Kunden-Boxen NICHTS gebaut/installiert werden muss.
set -e
cd "$(dirname "$0")"
# pkg-config liefert mehrere Compiler-Flags - Worttrennung ist hier gewollt.
# shellcheck disable=SC2046
gcc -shared -fPIC -O2 -Wall -Wextra -Wno-unused-parameter gstirlpip.c -o libgstirlpip-aarch64.so \
    $(pkg-config --cflags --libs gstreamer-video-1.0 gstreamer-base-1.0)
sha256sum libgstirlpip-aarch64.so gstirlpip.c > SHA256SUMS
echo "gebaut: $(cat SHA256SUMS | cut -c1-16 | tr '\n' ' ')"
