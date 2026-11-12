#!/usr/bin/env python3
"""OBS-Einstellungen des Kunden bei Updates erhalten (Review 11.11.2026, Befund A2).

provision.sh laeuft bei JEDER Neuinstallation UND bei jedem Update/Rollback. Bis V1.114 schrieb es dabei
die Szenen-Sammlung, basic.ini und global.ini jedes Mal komplett neu - Aufloesung/FPS, Hotkeys,
Aufnahmepfad und eigene Szenen des Kunden waren nach dem Update weg.

    obs-config-merge.py ini <datei> <vorlage>
        Datei fehlt/leer -> Vorlage unveraendert uebernehmen (Neuinstallation wie bisher).
        Datei vorhanden  -> nur FEHLENDE Abschnitte/Schluessel aus der Vorlage ergaenzen. Vorhandene Werte,
                            Reihenfolge, Gross-/Kleinschreibung und unbekannte Schluessel bleiben Byte fuer Byte.
        Ausgabe: NEU | UNVERAENDERT | ERGAENZT <[Abschnitt] Schluessel, ...>

    obs-config-merge.py sichern <obs-ordner> <sicherungs-ordner> [--behalten N]
        Zeitgestempelte Sicherung (tar.gz) von basic/ + global.ini + user.ini, OHNE service.json
        (Stream-Key). Nur die N neuesten bleiben (Standard 5). Ohne <obs-ordner>/basic passiert nichts.

Bewusst zeilenbasiert statt configparser (Gegenpruefung X1: configparser macht Schluessel klein und
bricht an '%' in Pfaden). Nur Python-Standardbibliothek. Exit 1 bei Fehlern - der Aufrufer loggt eine
WARNUNG, provision.sh bricht deshalb nie ab.
"""
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SECRET_FILES = {"service.json"}           # Stream-Key - wie obs-backup.py nie sichern
BACKUP_PREFIX = "obs-"
BACKUP_SUFFIX = ".tar.gz"
MAX_BACKUP_BYTES = 50 * 1024 * 1024       # Schutz gegen volle Platte (Szenen/Profile sind wenige KB)


def _section_of(line: str):
    s = line.strip().lstrip("\ufeff")
    if s.startswith("[") and s.endswith("]"):
        return s[1:-1].strip()
    return None


def _key_of(line: str):
    s = line.strip().lstrip("\ufeff")
    if not s or s[0] in "#;[" or "=" not in s:
        return None
    return s.split("=", 1)[0].strip()


def parse_template(text: str) -> list:
    """[(abschnitt, [(schluessel, zeile), ...]), ...] in Vorlagen-Reihenfolge."""
    out, cur = [], None
    for line in text.splitlines():
        sec = _section_of(line)
        if sec is not None:
            cur = (sec, [])
            out.append(cur)
        elif cur is not None:
            key = _key_of(line)
            if key is not None:
                cur[1].append((key, line.strip()))
    return out


def merge_missing(existing: str, template: str):
    """Ergaenzt fehlende Schluessel/Abschnitte. Rueckgabe (neuer_text, [ergaenzte '[Abschnitt] Schluessel'])."""
    nl = "\r\n" if "\r\n" in existing else "\n"
    lines = existing.splitlines(keepends=True)
    keys, last_line = {}, {}          # Abschnitt -> vorhandene Schluessel / Index der letzten Inhaltszeile
    cur = None
    for i, line in enumerate(lines):
        sec = _section_of(line)
        if sec is not None:
            cur = sec
            keys.setdefault(cur, set())
            last_line[cur] = i
            continue
        if cur is None:
            continue
        key = _key_of(line)
        if key is not None:
            keys[cur].add(key)
        if line.strip():
            last_line[cur] = i

    inserts, appended, added = {}, [], []
    for sec, items in parse_template(template):
        missing = [(k, ln) for k, ln in items if k not in keys.get(sec, set())]
        if not missing:
            continue
        added.extend(f"[{sec}] {k}" for k, _ in missing)
        if sec in keys:
            inserts.setdefault(last_line[sec], []).extend(ln for _, ln in missing)
        else:
            appended.append((sec, [ln for _, ln in missing]))
            keys[sec] = {k for k, _ in missing}
    if not added:
        return existing, []

    out = []
    for i, line in enumerate(lines):
        if i in inserts and not line.endswith(("\n", "\r")):
            line += nl
        out.append(line)
        out.extend(ln + nl for ln in inserts.get(i, []))
    text = "".join(out)
    for sec, sec_lines in appended:
        if text and not text.endswith("\n"):
            text += nl
        if text and not text.endswith(nl + nl) and text.strip():
            text += nl
        text += f"[{sec}]{nl}" + "".join(ln + nl for ln in sec_lines)
    return text, added


