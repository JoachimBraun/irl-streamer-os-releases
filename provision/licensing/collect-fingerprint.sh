#!/usr/bin/env bash
# IRL Streamer OS - Hardware-Fingerabdruck fuer die Lizenzpruefung
#
# Zweck: Liefert einen stabilen Identifikator fuer DIESES Geraet, der eine
# Ubuntu-Neuinstallation UEBERLEBT (anders als /etc/machine-id, die bei
# jeder Installation neu erzeugt wird - waere sonst ein triviales Schlupfloch
# fuer "30-Tage-Test einfach durch Neuinstallation zuruecksetzen").
#
# Quellen (kombiniert und gehasht, damit die Rohwerte nie uebertragen/
# gespeichert werden muessen):
#   1. Root-Datentraeger-Seriennummer (ueberlebt Neuinstallation, aendert
#      sich nur bei physischem Festplattentausch)
#   2. CPU-Modellbezeichnung + Kernanzahl (aendert sich nur bei Mainboard-/
#      CPU-Tausch)
#   3. DMI-Board-Seriennummer falls vorhanden (viele Mini-PCs haben eine,
#      manche billigen Boards nicht - deshalb nicht alleine verlassen)
#
# Bewusst NICHT verwendet: MAC-Adresse (per USB-Adapter leicht wechselbar),
# /etc/machine-id (per Neuinstallation trivial aenderbar), Hostname
# (vom Nutzer frei aenderbar).
#
# Ausgabe: ein einzelner SHA-256-Hex-String auf stdout, nichts weiter.
# Bei Fehlern (fehlende Berechtigungen/Tools) wird ein Fallback auf
# /etc/machine-id verwendet, damit das Skript nie hart abbricht - das
# Sicherheitsniveau sinkt dann zwar (Neuinstallation koennte die Testphase
# zuruecksetzen), aber ein kaputtes Geraet ist schlimmer als eine
# theoretisch umgehbare Testphase.

set -euo pipefail

collect_disk_serial() {
    # Root-Geraet ermitteln, dann dessen Seriennummer per udevadm/lsblk.
    # findmnt liefert z.B. /dev/nvme0n1p2 - wir wollen das Basisgeraet
    # (nvme0n1), nicht die Partition, weil Partitionen keine eigene
    # Seriennummer haben.
    local root_part root_disk serial
    root_part="$(findmnt -no SOURCE / 2>/dev/null || true)"
    [ -z "${root_part}" ] && return 1

    root_disk="$(lsblk -no PKNAME "${root_part}" 2>/dev/null | head -1)"
    [ -z "${root_disk}" ] && root_disk="$(basename "${root_part}" | sed -E 's/p?[0-9]+$//')"

    serial="$(udevadm info --query=property --name="/dev/${root_disk}" 2>/dev/null \
        | grep -E '^ID_SERIAL_SHORT=' | cut -d= -f2)"
    [ -z "${serial}" ] && serial="$(cat "/sys/block/${root_disk}/device/serial" 2>/dev/null || true)"

    [ -n "${serial}" ] && echo "${serial}" && return 0
    return 1
}

collect_cpu_info() {
    grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs || true
    nproc 2>/dev/null || true
}

collect_board_serial() {
    # dmidecode braucht root - laeuft hier bewusst als root (Aufruf aus
    # provision.sh, das ohnehin als root laeuft).
    cat /sys/class/dmi/id/board_serial 2>/dev/null || true
}

main() {
    local disk_serial cpu_info board_serial combined

    disk_serial="$(collect_disk_serial || true)"
    cpu_info="$(collect_cpu_info)"
    board_serial="$(collect_board_serial)"

    if [ -z "${disk_serial}" ] && [ -z "${board_serial}" ]; then
        # Kein einziges stabiles Hardware-Merkmal gefunden (sehr seltene
        # virtuelle/exotische Umgebung) - Fallback auf machine-id, mit
        # Warnung auf stderr (nicht stdout, damit stdout weiterhin NUR
        # den reinen Hash enthaelt).
        echo "WARNUNG: Kein Hardware-Fingerabdruck ermittelbar, falle auf machine-id zurueck (weniger robust gegen Neuinstallation)" >&2
        combined="fallback|$(cat /etc/machine-id 2>/dev/null || echo unknown)"
    else
        combined="disk:${disk_serial}|cpu:${cpu_info}|board:${board_serial}"
    fi

    echo -n "${combined}" | sha256sum | cut -d' ' -f1
}

main "$@"
