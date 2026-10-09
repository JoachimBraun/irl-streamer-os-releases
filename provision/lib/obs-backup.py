#!/usr/bin/env python3
"""IRL Streamer OS - Cloud-Backup der OBS-Konfiguration + Medien (Nutzerwunsch 09.10.2026).

Sichert OBS-Szenen/Profile und den Ordner IRL-Uploads (Filebrowser-Uploads) auf
den Relay-Server (5 GB pro Kunde) und spielt sie nach einer Neuinstallation wieder ein.

    obs-backup.py create       Backup erstellen und hochladen
    obs-backup.py restore      Backup herunterladen und einspielen
    obs-backup.py autorestore  einmalig nach (Neu-)Installation: Backup suchen und einspielen
    obs-backup.py info         Stand auf dem Relay + lokale Groesse als JSON ausgeben

Entscheidungen des Nutzers (09.10.2026):
  - Der Twitch-Stream-Key wird NIE gesichert (profiles/*/service.json ist ausgenommen).
  - Zuordnung ueber den Slug aus der Lizenz (signierter Relay-Token), nicht ueber den
    Geraete-Fingerprint - ueberlebt damit eine Neuinstallation.
  - Profile, Szenen und Medien werden immer wiederhergestellt.
Nicht gesichert: obs-websocket-Passwort (pro Geraet generiert, sonst brechen Dashboard/NOALBS),
Logs, Crash-Dumps, Caches.

Laeuft als root (host-control bzw. systemd), nutzt nur die Python-Standardbibliothek.
"""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
import posixpath
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TARGET_USER = os.environ.get("IRL_BACKUP_USER", "streamer")
HOME_DIR = Path(os.environ.get("IRL_BACKUP_HOME", f"/home/{TARGET_USER}"))
PROJECT_DIR = Path(os.environ.get("IRL_PROJECT_DIR", "/opt/irl-streamer-os"))
STATE_DIR = Path(os.environ.get("IRL_STATE_DIR", str(PROJECT_DIR / "state")))
CONTROL_DIR = Path(os.environ.get("IRL_CONTROL_DIR", "/var/lib/irl-streamer-host-control"))
WORK_DIR = Path(os.environ.get("IRL_BACKUP_WORK_DIR", "/var/lib/irl-streamer-backup"))
RELAY_URL = os.environ.get("RELAY_PROVISIONER_URL", "https://relay.irlstreameros.de")
LICENSE_URL = os.environ.get("LICENSE_SERVER_URL", "https://lizenz.irlstreameros.de")
FINGERPRINT_SCRIPT = PROJECT_DIR / "provision" / "licensing" / "collect-fingerprint.sh"
STREAM_CHECK = PROJECT_DIR / "provision" / "systemd" / "irl-stream-active-check.sh"

OBS_DIR = HOME_DIR / ".config" / "obs-studio"
UPLOADS_DIR = HOME_DIR / "IRL-Uploads"
STATUS_FILE = CONTROL_DIR / "backup_status.json"
DONE_MARKER = STATE_DIR / "backup-restore-done"
ATTEMPTS_FILE = STATE_DIR / "backup-restore-attempts"
PRE_RESTORE_SNAPSHOT = STATE_DIR / "obs-pre-restore.tar.gz"

QUOTA_BYTES = 5 * 1024 ** 3
MAX_EXTRACT_BYTES = 25 * 1024 ** 3     # Schutz vor Zip-Bomben (komprimiert max. 5 GB)
MAX_AUTORESTORE_ATTEMPTS = 3
CHUNK = 1024 * 1024
HTTP_TIMEOUT = 60

# Was aus ~/.config/obs-studio gesichert wird (alles andere bleibt unberuehrt).
OBS_INCLUDE = ("basic/scenes", "basic/profiles")
# Dateinamen, die NIE ins Backup duerfen (Stream-Key!).
SECRET_FILES = {"service.json"}
ALLOWED_TOP = ("obs-studio", "IRL-Uploads")
MANIFEST_NAME = "backup-manifest.json"
BASIC_KEYS = ("Profile", "ProfileDir", "SceneCollection", "SceneCollectionFile")


