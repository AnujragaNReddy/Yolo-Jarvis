"""A private certificate authority and an HTTPS certificate for this PC's network address.

Phones only allow the microphone on HTTPS pages. Installing ca.crt on a phone makes it trust JARVIS
without a warning; without it the phone shows a warning once that can be clicked through.
"""

import datetime as dt
import ipaddress
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import access, config

DIR = config.DB_PATH.parent / "tls"
CA_CERT, CA_KEY = DIR / "ca.crt", DIR / "ca.key"
CERT, KEY = DIR / "server.crt", DIR / "server.key"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _save_key(key, path) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))


def _ca():
    if CA_CERT.exists() and CA_KEY.exists():
        return (x509.load_pem_x509_certificate(CA_CERT.read_bytes()),
                serialization.load_pem_private_key(CA_KEY.read_bytes(), None))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"YOLO-JARVIS local CA ({socket.gethostname()})")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(days=1)).not_valid_after(_now() + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    _save_key(key, CA_KEY)
    CA_CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def _covers(ip: str) -> bool:
    if not (CERT.exists() and KEY.exists()):
        return False
    cert = x509.load_pem_x509_certificate(CERT.read_bytes())
    if cert.not_valid_after_utc < _now() + dt.timedelta(days=30):
        return False
    ips = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
    return ipaddress.ip_address(ip) in ips


def _tailscale_ip() -> str | None:
    """This PC's Tailscale address, if Tailscale is installed and connected (private network, not the internet)."""
    import subprocess
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
        return str(ipaddress.ip_address(out[0])) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ensure() -> tuple[str, str]:
    """Paths to the server certificate and key, (re)made when the PC's network address changes."""
    DIR.mkdir(parents=True, exist_ok=True)
    ip = access.lan_ip() or "127.0.0.1"
    ts = _tailscale_ip()
    if _covers(ip) and (not ts or _covers(ts)):
        return str(CERT), str(KEY)
    ca_cert, ca_key = _ca()
    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname()),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address(ip))]
    if ts and ts != ip:
        sans.append(x509.IPAddress(ipaddress.ip_address(ts)))
    # 397 days: the longest lifetime Apple and Chrome accept for a server certificate.
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"JARVIS {ip}")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(days=1)).not_valid_after(_now() + dt.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _save_key(key, KEY)
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
    return str(CERT), str(KEY)
