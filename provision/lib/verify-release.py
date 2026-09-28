#!/usr/bin/env python3
"""
IRL Streamer OS - Pruefung eines signierten Release-Payloads (V1.85+)

Wird von irl-streamer-update-check.sh aufgerufen, BEVOR irgendetwas an der
laufenden Installation veraendert wird. Aufruf IMMER aus der bereits
installierten (= frueher schon geprueften bzw. per ISO gelieferten) Kopie
unter /opt/irl-streamer-os/provision/lib/ - NIE aus dem frisch geklonten
Release, sonst koennte ein manipuliertes Release seinen eigenen Pruefer
mitbringen.

Release-Format (Wurzel des oeffentlichen Release-Repos):
  RELEASE_MANIFEST.json      {"version":"1.85","files":{"<relpfad>":"<sha256>",...}}
                             kanonisches JSON (sort_keys, separators=(",",":"))
  RELEASE_MANIFEST.json.sig  Base64 der Ed25519-Signatur ueber die EXAKTEN
                             Bytes von RELEASE_MANIFEST.json

Vertrauensanker: der Release-Public-Key aus /etc/irl-streamer-os/
release-pubkey.b64 (root-owned, liegt AUSSERHALB des Update-rsync-Ziels,
wird von provision.sh nur angelegt, wenn er fehlt) - Fallback ist die
unten fest eingetragene Konstante DIESER (installierten) Datei. Ein
Angreifer mit Schreibzugriff auf das Release-Repo kann den Schluessel also
nicht per Update austauschen.

Aufruf:
  verify-release.py --release-dir DIR --expected-version 1.85 [--pubkey-file F]
Exit 0 = alles gueltig, 1 = ungueltig (Grund auf stderr, eine Zeile).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys

# Oeffentlicher Release-Signierschluessel (Ed25519, raw, base64). Der
# private Teil liegt NUR offline beim Betreiber (scripts/sign-release.py).
RELEASE_PUBKEY_B64 = "PA/gDYFwYXInDANtkiqHB+1/u53vuWEDtkf7WiXVXQA="
DEFAULT_PUBKEY_FILE = "/etc/irl-streamer-os/release-pubkey.b64"

PAYLOAD_DIRS = ("provision", "docker", "remote-access")
PAYLOAD_FILES = ("VERSION",)
MANIFEST_NAME = "RELEASE_MANIFEST.json"
SIG_NAME = "RELEASE_MANIFEST.json.sig"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class VerifyError(Exception):
    pass


def _ignored(relpath: str) -> bool:
    parts = relpath.split("/")
    return "__pycache__" in parts or relpath.endswith(".pyc")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_payload_files(root: str) -> list:
    """Alle Payload-Dateien (relativ, '/'-getrennt, sortiert). Symlinks und
    Sonderdateien sind im Payload NICHT erlaubt -> VerifyError."""
    out = []
    for d in PAYLOAD_DIRS:
        base = os.path.join(root, d)
        if os.path.islink(base):
            raise VerifyError(f"Symlink im Payload nicht erlaubt: {d}")
        if not os.path.isdir(base):
            continue
        for cur, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for name in list(dirs):
                if os.path.islink(os.path.join(cur, name)):
                    rel = os.path.relpath(os.path.join(cur, name), root).replace(os.sep, "/")
                    raise VerifyError(f"Symlink im Payload nicht erlaubt: {rel}")
            for name in files:
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if _ignored(rel):
                    continue
                st = os.lstat(full)
                if not stat.S_ISREG(st.st_mode):
                    raise VerifyError(f"Keine regulaere Datei im Payload: {rel}")
                out.append(rel)
    for f in PAYLOAD_FILES:
        full = os.path.join(root, f)
        if os.path.lexists(full):
            if not stat.S_ISREG(os.lstat(full).st_mode):
                raise VerifyError(f"Keine regulaere Datei im Payload: {f}")
            out.append(f)
    return sorted(out)


def build_manifest(root: str, version: str, files=None) -> dict:
    if not VERSION_RE.match(version):
        raise VerifyError(f"Ungueltige Version: {version!r}")
    files = list_payload_files(root) if files is None else sorted(files)
    return {"version": version,
            "files": {rel: sha256_file(os.path.join(root, rel)) for rel in files}}


def canonical_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_pubkey(pubkey_file: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    b64 = None
    if pubkey_file and os.path.isfile(pubkey_file):
        with open(pubkey_file) as f:
            b64 = f.read().strip()
    if not b64:
        b64 = RELEASE_PUBKEY_B64
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(b64))


def _safe_relpath(rel: str) -> bool:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return rel in PAYLOAD_FILES or parts[0] in PAYLOAD_DIRS


def verify_release(release_dir: str, expected_version: str, pubkey) -> dict:
    from cryptography.exceptions import InvalidSignature

    if not VERSION_RE.match(expected_version or ""):
        raise VerifyError(f"Ungueltige erwartete Version: {expected_version!r}")

    mpath = os.path.join(release_dir, MANIFEST_NAME)
    spath = os.path.join(release_dir, SIG_NAME)
    if not os.path.isfile(mpath) or not os.path.isfile(spath):
        raise VerifyError("Release ist nicht signiert (Manifest oder Signatur fehlt)")

    with open(mpath, "rb") as f:
        raw = f.read()
    with open(spath) as f:
        sig_b64 = f.read().strip()
    try:
        sig = base64.b64decode(sig_b64, validate=True)
        pubkey.verify(sig, raw)
    except (InvalidSignature, ValueError):
        raise VerifyError("Signatur des Release-Manifests ungueltig")

    try:
        manifest = json.loads(raw)
    except ValueError:
        raise VerifyError("Release-Manifest ist kein gueltiges JSON")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise VerifyError("Release-Manifest hat ein unerwartetes Format")
    if manifest.get("version") != expected_version:
        raise VerifyError(
            f"Versionskonflikt: Manifest {manifest.get('version')!r} != erwartet {expected_version!r}")

    files = manifest["files"]
    for rel, digest in files.items():
        if not _safe_relpath(rel):
            raise VerifyError(f"Unzulaessiger Pfad im Manifest: {rel!r}")
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise VerifyError(f"Ungueltiger Hash im Manifest fuer {rel}")

    actual = list_payload_files(release_dir)
    extra = sorted(set(actual) - set(files))
    missing = sorted(set(files) - set(actual))
    if extra:
        raise VerifyError(f"Datei(en) ausserhalb des Manifests: {', '.join(extra[:5])}")
    if missing:
        raise VerifyError(f"Datei(en) aus dem Manifest fehlen: {', '.join(missing[:5])}")
    for rel in actual:
        if sha256_file(os.path.join(release_dir, rel)) != files[rel]:
            raise VerifyError(f"Pruefsumme stimmt nicht: {rel}")

    if "VERSION" not in files:
        raise VerifyError("VERSION-Datei fehlt im Manifest")
    with open(os.path.join(release_dir, "VERSION")) as f:
        if f.read().strip() != expected_version:
            raise VerifyError("VERSION-Datei passt nicht zur Manifest-Version")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Signiertes Release pruefen")
    ap.add_argument("--release-dir", required=True)
    ap.add_argument("--expected-version", required=True)
    ap.add_argument("--pubkey-file", default=DEFAULT_PUBKEY_FILE)
    args = ap.parse_args(argv)
    try:
        pubkey = load_pubkey(args.pubkey_file)
        m = verify_release(args.release_dir, args.expected_version, pubkey)
    except VerifyError as exc:
        print(f"UNGUELTIG: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - jeder Fehler = ungueltig (fail closed)
        print(f"UNGUELTIG: Pruefung fehlgeschlagen ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    print(f"OK: Release {m['version']} gueltig signiert, {len(m['files'])} Dateien geprueft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