class BackupError(Exception):
    """Fehler mit einer fuer den Kunden verstaendlichen deutschen Meldung."""


def log(msg: str) -> None:
    print(f"[irl-obs-backup] {msg}", flush=True)


# --------------------------------------------------------------------------
# Status (vom Dashboard gelesen)
# --------------------------------------------------------------------------
_last_status_write = 0.0


def write_status(state: str, action: str, message: str, percent=None, force=True) -> None:
    global _last_status_write
    now = time.monotonic()
    if not force and now - _last_status_write < 1.0:
        return
    _last_status_write = now
    data = {"state": state, "action": action, "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat()}
    if percent is not None:
        data["percent"] = int(percent)
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATUS_FILE)
    except OSError as exc:
        log(f"Status konnte nicht geschrieben werden: {exc}")


# --------------------------------------------------------------------------
# Dateiauswahl + Archiv
# --------------------------------------------------------------------------
def _skip(name: str) -> bool:
    low = name.lower()
    return name in SECRET_FILES or ".bak" in low or low.endswith(".tmp")


def collect_entries(obs_dir: Path = None, uploads_dir: Path = None) -> list:
    """Liste von (Archivname, Pfad). Symlinks und Stream-Key-Dateien werden uebersprungen."""
    obs_dir = OBS_DIR if obs_dir is None else obs_dir
    uploads_dir = UPLOADS_DIR if uploads_dir is None else uploads_dir
    out = []

    def walk(base: Path, prefix: str, roots=None):
        starts = [base / r for r in roots] if roots else [base]
        for start in starts:
            if not start.is_dir() or start.is_symlink():
                continue
            for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
                for fn in sorted(filenames):
                    p = Path(dirpath) / fn
                    if p.is_symlink() or not p.is_file() or _skip(fn):
                        continue
                    out.append((f"{prefix}/{p.relative_to(base).as_posix()}", p))

    walk(obs_dir, "obs-studio", OBS_INCLUDE)
    walk(uploads_dir, "IRL-Uploads")
    return out


def read_basic_selection(obs_dir: Path = None) -> dict:
    """Welches Profil/welche Szenensammlung in OBS gerade ausgewaehlt ist ([Basic] in global.ini)."""
    ini = (OBS_DIR if obs_dir is None else obs_dir) / "global.ini"
    sel, in_basic = {}, False
    try:
        for line in ini.read_text(errors="replace").splitlines():
            s = line.strip()
            if s.startswith("["):
                in_basic = s == "[Basic]"
            elif in_basic and "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                if k.strip() in BASIC_KEYS:
                    sel[k.strip()] = v.strip()
    except OSError:
        pass
    return sel


def merge_basic_selection(ini_text: str, sel: dict) -> str:
    """Setzt die Auswahl-Schluessel im [Basic]-Abschnitt, alles andere bleibt unveraendert."""
    lines = ini_text.splitlines()
    out, in_basic, done, seen_basic = [], False, set(), False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            if in_basic:
                out.extend(f"{k}={v}" for k, v in sel.items() if k not in done)
                done = set(sel)
            in_basic = s == "[Basic]"
            seen_basic = seen_basic or in_basic
        elif in_basic and "=" in s and not s.startswith("#"):
            k = s.split("=", 1)[0].strip()
            if k in sel:
                out.append(f"{k}={sel[k]}")
                done.add(k)
                continue
        out.append(line)
    if in_basic:
        out.extend(f"{k}={v}" for k, v in sel.items() if k not in done)
    elif not seen_basic and sel:
        out = ["[Basic]"] + [f"{k}={v}" for k, v in sel.items()] + [""] + out
    return "\n".join(out) + "\n"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def pack(entries: list, out_path: Path, slug: str = "", quota: int = None, progress=None) -> dict:
    quota = QUOTA_BYTES if quota is None else quota
    total = sum(p.stat().st_size for _, p in entries) or 1
    done = 0
    manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "slug": slug,
                "obs_selection": read_basic_selection(),
                "files": len(entries), "bytes": total,
                "excluded": sorted(SECRET_FILES)}
    media = sum(p.stat().st_size for a, p in entries if a.startswith("IRL-Uploads/"))
    with tarfile.open(out_path, "w:gz", compresslevel=3) as tar:
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size, info.mtime, info.mode = len(raw), int(time.time()), 0o644
        import io
        tar.addfile(info, io.BytesIO(raw))
        for arc, path in entries:
            try:
                tar.add(str(path), arcname=arc, recursive=False)
            except OSError as exc:
                log(f"Uebersprungen (nicht lesbar): {path}: {exc}")
                continue
            done += path.stat().st_size if path.exists() else 0
            if out_path.stat().st_size > quota - CHUNK:
                raise BackupError(
                    f"Das Backup waere groesser als das Kontingent von {human(quota)} "
                    f"(Medien in IRL-Uploads: {human(media)}). Bitte grosse Dateien aus IRL-Uploads "
                    f"entfernen und erneut sichern.")
            if progress:
                progress(done / total)
    return manifest