def _atomic_write(path: Path, text: str) -> None:
    st = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if st is not None:
            os.chmod(tmp, st.st_mode & 0o7777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        else:  # neue Datei: Rechte wie bisher 'cat >' (0666 minus umask), nicht mkstemps 0600
            mask = os.umask(0)
            os.umask(mask)
            os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_ini(target: Path, template: Path) -> str:
    tpl = template.read_text(encoding="utf-8")
    if target.is_symlink():
        raise OSError(f"{target} ist ein Symlink - nicht angefasst")
    if not target.exists() or target.stat().st_size == 0:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, tpl)
        return "NEU"
    old = target.read_text(encoding="utf-8", errors="surrogateescape")
    new, added = merge_missing(old, tpl)
    if not added:
        return "UNVERAENDERT"
    _atomic_write(target, new)
    return "ERGAENZT " + ", ".join(added)


def _backup_members(obs_dir: Path) -> list:
    out = []
    for name in ("global.ini", "user.ini"):
        p = obs_dir / name
        if p.is_file() and not p.is_symlink():
            out.append(p)
    basic = obs_dir / "basic"
    for root, dirs, files in os.walk(basic):          # folgt keinen Symlinks
        dirs.sort()
        for fn in sorted(files):
            p = Path(root) / fn
            # auch OBS-Kopien wie service.json.bak/.tmp enthalten den Stream-Key
            # (live auf der Test-VM gefunden, 11.11.2026) - wie obs-backup.py nie sichern
            if any(fn == s or fn.startswith(s + ".") for s in SECRET_FILES) \
                    or p.is_symlink() or not p.is_file():
                continue
            out.append(p)
    return out


def cmd_sichern(obs_dir: Path, backup_dir: Path, keep: int) -> str:
    if not (obs_dir / "basic").is_dir():
        return "KEINE (noch keine OBS-Konfiguration)"
    members = _backup_members(obs_dir)
    if not members:
        return "KEINE (noch keine OBS-Konfiguration)"
    total = sum(p.stat().st_size for p in members)
    if total > MAX_BACKUP_BYTES:
        raise OSError(f"OBS-Konfiguration zu gross fuer die lokale Sicherung ({total} Bytes)")
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    n = 1
    while dest.exists():
        n += 1
        dest = backup_dir / f"{BACKUP_PREFIX}{stamp}-{n}{BACKUP_SUFFIX}"
    tmp = dest.with_name(dest.name + ".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        for p in members:
            tar.add(str(p), arcname=str(Path("obs-studio") / p.relative_to(obs_dir)), recursive=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    old = sorted(backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"), key=lambda p: p.stat().st_mtime)
    for p in old[:-keep] if keep > 0 else []:
        p.unlink(missing_ok=True)
    return str(dest)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(argv) == 3 and argv[0] == "ini":
            print(cmd_ini(Path(argv[1]), Path(argv[2])))
            return 0
        if len(argv) in (3, 5) and argv[0] == "sichern":
            keep = 5
            if len(argv) == 5:
                if argv[3] != "--behalten":
                    raise ValueError(argv[3])
                keep = max(1, int(argv[4]))
            print(cmd_sichern(Path(argv[1]), Path(argv[2]), keep))
            return 0
    except (OSError, ValueError, tarfile.TarError) as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
