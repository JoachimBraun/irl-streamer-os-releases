#!/usr/bin/env python3
"""IRL Streamer OS - haelt logind mit einem Delay-Inhibitor-Lock auf,
bis OBS/Chrome sauber beendet sind, BEVOR das eigentliche Herunterfahren
(inkl. GDM/X11-Abschaltung) ueberhaupt beginnt.

Root-Ursache mehrerer vorheriger Fehlversuche (systemd-System-Unit mit
Conflicts=shutdown.target/After=gdm.service): egal wie die Unit-
Reihenfolge gesetzt wird, systemd-logind killt beim Session-Ende (das
zeitlich MIT dem Stoppen von gdm.service zusammenfaellt, wegen
KillUserProcesses=yes) parallel und unabhaengig davon ALLE Prozesse des
Nutzers - ein reiner Reihenfolge-Trick zwischen zwei System-Units kann
diesen Wettlauf nicht gewinnen, weil logind ausserhalb dieser Kette
eingreift (live reproduziert: OBS/Chrome bekamen abwechselnd noch
rechtzeitig SIGTERM oder wurden vom X11-Abriss ueberholt, je nach
Zeitpunkt - nie zuverlaessig).

Der von logind selbst vorgesehene Mechanismus fuer genau diesen Fall ist
ein "delay"-Inhibitor (siehe `systemd-inhibit --list`, `man
systemd-inhibit`): so lange irgendein Prozess eine Delay-Sperre auf
"shutdown" haelt, WARTET logind mit dem GESAMTEN Herunterfahren (bis zu
InhibitDelayMaxSec, Standard 30s auf dieser Distribution - unser
15s-Wartefenster passt bequem darunter), bis die Sperre freigegeben
wird. Erst DANACH beginnt logind ueberhaupt, die Session/GDM/X11 zu
beenden - der Wettlauf existiert dadurch gar nicht mehr.
"""
import os
import subprocess
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

APPS_TO_STOP = ("obs", "chrome")
WAIT_SECONDS = 15
# Nutzerwunsch/Bugfix (03.09.): das eigene Diagnose-Dashboard (Docker-
# Container "irl-diagnostics") pollt OBS alle 5s per WebSocket
# (obsws_python). Live reproduziert: wenn ausgerechnet WAEHREND OBS'
# eigenem SIGTERM-Shutdown so ein Poll-Verbindungsversuch eintrifft,
# fuehrt das zu einem Segfault im "hotkey"-Thread von libobs (kernel-
# Log: "libobs: hotkey [...]: segfault ... in libobs.so.30") - OBS
# stirbt dadurch selbst unsauber, VOELLIG UNABHAENGIG davon, wie lange
# unser Skript wartet (das Problem sitzt in OBS' eigenem Crash, nicht in
# unserem Timing). Fix: den Dashboard-Container kurz stoppen, BEVOR OBS
# ueberhaupt das SIGTERM bekommt, damit garantiert kein Poll-Zyklus mehr
# waehrend OBS' Shutdown dazwischenfunken kann.
DIAGNOSTICS_CONTAINER = "irl-diagnostics"


def log(msg):
    print(f"[irl-streamer-shutdown-inhibitor] {msg}", flush=True)


