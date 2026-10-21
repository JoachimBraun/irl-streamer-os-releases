# guacd mit Ton fuer GNOME Remote Desktop (IRL Streamer OS, V1.107)
#
# Warum: Das offizielle guacd meldet sich auf dem RDP-Tonkanal (RDPSND) fest
# als Protokollversion 6. GNOME Remote Desktop verlangt mindestens Version 8
# und schaltet den Ton sonst ab ("Client protocol version (6) is too old").
# Der Patch (guacd-rdpsnd-v8-wave2.patch) meldet Version 8 und versteht das
# dazugehoerige Wave2-Tonpaket. Alles andere bleibt das offizielle guacd.
#
# Bauweise: Nur das RDP-Modul (libguac-client-rdp) wird aus dem gleichen
# Quelltext (guacamole-server 1.6.0, Pruefsumme fest) neu gebaut - gegen die
# FreeRDP-/libguac-Dateien AUS dem offiziellen Image. Das Endergebnis ist das
# offizielle Image mit genau dieser einen ausgetauschten Datei. Dauer auf dem
# Mini-PC: wenige Minuten, danach im Docker-Cache.
#
# Gebaut/aktualisiert von provision/lib/guacd-audio.sh (bei jeder
# Provisionierung und jedem IRL-Streamer-OS-Update); schlaegt der Bau fehl,
# laeuft automatisch das offizielle guacd (Fernzugriff geht, nur ohne Ton).

ARG GUACD_BASE=guacamole/guacd@sha256:2d266a525a72ad55d8ea7cd4ca8342a1b44075759b03660d8684821b3636a922

FROM ${GUACD_BASE} AS builder
USER root
ARG GUAC_VERSION=1.6.0
ARG GUAC_SHA256=8bc45675da96d7b6f39728160181e3d4ff3c08f460f6d26de5805b642bf13f2b
RUN apk add --no-cache autoconf automake build-base cairo-dev libjpeg-turbo-dev libpng-dev \
        libtool libwebp-dev openssl1.1-compat-dev pango-dev pulseaudio-dev util-linux-dev patch
WORKDIR /build
RUN wget -q -O guac.tar.gz "https://archive.apache.org/dist/guacamole/${GUAC_VERSION}/source/guacamole-server-${GUAC_VERSION}.tar.gz" \
 && echo "${GUAC_SHA256}  guac.tar.gz" | sha256sum -c - \
 && tar xzf guac.tar.gz && mv "guacamole-server-${GUAC_VERSION}" src
COPY guacd-rdpsnd-v8-wave2.patch /build/
RUN cd src && patch -p1 < /build/guacd-rdpsnd-v8-wave2.patch \
 && PKG_CONFIG_PATH=/opt/guacamole/lib/pkgconfig \
    ./configure --prefix=/opt/guacamole --disable-guaclog --disable-guacenc --disable-guacd-docs \
       CPPFLAGS="-I/opt/guacamole/include -Wno-error=deprecated-declarations" LDFLAGS="-L/opt/guacamole/lib" \
 && make -j"$(nproc)" -C src/libguac && make -j"$(nproc)" -C src/common && \
    ( [ ! -d src/common-ssh ] || make -j"$(nproc)" -C src/common-ssh ) && \
    make -j"$(nproc)" -C src/protocols/rdp \
 && cp src/protocols/rdp/.libs/libguac-client-rdp.so.0.0.0 /build/libguac-client-rdp.so.0.0.0

FROM ${GUACD_BASE}
LABEL org.irlstreameros.guacd-audio="rdpsnd-v8-wave2"
COPY --from=builder /build/libguac-client-rdp.so.0.0.0 /opt/guacamole/lib/libguac-client-rdp.so.0.0.0
