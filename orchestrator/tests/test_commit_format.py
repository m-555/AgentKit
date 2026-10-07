"""Formatting is opt-in, byte bounded by audited paths and current generation."""
from types import SimpleNamespace

import pytest

from agentkit.commit_format import normalize


@pytest.mark.parametrize("setting,expected", [(None, b"x\n"), ("crlf", b"x\r\n"), ("lf", b"x\n")])
def test_selected_line_endings_only(tmp_path, setting, expected):
    source = tmp_path / "owned.py"
    source.write_bytes(b"x\n")
    foreign = tmp_path / "foreign.py"
    foreign.write_bytes(b"y\n")
    normalize(SimpleNamespace(raw={"source_line_endings": setting}), tmp_path, ["owned.py"], lambda: None)
    assert source.read_bytes() == expected
    assert foreign.read_bytes() == b"y\n"


def test_crlf_normalization_is_idempotent_and_skips_binary(tmp_path):
    (tmp_path / "owned.py").write_bytes(b"x\r\n")
    (tmp_path / "binary.py").write_bytes(b"\0x\n")
    def unexpected():
        pytest.fail("Unchanged/binary inputs need no write")
    normalize(SimpleNamespace(raw={"source_line_endings": "crlf"}), tmp_path,
              ["owned.py", "binary.py"], unexpected)
    assert (tmp_path / "owned.py").read_bytes() == b"x\r\n"
    assert (tmp_path / "binary.py").read_bytes() == b"\0x\n"


def test_generation_guard_precedes_formatter_write(tmp_path):
    source = tmp_path / "owned.py"
    source.write_bytes(b"x\n")
    def stale():
        raise PermissionError("stale generation")
    with pytest.raises(PermissionError, match="stale"):
        normalize(SimpleNamespace(raw={"source_line_endings": "crlf"}), tmp_path, ["owned.py"], stale)
    assert source.read_bytes() == b"x\n"


def test_formatter_refuses_escape_path(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    source = tmp_path / "outside.py"
    source.write_bytes(b"x\n")
    with pytest.raises(PermissionError):
        normalize(SimpleNamespace(raw={"source_line_endings": "crlf"}), work, ["../outside.py"], lambda: None)
    assert source.read_bytes() == b"x\n"
