"""Self-signed TLS certificate generation for the local web UI.

Browsers refuse the Notification API on insecure HTTP origins (Yandex Browser
is especially strict). Generating a self-signed cert lets the bot serve over
HTTPS without external infrastructure. The user has to accept the "not trusted"
warning once per browser; after that it just works.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def detect_local_ip() -> Optional[str]:
    """Best-effort detection of the host's primary LAN IPv4 address."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No actual packet is sent; this just picks the right interface.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def detect_hostnames() -> list[str]:
    """Hostnames that should be valid for the cert."""
    names: list[str] = ["localhost"]
    try:
        h = socket.gethostname()
        if h and h not in names:
            names.append(h)
        # On Linux, hostname.local is a common mDNS name
        if h and not h.endswith(".local"):
            names.append(f"{h}.local")
    except OSError:
        pass
    return names


def ensure_self_signed_cert(
    cert_path: Path,
    key_path: Path,
    *,
    extra_ips: Optional[list[str]] = None,
    valid_days: int = 3650,
) -> tuple[Path, Path]:
    """Generate a self-signed certificate and key if they don't already exist.

    Uses the `cryptography` library when available; falls back to OpenSSL CLI
    if the library is missing (so the bot still boots on minimal images).
    """
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    log.info("Generating self-signed TLS certificate at %s", cert_path)

    san_dns = detect_hostnames()
    san_ips: list[str] = ["127.0.0.1"]
    local_ip = detect_local_ip()
    if local_ip and local_ip not in san_ips:
        san_ips.append(local_ip)
    if extra_ips:
        for ip in extra_ips:
            if ip and ip not in san_ips:
                san_ips.append(ip)

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        log.warning("`cryptography` not installed — falling back to OpenSSL CLI")
        return _fallback_openssl(cert_path, key_path, san_dns, san_ips, valid_days)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "weather-mesh-bridge"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Local"),
    ])
    san_entries: list = [x509.DNSName(d) for d in san_dns]
    for ip in san_ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    log.info(
        "Certificate generated. SAN dns=%s ip=%s, valid for %d days.",
        san_dns, san_ips, valid_days,
    )
    return cert_path, key_path


def _fallback_openssl(
    cert_path: Path,
    key_path: Path,
    san_dns: list[str],
    san_ips: list[str],
    valid_days: int,
) -> tuple[Path, Path]:
    """Last resort: shell out to `openssl` if `cryptography` is unavailable."""
    san_parts = [f"DNS:{d}" for d in san_dns] + [f"IP:{ip}" for ip in san_ips]
    san = ",".join(san_parts)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key_path),
        "-out", str(cert_path),
        "-days", str(valid_days),
        "-subj", "/CN=weather-mesh-bridge",
        "-addext", f"subjectAltName={san}",
    ]
    subprocess.run(cmd, check=True)
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return cert_path, key_path


__all__ = ["ensure_self_signed_cert", "detect_local_ip", "detect_hostnames"]
