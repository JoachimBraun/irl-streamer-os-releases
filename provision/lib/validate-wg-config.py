#!/usr/bin/env python3
"""
IRL Streamer OS - Pruefung der WireGuard-Config vom Relay-Server (V1.85)

wg-quick fuehrt PreUp/PostUp/PreDown/PostDown als root-Shellbefehle aus -
eine ungefilterte Config vom Relay waere damit root auf jedem Geraet.
Dieses Skript liest die Config (stdin), prueft sie gegen eine strikte
Whitelist und gibt NUR die erlaubten Zeilen normalisiert auf stdout aus.

Erlaubt:  [Interface]: PrivateKey, Address, MTU, ListenPort
          [Peer]:      PublicKey, PresharedKey, Endpoint, AllowedIPs,
                       PersistentKeepalive
Abgelehnt (Exit 1, Grund auf stderr): jeder andere Schluessel (PreUp,
PostUp, PreDown, PostDown, Table, SaveConfig, FwMark, DNS, ...), andere
Sektionen, AllowedIPs/Address ausserhalb 10.8.0.0/24, Endpoint nicht
relay.irlstreameros.de (bzw. dessen aktuelle IP), mehr als ein Peer.
Kommentar- und Leerzeilen werden verworfen.

Aufruf: validate-wg-config.py [--endpoint-host H] < roh.conf > wg-relay.conf
"""
import argparse
import ipaddress
import re
import socket
import sys

RELAY_NET = ipaddress.ip_network("10.8.0.0/24")
DEFAULT_HOST = "relay.irlstreameros.de"
KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")

ALLOWED = {
    "interface": ("PrivateKey", "Address", "MTU", "ListenPort"),
    "peer": ("PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"),
}


class Invalid(Exception):
    pass


def _nets(value, what):
    out = []
    for part in value.split(","):
        part = part.strip()
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise Invalid(f"{what}: ungueltiges Netz {part!r}")
        if net.version != 4 or not net.subnet_of(RELAY_NET):
            raise Invalid(f"{what} ausserhalb {RELAY_NET}: {part}")
        out.append(part)
    if not out:
        raise Invalid(f"{what} leer")
    return ", ".join(out)


def _int(value, what, lo, hi):
    if not re.fullmatch(r"[0-9]{1,5}", value) or not lo <= int(value) <= hi:
        raise Invalid(f"{what} ungueltig: {value!r}")
    return value


def _endpoint(value, allowed_hosts):
    m = re.fullmatch(r"([A-Za-z0-9.-]+):([0-9]{1,5})", value)
    if not m:
        raise Invalid(f"Endpoint ungueltig: {value!r}")
    host, port = m.group(1).lower(), m.group(2)
    _int(port, "Endpoint-Port", 1, 65535)
    if host not in allowed_hosts:
        raise Invalid(f"Endpoint-Host nicht erlaubt: {host}")
    return f"{host}:{port}"


def resolve_hosts(host):
    hosts = {host.lower()}
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            hosts.add(info[4][0])
    except OSError:
        pass
    return hosts


def validate(text, allowed_hosts):
    section = None
    seen = {"interface": 0, "peer": 0}
    out = []
    keys_in_section = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            name = line.lower()
            if name == "[interface]":
                section = "interface"
            elif name == "[peer]":
                section = "peer"
            else:
                raise Invalid(f"Unbekannte Sektion: {line}")
            seen[section] += 1
            keys_in_section = set()
            if out:
                out.append("")
            out.append("[Interface]" if section == "interface" else "[Peer]")
            continue
        if section is None:
            raise Invalid("Eintrag ausserhalb einer Sektion")
        if "=" not in line:
            raise Invalid(f"Zeile ohne '=': {line[:40]!r}")
        key, value = (x.strip() for x in line.split("=", 1))
        canon = {k.lower(): k for k in ALLOWED[section]}.get(key.lower())
        if canon is None:
            raise Invalid(f"Schluessel nicht erlaubt in [{section}]: {key}")
        if canon in keys_in_section:
            raise Invalid(f"Schluessel doppelt: {canon}")
        keys_in_section.add(canon)
        if any(c in value for c in "\n\r\0"):
            raise Invalid("Steuerzeichen im Wert")
        if canon in ("PrivateKey", "PublicKey", "PresharedKey"):
            if not KEY_RE.match(value):
                raise Invalid(f"{canon} hat kein gueltiges Schluesselformat")
        elif canon == "Address":
            value = _nets(value, "Address")
        elif canon == "AllowedIPs":
            value = _nets(value, "AllowedIPs")
        elif canon == "MTU":
            value = _int(value, "MTU", 576, 9000)
        elif canon == "ListenPort":
            value = _int(value, "ListenPort", 1, 65535)
        elif canon == "PersistentKeepalive":
            value = _int(value, "PersistentKeepalive", 0, 65535)
        elif canon == "Endpoint":
            value = _endpoint(value, allowed_hosts)
        out.append(f"{canon} = {value}")
    if seen["interface"] != 1 or seen["peer"] != 1:
        raise Invalid("Genau eine [Interface]- und eine [Peer]-Sektion erwartet")
    text_out = "\n".join(out)
    for required in ("PrivateKey", "Address", "PublicKey", "Endpoint", "AllowedIPs"):
        if f"\n{required} = " not in "\n" + text_out:
            raise Invalid(f"Pflichtfeld fehlt: {required}")
    return text_out + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint-host", default=DEFAULT_HOST)
    args = ap.parse_args(argv)
    try:
        sys.stdout.write(validate(sys.stdin.read(), resolve_hosts(args.endpoint_host)))
    except Invalid as exc:
        print(f"WireGuard-Config abgelehnt: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
