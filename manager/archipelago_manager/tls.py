"""Encrypted game connections (wss://): the certificate the game servers present.

Given a certificate (--cert, --cert_key), MultiServer.py accepts encrypted connections only. Archipelago's own clients
try wss:// by themselves when ws:// gets no answer, so players keep entering host:port as before. Clients that cannot
encrypt at all need their game to run without it: every game has the server option `tls` for that.

The files are named by path, so a certificate that something else renews (Let's Encrypt, a reverse proxy's folder
mounted into the container) is picked up: a server reads it when it starts, and rooms.watch_certificate restarts the
servers that still present an older one once nobody is connected to them.
"""
from __future__ import annotations

import ipaddress
import os
import ssl
import time
from pathlib import Path
from typing import Any

from . import db, settings as app_settings

# Admins are warned this long before the certificate expires
EXPIRY_WARNING_DAYS = 14

_inspected: dict[tuple, dict] = {}


class TLSError(ValueError):
    pass


def paths(values: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """The certificate and its key as set, or None if encrypted connections are off."""
    values = values or app_settings.get_settings()
    cert, key = values["tls_cert"], values["tls_key"]
    return (cert, key or cert) if cert else None


def stamp(cert: str, key: str) -> tuple:
    """Changes whenever either file changes, such as when the certificate is renewed."""
    result = []
    for path in (cert, key):
        try:
            st = os.stat(path)
            result.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            result.append((path, None, None))
    return tuple(result)


def _check_file(path: str, relative: str) -> None:
    file = Path(path)
    if not file.is_absolute():
        raise TLSError(relative)
    if not file.is_file():
        raise TLSError(f"There is no file {path} in the container")
    if not os.access(file, os.R_OK):
        raise TLSError(f"The manager runs as user {os.getuid()} and may not read {path}")


def inspect(cert: str, key: str) -> dict:
    """Loads the certificate with its key the way MultiServer.py does, and says what it is good for. Raises TLSError
    with the reason if a server could not use it."""
    key_stamp = stamp(cert, key)
    if key_stamp in _inspected:
        return _inspected[key_stamp]
    _check_file(cert, "Enter the certificate with its full path in the container, e.g. /config/tls/fullchain.pem")
    _check_file(key, "Enter the key with its full path in the container, e.g. /config/tls/privkey.pem")
    try:
        # the first certificate in the file is the server's own; a chain of intermediates follows it
        decoded = ssl._ssl._test_decode_cert(cert)  # noqa: SLF001 - the only way the standard library reads one
    except (ssl.SSLError, OSError):
        raise TLSError(f"{cert} is not a certificate in PEM format (-----BEGIN CERTIFICATE-----)") from None
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    except (ssl.SSLError, OSError):
        raise TLSError(f"{key} is not the private key of this certificate (a PEM file without a password)") from None
    names = [value for kind, value in decoded.get("subjectAltName", ()) if kind in ("DNS", "IP Address")]
    if not names:
        names = [value for rdn in decoded.get("subject", ()) for name, value in rdn if name == "commonName"]
    info = {"names": names, "expires_at": ssl.cert_time_to_seconds(decoded["notAfter"]),
            "issuer": next((value for rdn in decoded.get("issuer", ()) for name, value in rdn
                            if name in ("organizationName", "commonName")), None),
            "self_signed": decoded.get("issuer") == decoded.get("subject")}
    _inspected.clear()  # only the current files are of interest
    _inspected[key_stamp] = info
    return info


def covers(names: list[str], host: str) -> bool:
    """Whether a certificate for these names is valid for a host, as a client checks it (wildcards: one label)."""
    host = host.strip().lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for name in names:
        name = name.lower().rstrip(".")
        if address is not None:
            try:
                if ipaddress.ip_address(name) == address:
                    return True
            except ValueError:
                continue
        elif name == host or (name.startswith("*.") and host.count(".") >= 2 and host.split(".", 1)[1] == name[2:]):
            return True
    return False


def status(values: dict[str, Any] | None = None) -> dict:
    """What the admin sees: whether encrypted connections are on, and whether the certificate is fine."""
    values = values or app_settings.get_settings()
    files = paths(values)
    if not files:
        return {"enabled": False}
    result: dict[str, Any] = {"enabled": True, "cert": files[0], "key": files[1]}
    try:
        info = inspect(*files)
    except TLSError as e:
        return {**result, "ok": False, "error": str(e)}
    host = values["public_host"]
    result.update(info, ok=True, expired=info["expires_at"] < time.time(),
                  expires_soon=info["expires_at"] < time.time() + EXPIRY_WARNING_DAYS * 86400,
                  # without a public address, players use whatever address they opened the web UI with
                  covers_public_host=covers(info["names"], host) if host else None,
                  games_without=db.query_one("SELECT COUNT(*) AS n FROM games WHERE json_extract(options, '$.tls') = 0 "
                                             "AND finished_at IS NULL")["n"])
    return result


def wanted(game: Any) -> bool:
    """Whether a game's server starts with encrypted connections."""
    return bool(app_settings.clean_server_options(db.jloads(game["options"], {}))["tls"] and paths())


def for_game(game: Any) -> tuple[str, str] | None:
    """The certificate and key a game's server is started with, or None to start it unencrypted. Raises TLSError if
    the game is to be encrypted and the certificate cannot be used: it then does not start, rather than quietly
    letting its passwords go over the network in the clear."""
    if not wanted(game):
        return None
    files = paths()
    inspect(*files)
    return files
