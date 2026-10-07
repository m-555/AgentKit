"""Published CA dumps must not make private keys or credentials acceptable."""
import base64
import hashlib
import ssl

import pytest

from agentkit import secrets


def fixture(root, extra="", *, attest=True):
    path = root / "venv/Lib/site-packages/grpc/_cython/_credentials/roots.pem"
    path.parent.mkdir(parents=True)
    cert = ssl.create_default_context().get_ca_certs(binary_form=True)[0]
    path.write_text("Certificate:\n    Data:\n        Subject: Public Root CA\n" + ssl.DER_cert_to_PEM_cert(cert) + extra)
    data = path.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
    record = path.parents[3] / "grpcio-1.0.dist-info/RECORD"
    record.parent.mkdir()
    record.write_text("grpc/_cython/_credentials/roots.pem,sha256=" + (digest if attest else "wrong") + "," + str(len(data)))
    return path


def test_unchanged_attested_grpc_public_root_bundle_is_allowed(tmp_path):
    fixture(tmp_path)
    assert secrets.assert_clean(tmp_path) == []


@pytest.mark.parametrize("extra", [
    "\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
    "\nAPI_KEY=sk-" + "a" * 30,
    "\nPASSWORD=actual-password",
    "\n-----BEGIN UNKNOWN-----\nabc\n-----END UNKNOWN-----",
])
def test_private_material_stays_refused_even_with_matching_record(tmp_path, extra):
    path = fixture(tmp_path, extra)
    assert str(path.relative_to(tmp_path)).replace("\\", "/") in secrets.scan_worktree(tmp_path)["files"]


def test_modified_bundle_with_wrong_package_digest_is_refused(tmp_path):
    fixture(tmp_path, attest=False)
    assert secrets.scan_worktree(tmp_path)["files"]


def test_annotations_outside_attested_bundle_are_refused(tmp_path):
    path = fixture(tmp_path)
    (tmp_path / "arbitrary.pem").write_bytes(path.read_bytes())
    assert secrets.scan_worktree(tmp_path)["files"] == ["arbitrary.pem"]
