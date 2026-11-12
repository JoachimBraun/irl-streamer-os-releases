#!/usr/bin/env bash
# IRL-STREAMER-OS-CHROME-WRAPPER (Web Bluetooth) - von provision.sh per dpkg-divert verwaltet.
# Ruft IMMER direkt das echte Chrome auf, nie /usr/bin/google-chrome-stable(.real) - eine
# Endlosschleife ist damit in jedem Zustand von Diversion/Paket ausgeschlossen (Review 11.11.2026, A7).
exec /opt/google/chrome/google-chrome --enable-experimental-web-platform-features "$@"