def _safe_member_path(name: str) -> str:
    norm = posixpath.normpath(name)
    parts = norm.split("/")
    if (name.startswith("/") or norm.startswith("..") or ".." in parts or "\\" in name
            or "\x00" in name):
        raise BackupError(f"Unsicherer Pfad im Backup: {name!r}")
    return norm


def safe_extract(archive: Path, dest: Path) -> dict:
    """Entpackt nur normale Dateien/Ordner unter obs-studio/ und IRL-Uploads/. Gibt das Manifest zurueck."""
    manifest = None
    total = 0
    free = shutil.disk_usage(dest).free
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar:
            name = _safe_member_path(m.name)
            if name == MANIFEST_NAME:
                f = tar.extractfile(m)
                try:
                    manifest = json.loads(f.read(1024 * 1024).decode())
                except Exception:
                    raise BackupError("Backup-Manifest ist unlesbar")
                continue
            if name.split("/")[0] not in ALLOWED_TOP:
                raise BackupError(f"Unerwarteter Eintrag im Backup: {name!r}")
            if m.isdir():
                (dest / name).mkdir(parents=True, exist_ok=True)
                continue
            if not m.isreg():
                raise BackupError(f"Nicht erlaubter Dateityp im Backup: {name!r}")
            if posixpath.basename(name) in SECRET_FILES:
                continue  # selbst wenn jemand eine service.json einschmuggelt: nie einspielen
            total += m.size
            if total > MAX_EXTRACT_BYTES or total > free - 512 * 1024 ** 2:
                raise BackupError("Nicht genug Speicherplatz zum Entpacken des Backups")
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            os.chmod(target, 0o644)
    if not manifest or manifest.get("version") != 1:
        raise BackupError("Das ist kein gueltiges IRL-Streamer-OS-Backup")
    return manifest


# --------------------------------------------------------------------------
# Relay-Zugriff (Token vom Lizenzserver, Backup-API am Relay)
# --------------------------------------------------------------------------
def _json_post(url: str, body: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise BackupError(f"Lizenzserver lehnt ab (HTTP {exc.code}) - ist die Lizenz aktiv?")
    except Exception as exc:
        raise BackupError(f"Lizenzserver nicht erreichbar: {exc}")


def get_auth() -> tuple:
    """(Header, Slug) - der Slug stammt aus dem signierten Token, nicht vom Geraet."""
    if not FINGERPRINT_SCRIPT.exists():
        raise BackupError("Fingerprint-Skript nicht gefunden")
    fp = subprocess.run(["bash", str(FINGERPRINT_SCRIPT)], capture_output=True, text=True,
                        timeout=30).stdout.strip()
    if not fp:
        raise BackupError("Geraete-Fingerprint konnte nicht ermittelt werden")
    tok = _json_post(f"{LICENSE_URL}/relay/token", {"device_fingerprint": fp})
    try:
        headers = {"X-Device-Fingerprint": tok["device_fingerprint"],
                   "X-Token-Payload": tok["payload"], "X-Token-Signature": tok["signature"]}
        slug = tok.get("subdomain_slug") or tok["payload"].split("|")[3]
    except (KeyError, IndexError):
        raise BackupError("Lizenzserver lieferte keinen gueltigen Token")
    return headers, slug


def _conn(timeout: int = HTTP_TIMEOUT):
    u = urllib.parse.urlparse(RELAY_URL)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout)


