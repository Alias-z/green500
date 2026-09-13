"""Immutable local source storage and public URL validation."""

import hashlib
import ipaddress
import json
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def validate_public_url(url: str) -> str:
    """Reject credentials, local targets and non-web schemes before outbound work."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Source URL must be a public HTTP(S) URL without credentials.")
    if parsed.port not in {None, 443 if parsed.scheme == "https" else 80}:
        raise ValueError("Source URL must use HTTP or HTTPS's standard port.")
    addresses = socket.getaddrinfo(
        parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
    )
    if not addresses or any(
        not ipaddress.ip_address(item[4][0]).is_global for item in addresses
    ):
        raise ValueError("Source URL resolves to a private or reserved address.")
    return urlunsplit(
        (parsed.scheme, parsed.hostname.lower(), parsed.path or "/", parsed.query, "")
    )


def save_bytes(root: Path, body: bytes) -> str:
    """Atomically store bytes under their full SHA-256 without replacing other content."""
    digest = hashlib.sha256(body).hexdigest()
    path = root / "objects" / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Stored source failed SHA-256 verification.")
        return digest
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return digest


def read_bytes(root: Path, digest: str) -> bytes:
    """Read a verified object without accepting user-controlled file paths."""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("Invalid object SHA-256.")
    body = (root / "objects" / digest[:2] / digest).read_bytes()
    if hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("Stored source failed SHA-256 verification.")
    return body


def save_file(root: Path, source: Path, *, expected_digest: str | None = None) -> str:
    """Stream a local original into atomic hash storage without loading it into memory."""
    objects = root / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=objects)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as destination, source.open("rb") as original:
            while chunk := original.read(1024 * 1024):
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        value = digest.hexdigest()
        if expected_digest is not None and value != expected_digest:
            raise ValueError("Source file failed SHA-256 verification.")
        path = objects / value[:2] / value
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with path.open("rb") as existing:
                if hashlib.file_digest(existing, "sha256").hexdigest() != value:
                    raise ValueError("Stored source failed SHA-256 verification.")
        else:
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return value
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(root: Path, value: object) -> str:
    """Store a deterministic JSON artifact as immutable bytes."""
    return save_bytes(
        root,
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(),
    )
