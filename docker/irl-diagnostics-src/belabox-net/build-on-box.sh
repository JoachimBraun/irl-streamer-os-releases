#!/bin/sh
# Baut belacoder fuer die Sendekette nach - AUF DER BOX (RK3588, aarch64), z. B. nach einer neuen Upstream-Version.
# Ergebnis: belacoder-aarch64 in diesem Ordner (danach ins Repo uebernehmen).
# Quelltext = Upstream-Commit + NUR der Kennzahlen-Patch (patches/belacoder-stats.patch, GPL-3.0, siehe README.md).
# Regler und srtla_send bleiben Original: der Live-Vergleich 25.10. (nur Mobilfunk) war mit den IRL4YOU-Regler-/Wegewahl-
# Patches deutlich schlechter (23-27 statt 6 Einbrueche unter 800 kbit, 1800-3100 statt 35 verlorene Pakete in 10 min).
# Braucht git, gcc, patch, pkg-config, gstreamer-/glib-Header und /usr/include/srt/srt.h (auf der BELABOX vorhanden).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
BC_COMMIT=ccce9ca33c8e425b33353500b95795101e847964
D=$(mktemp -d /var/tmp/irl-net-build.XXXXXX)
trap 'rm -rf "$D"' EXIT
git clone --quiet https://github.com/BELABOX/belacoder "$D/belacoder"
# shellcheck disable=SC2046  # pkg-config-Flags sollen in einzelne Woerter zerfallen
(cd "$D/belacoder" && git checkout --quiet "$BC_COMMIT" \
  && patch -p1 --quiet < "$HERE/patches/belacoder-stats.patch" \
  && gcc -O2 -Wall -DVERSION="\"${BC_COMMIT%"${BC_COMMIT#???????}"}-irl2\"" belacoder.c -o belacoder \
       $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-app-1.0 glib-2.0) -I/usr/include/srt -lsrt -ldl)
install -m 755 "$D/belacoder/belacoder" "$HERE/belacoder-aarch64"
sha256sum "$HERE/belacoder-aarch64"