def _detail(body: bytes) -> str:
    try:
        return str(json.loads(body).get("detail", ""))
    except Exception:
        return body[:200].decode(errors="replace")


def relay_info(headers: dict) -> dict:
    c = _conn(15)
    try:
        c.request("GET", "/backup/info", headers=headers)
        r = c.getresponse()
        body = r.read()
    except OSError as exc:
        raise BackupError(f"Relay-Server nicht erreichbar: {exc}")
    finally:
        c.close()
    if r.status != 200:
        raise BackupError(f"Relay lehnt ab (HTTP {r.status}): {_detail(body)}")
    return json.loads(body)


def upload(path: Path, headers: dict, progress=None) -> None:
    size = path.stat().st_size
    c = _conn()
    try:
        c.putrequest("PUT", "/backup")
        for k, v in headers.items():
            c.putheader(k, v)
        c.putheader("Content-Type", "application/gzip")
        c.putheader("Content-Length", str(size))
        c.endheaders()
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                c.send(chunk)
                sent += len(chunk)
                if progress:
                    progress(sent / size)
        r = c.getresponse()
        body = r.read()
    except OSError as exc:
        raise BackupError(f"Upload unterbrochen: {exc}")
    finally:
        c.close()
    if r.status == 413:
        raise BackupError("Das Backup ist groesser als das Kontingent von 5 GB")
    if r.status != 200:
        raise BackupError(f"Upload abgelehnt (HTTP {r.status}): {_detail(body)}")


def download(dest: Path, headers: dict, progress=None) -> None:
    c = _conn()
    try:
        c.request("GET", "/backup", headers=headers)
        r = c.getresponse()
        if r.status == 404:
            raise BackupError("Auf dem Server liegt noch kein Backup")
        if r.status != 200:
            raise BackupError(f"Download abgelehnt (HTTP {r.status}): {_detail(r.read())}")
        total = int(r.getheader("Content-Length") or 0)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(got / total)
        if total and got != total:
            raise BackupError("Download unvollstaendig")
    except OSError as exc:
        raise BackupError(f"Download unterbrochen: {exc}")
    finally:
        c.close()


