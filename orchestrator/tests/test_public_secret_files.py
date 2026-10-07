"""Public fixtures must not mask real credentials or private keys."""
import pytest

from agentkit import secrets


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template"])
def test_template_with_empty_credentials_and_model_budgets(tmp_path, name):
    (tmp_path / name).write_text("API_KEY=your_key_here\nCLIENT_SECRET=\nPROMPT_MAX_TOKENS=12000\nURL=http://localhost:8770\n")
    assert secrets.assert_clean(tmp_path) == []


@pytest.mark.parametrize("content", ["API_KEY=real-value", "PASSWORD=123", "URL=https://user:password@example.org", "sk-" + "a" * 30, "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", "not an assignment"])
def test_template_with_credentials_or_unknown_contents_is_refused(tmp_path, content):
    (tmp_path / ".env.example").write_text(content)
    assert secrets.scan_worktree(tmp_path)["files"] == [".env.example"]


def test_public_certificates_only_are_allowed(tmp_path):
    import ssl
    certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
    assert certificates, "The test requires an OS public trust store"
    (tmp_path / "public.pem").write_text(ssl.DER_cert_to_PEM_cert(certificates[0]))
    assert secrets.assert_clean(tmp_path) == []
    with (tmp_path / "public.pem").open("a") as stream:
        stream.write("\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
    assert secrets.scan_worktree(tmp_path)["files"] == ["public.pem"]


def test_regular_environment_file_remains_refused(tmp_path):
    (tmp_path / ".env").write_text("URL=http://localhost")
    assert secrets.scan_worktree(tmp_path)["files"] == [".env"]


@pytest.mark.parametrize("url,allowed", [
    ("postgresql://demo:demo@localhost:5432/demo", True),
    ("postgresql://postgres:password@localhost:5432/example_db", True),
    ("postgresql://demo:demo@database.example.org/demo", False),
    ("postgresql://demo:real-password@localhost/demo", False),
    ("https://demo:demo@localhost/demo", False),
])
def test_only_recognizable_local_database_demo_urls_are_allowed(tmp_path, url, allowed):
    (tmp_path / ".env.example").write_text("DATABASE_URL=" + url)
    assert bool(secrets.scan_worktree(tmp_path)["files"]) is not allowed