def stop_diagnostics_container():
    result = subprocess.run(
        ["docker", "stop", "--time", "5", DIAGNOSTICS_CONTAINER],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        log(f"Diagnose-Dashboard-Container ({DIAGNOSTICS_CONTAINER}) gestoppt")
    else:
        log(f"WARNUNG: Diagnose-Dashboard-Container konnte nicht gestoppt werden: {result.stderr.strip()}")


def stop_apps_gracefully():
    log("stop_apps_gracefully() gestartet")
    # ZUERST den Dashboard-Poller stoppen (siehe Kommentar bei
    # DIAGNOSTICS_CONTAINER oben) - danach kann garantiert kein
    # WebSocket-Poll mehr eintreffen, waehrend OBS sein SIGTERM bekommt.
    stop_diagnostics_container()

    pids = []
    for proc in APPS_TO_STOP:
        result = subprocess.run(
            ["pgrep", "-x", proc], capture_output=True, text=True
        )
        proc_pids = result.stdout.split()
        if proc_pids:
            pid = proc_pids[0]
            subprocess.run(["kill", "-TERM", pid])
            pids.append(pid)
            log(f"SIGTERM an {proc} (PID {pid}) gesendet")

    if not pids:
        log("keine erfassten Prozesse laufen - nichts zu tun")
        return

    # Nutzerentscheidung (03.09.): IMMER die vollen WAIT_SECONDS abwarten,
    # auch wenn alle Prozesse schon vorher als beendet erkannt werden -
    # kein fruehzeitiges Zurueckkehren mehr. Grund: Sicherheitsmarge fuer
    # Faelle, in denen ein Prozess zwar aus "pgrep"/"kill -0"-Sicht schon
    # verschwunden ist, aber intern (Dateisystem-Flush, Zustand schreiben)
    # noch nicht wirklich fertig ist - ein Totzeit-Polling auf reine
    # Prozessexistenz kann das nicht zuverlaessig unterscheiden. Lieber
    # bewusst die volle Wartezeit "verschenken" als zu frueh loslassen.
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        still_running = any(
            subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0
            for pid in pids
        )
        if not still_running:
            log("alle erfassten Prozesse sind beendet - warte trotzdem bis zum vollen Zeitfenster weiter")
        time.sleep(0.5)
    log(f"volle Wartezeit von {WAIT_SECONDS}s abgelaufen - fahre fort")


class ShutdownInhibitor:
    def __init__(self, bus, loop):
        self.bus = bus
        self.loop = loop
        self.fd = None
        self.take_lock()

    def take_lock(self):
        login_manager = self.bus.get_object(
            "org.freedesktop.login1", "/org/freedesktop/login1"
        )
        interface = dbus.Interface(login_manager, "org.freedesktop.login1.Manager")
        # "delay"-Modus (nicht "block"): logind darf den Shutdown-Vorgang
        # STARTEN und das PrepareForShutdown-Signal senden, wartet aber mit
        # dem eigentlichen Fortfahren, bis wir das Filedescriptor-Handle
        # wieder schliessen (siehe on_prepare_for_shutdown unten).
        self.fd = interface.Inhibit(
            "shutdown",
            "IRL Streamer OS",
            "OBS/Chrome vor dem Herunterfahren sauber beenden",
            "delay",
        )
        interface.connect_to_signal(
            "PrepareForShutdown", self.on_prepare_for_shutdown
        )
        log("Inhibit-Sperre aktiv, warte auf PrepareForShutdown-Signal")

    def on_prepare_for_shutdown(self, starting):
        log(f"PrepareForShutdown-Signal empfangen (starting={bool(starting)})")
        if not starting:
            # Ein abgebrochener Shutdown (Nutzer hat z.B. abgebrochen) -
            # einfach eine neue Sperre fuer den naechsten Versuch holen.
            self.take_lock()
            return
        stop_apps_gracefully()
        # Sperre freigeben (Filedescriptor schliessen) - logind faehrt ab
        # hier mit dem eigentlichen Herunterfahren/GDM-Stop fort.
        # WICHTIG: dbus-python's UnixFd.take() liefert NUR die rohe FD-
        # Nummer und uebergibt die Besitzerschaft - schliesst den FD aber
        # NICHT selbst. Ohne das explizite os.close() bliebe der FD offen
        # und logind wuerde ewig auf die Freigabe warten (Shutdown haengt).
        if self.fd is not None:
            os.close(self.fd.take())
            self.fd = None
        log("Inhibit-Sperre freigegeben")


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = GLib.MainLoop()
    # Sicherheitsnetz: falls der letzte Shutdown den Diagnostics-
    # Container per "docker stop" beendet hat (siehe stop_diagnostics_
    # container() oben), bleibt er wegen restart-policy "unless-stopped"
    # nach einem echten Reboot ansonsten dauerhaft aus - hier beim
    # eigenen Boot-Start des Inhibitor-Service (also bei jedem System-
    # start) einmalig sicherstellen, dass er wieder laeuft.
    subprocess.run(["docker", "start", DIAGNOSTICS_CONTAINER], capture_output=True)
    ShutdownInhibitor(bus, loop)
    loop.run()


if __name__ == "__main__":
    main()