# --------------------------------------------------------------------------
# Host: Stream-Erkennung, OBS-Steuerung, Rechte
# --------------------------------------------------------------------------
def stream_idle() -> bool:
    """True nur, wenn sicher KEIN Stream laeuft (unklar = wie aktiv behandeln, wie im Connectivity-Client)."""
    try:
        return subprocess.run(["bash", str(STREAM_CHECK)], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def obs_running() -> bool:
    return subprocess.run(["pgrep", "-x", "obs"], capture_output=True).returncode == 0


def _as_streamer(args: list) -> subprocess.CompletedProcess:
    uid = pwd.getpwnam(TARGET_USER).pw_uid
    return subprocess.run(
        ["sudo", "-u", TARGET_USER, "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
         f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"] + args,
        capture_output=True, text=True, timeout=30)


def obs_stop() -> None:
    _as_streamer(["systemctl", "--user", "stop", "irl-streamer-obs.service"])
    time.sleep(1)
    if obs_running():
        _as_streamer(["pkill", "-TERM", "-x", "obs"])
        time.sleep(3)
    if obs_running():
        raise BackupError("OBS liess sich nicht beenden - Wiederherstellung abgebrochen")


def obs_start() -> None:
    _as_streamer(["systemctl", "--user", "reset-failed", "irl-streamer-obs.service"])
    _as_streamer(["systemctl", "--user", "start", "irl-streamer-obs.service"])


def _chown_tree(path: Path) -> None:
    try:
        pw = pwd.getpwnam(TARGET_USER)
    except KeyError:
        return
    for dirpath, dirnames, filenames in os.walk(path):
        for n in [dirpath] + [os.path.join(dirpath, x) for x in dirnames + filenames]:
            try:
                os.chown(n, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
            except OSError:
                pass


def _copy_merge(src: Path, dst: Path) -> int:
    n = 0
    for dirpath, _dirs, files in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for fn in files:
            shutil.copy2(Path(dirpath) / fn, dst / rel / fn)
            n += 1
    return n


class Lock:
    def __init__(self):
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(WORK_DIR / "lock", "w")

    def acquire(self) -> bool:
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def _ensure_space(need: int) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(WORK_DIR).free < need + 1024 ** 3:
        raise BackupError("Zu wenig freier Speicherplatz fuer das Backup auf diesem PC")


# --------------------------------------------------------------------------
# Aktionen
# --------------------------------------------------------------------------
def do_create() -> None:
    write_status("running", "create", "Pruefe Voraussetzungen...", 0)
    if not stream_idle():
        raise BackupError("Es laeuft gerade ein Stream - bitte nach dem Stream sichern (Upload wuerde die Verbindung belasten).")
    headers, slug = get_auth()
    entries = collect_entries()
    if not entries:
        raise BackupError("Es gibt nichts zu sichern (keine OBS-Szenen/Profile gefunden)")
    est = sum(p.stat().st_size for _, p in entries)
    _ensure_space(min(est, QUOTA_BYTES))
    tmp = WORK_DIR / f"backup-{os.getpid()}.tar.gz"
    try:
        write_status("running", "create", f"Packe {len(entries)} Dateien ({human(est)})...", 0)
        manifest = pack(entries, tmp, slug=slug,
                        progress=lambda f: write_status("running", "create", "Packe Dateien...", f * 50, force=False))
        size = tmp.stat().st_size
        write_status("running", "create", f"Lade {human(size)} hoch...", 50)
        upload(tmp, headers, progress=lambda f: write_status(
            "running", "create", f"Lade {human(size)} hoch...", 50 + f * 50, force=False))
    finally:
        tmp.unlink(missing_ok=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DONE_MARKER.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "reason": "create"}))
    except OSError:
        pass
    write_status("ok", "create", f"Backup gesichert ({manifest['files']} Dateien, {human(size)})", 100)
    log(f"Backup gesichert: {manifest['files']} Dateien, {size} Bytes")


def do_restore(auto: bool = False) -> bool:
    """True = eingespielt. Bei auto=True und fehlendem Backup: False statt Fehler."""
    action = "autorestore" if auto else "restore"
    write_status("running", action, "Pruefe Voraussetzungen...", 0)
    if not stream_idle():
        raise BackupError("Es laeuft gerade ein Stream - bitte nach dem Stream wiederherstellen.")
    headers, _slug = get_auth()
    info = relay_info(headers)
    if not info.get("exists"):
        if auto:
            return False
        raise BackupError("Auf dem Server liegt noch kein Backup")
    _ensure_space(int(info.get("size", 0)) * 3)
    tmp = WORK_DIR / f"restore-{os.getpid()}.tar.gz"
    staging = Path(tempfile.mkdtemp(prefix="stage-", dir=WORK_DIR))
    try:
        write_status("running", action, f"Lade Backup herunter ({human(info.get('size', 0))})...", 0)
        download(tmp, headers, progress=lambda f: write_status(
            "running", action, "Lade Backup herunter...", f * 40, force=False))
        write_status("running", action, "Pruefe und entpacke Backup...", 40)
        manifest = safe_extract(tmp, staging)
        tmp.unlink(missing_ok=True)

        was_running = obs_running()
        write_status("running", action, "Sichere aktuellen Stand und beende OBS...", 70)
        _snapshot_current()
        if was_running:
            obs_stop()
        write_status("running", action, "Spiele Dateien ein...", 80)
        n = 0
        if (staging / "obs-studio").is_dir():
            n += _copy_merge(staging / "obs-studio", OBS_DIR)
            _chown_tree(OBS_DIR)
        if (staging / "IRL-Uploads").is_dir():
            n += _copy_merge(staging / "IRL-Uploads", UPLOADS_DIR)
            _chown_tree(UPLOADS_DIR)
        _apply_selection(manifest.get("obs_selection") or {})
        if was_running:
            obs_start()
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DONE_MARKER.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "reason": action,
                                           "backup_created_at": manifest.get("created_at")}))
    except OSError:
        pass
    write_status("ok", action, f"Backup vom {manifest.get('created_at', '?')[:10]} eingespielt ({n} Dateien)", 100)
    log(f"Backup eingespielt: {n} Dateien")
    return True


