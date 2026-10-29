# Sendekette der BELABOX (IRL Streamer OS)

Vom Dashboard automatisch auf die Belabox gespielt und aktuell gehalten (wie die RTMP-Schutzschicht), Einrichtung:
`net-setup.sh install|uninstall|status`. Die Ideen stammen aus **IRL4YOU BOX** v1.1.1
(https://github.com/IRL4YOU/irl4you-pip, (c) IRL4YOU / Akinos01, Bittersweet1987) - danke!
Seit V1.112 enthaelt das Paket **keinen Code von IRL4YOU** mehr: Ersatz-Internetweg und Kennzahlen-Patch wurden
als Clean-Room-Fassung neu geschrieben (nur nach einer Funktionsbeschreibung, ohne Einsicht in den IRL4YOU-Code).

| Teil | Datei | Herkunft / Lizenz |
|---|---|---|
| Ersatz-Internetweg je Netzkarte | `irl-net-fallback-route` | IRL Streamer OS (eigene Fassung, Idee nach IRL4YOU) |
| belacoder: Kennzahlen-Datei (sonst Original) | `patches/belacoder-stats.patch` | IRL Streamer OS (eigene Fassung), GPL-3.0 wie belacoder |
| Haenger-Waechter | `irl_net_watch.py`, `irl-net-watch.service` | IRL Streamer OS, Idee nach IRL4YOU `pipbox_watch.py` |

Lizenztext: `COPYING-belacoder.txt` (GPL-3.0, gilt nur fuer belacoder und den Patch). Er geht mit dem Paket auf die Box
(`/opt/irl-net`), zusammen mit dem vollstaendigen Quelltext.

Quelltext von `belacoder-aarch64` (Version `ccce9ca-irl3`) = https://github.com/BELABOX/belacoder Commit
`ccce9ca33c8e425b33353500b95795101e847964` (liegt bei: `src/belacoder-ccce9ca.tar.gz`, per `git archive`) +
`patches/belacoder-stats.patch`. Nachbauen auf einer Belabox: `sh build-on-box.sh`.
Einstellung (Umgebung von belaUI, `/etc/systemd/system/belaUI.service.d/irl-net.conf`):
`BELACODER_STATS_FILE=/run/irl-net/belacoder.json`. Das Originalprogramm bleibt per `dpkg-divert` als
`/usr/bin/belacoder.distrib` erhalten; liefert ein BELABOX-Update einen anderen Upstream-Stand, stellt `install` automatisch das
Original wieder her.

Bewusst NICHT (mehr) dabei - Bitrate-Regler und Wegewahl bleiben BELABOX-Original:
IRL4YOU-Patches belacoder jitter-tolerant/stall-output, srtla_send latency-aware/min-share sowie die eigene Anlaufphase/
schnelle Absenkung. Live-Vergleich 25.10.2026, nur Mobilfunk (X2000 o2-LTE + Acer), je 10 min:

| | mit Patches + schneller Absenkung | mit Patches ohne | Original |
|---|---|---|---|
| Einbrueche < 800 kbit | 23 | 27 | 6 |
| Sekunden LOW / BRB (NOALBS) | 53 / 19 | 61 / 16 | 17 / 4 |
| endgueltig verlorene Pakete | 1799 | 3114 | 35 |
| X2000 / Acer im Mittel (Mbit/s) | 0,8 / 4 | 0,8 / 4 | 2,2 / 3,3 |

Ursache: die Wegewahl nach Laufzeit schob fast alles auf einen Weg; bei dessen Laufzeitspitzen fehlte der zweite.

Nicht uebernommen: das Erkennungsmodul "Datenschutz" von IRL4YOU (eigene Lizenz, fuer Unternehmen/Verkauf verboten).
