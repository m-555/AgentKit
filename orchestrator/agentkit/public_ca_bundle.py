"""Attest the annotated public CA bundle installed by grpcio, never private material."""
import base64
import csv
import hashlib
import re
import ssl

RELATIVE = "grpc/_cython/_credentials/roots.pem"


def public_bundle(path, content):
    if not path.as_posix().lower().endswith("/site-packages/" + RELATIVE):
        return False
    from .secrets import _PATTERNS
    if any(pattern.search(content) for _, pattern in _PATTERNS):
        return False
    labels = re.findall(r"-----BEGIN ([A-Z0-9 ]+)-----", content)
    if not labels or any(label != "CERTIFICATE" for label in labels):
        return False
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----", content)
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata="\n".join(blocks))
        data = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        for record in path.parents[3].glob("grpcio-*.dist-info/RECORD"):
            with record.open(encoding="utf-8", newline="") as stream:
                for row in csv.reader(stream):
                    if row == [RELATIVE, "sha256=" + digest, str(len(data))]:
                        return True
    except (OSError, UnicodeError, ValueError, ssl.SSLError, csv.Error):
        return False
    return False