def _snapshot_current() -> None:
    """Kleine Sicherheitskopie der aktuellen OBS-Config (ohne Medien), falls die Wiederherstellung nicht gefaellt."""
    entries = collect_entries(uploads_dir=Path("/nonexistent"))
    if not entries:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pack(entries, PRE_RESTORE_SNAPSHOT, quota=QUOTA_BYTES)


def _apply_selection(sel: dict) -> None:
    """Profil/Szenensammlung wieder auswaehlen - aber nur, wenn die Dateien auch existieren."""
    if not sel:
        return
    ok = {}
    if sel.get("SceneCollectionFile") and (OBS_DIR / "basic/scenes" / f"{sel['SceneCollectionFile']}.json").exists():
        ok["SceneCollectionFile"] = sel["SceneCollectionFile"]
        if sel.get("SceneCollection"):
            ok["SceneCollection"] = sel["SceneCollection"]
    if sel.get("ProfileDir") and (OBS_DIR / "basic/profiles" / sel["ProfileDir"]).is_dir():
        ok["ProfileDir"] = sel["ProfileDir"]
        if sel.get("Profile"):
            ok["Profile"] = sel["Profile"]
    if not ok:
        return
    ini = OBS_DIR / "global.ini"
    text = ini.read_text(errors="replace") if ini.exists() else ""
    ini.write_text(merge_basic_selection(text, ok))
    _chown_tree(ini)


def do_autorestore() -> int:
    """Einmalig nach Neuinstallation. Marker verhindert jede Wiederholung (auch nach Updates)."""
    if DONE_MARKER.exists():
        return 0
    n = int(ATTEMPTS_FILE.read_text() or 0) if ATTEMPTS_FILE.exists() else 0
    try:
        if not stream_idle():
            return 0
        headers, _slug = get_auth()
        info = relay_info(headers)
    except BackupError as exc:
        log(f"Autorestore: noch nicht moeglich ({exc}) - naechster Versuch spaeter")
        return 0
    if not info.get("exists"):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DONE_MARKER.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "reason": "kein Backup vorhanden"}))
        log("Autorestore: kein Backup auf dem Server - nichts zu tun")
        return 0
    try:
        do_restore(auto=True)
    except BackupError as exc:
        n += 1
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ATTEMPTS_FILE.write_text(str(n))
        write_status("error", "autorestore", str(exc))
        log(f"Autorestore fehlgeschlagen ({n}/{MAX_AUTORESTORE_ATTEMPTS}): {exc}")
        if n >= MAX_AUTORESTORE_ATTEMPTS:
            DONE_MARKER.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                               "reason": f"aufgegeben: {exc}"}))
        return 0
    return 0


def do_info() -> dict:
    out = {"ok": True, "quota_bytes": QUOTA_BYTES, "remote": None, "error": None}
    try:
        entries = collect_entries()
        out["local_bytes"] = sum(p.stat().st_size for _, p in entries)
        out["local_files"] = len(entries)
    except OSError:
        out["local_bytes"], out["local_files"] = 0, 0
    try:
        headers, _slug = get_auth()
        out["remote"] = relay_info(headers)
    except BackupError as exc:
        out["error"] = str(exc)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["create", "restore", "autorestore", "info"])
    args = ap.parse_args(argv)

    if args.action == "info":
        print(json.dumps(do_info()))
        return 0

    lock = Lock()
    if not lock.acquire():
        if args.action == "autorestore":
            return 0
        write_status("error", args.action, "Es laeuft bereits ein Backup/eine Wiederherstellung")
        return 1
    try:
        if args.action == "autorestore":
            return do_autorestore()
        if args.action == "create":
            do_create()
        else:
            do_restore()
        return 0
    except BackupError as exc:
        log(f"FEHLER: {exc}")
        write_status("error", args.action, str(exc))
        return 1
    except Exception as exc:  # unerwartet - Kunde soll trotzdem eine Meldung sehen
        log(f"UNERWARTETER FEHLER: {exc!r}")
        write_status("error", args.action, f"Unerwarteter Fehler: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
