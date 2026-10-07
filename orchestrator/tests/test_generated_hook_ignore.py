"""Generated hooks must not appear as worker source changes."""
import subprocess

from agentkit.init_project import init


def test_generated_codex_hook_is_ignored_but_unowned_source_remains_visible(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    init(tmp_path)
    settings = tmp_path / ".codex/hooks.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text('{"hooks":{}}', encoding="utf-8")
    (tmp_path / "unowned.py").write_text("VALUE = 1", encoding="utf-8")
    ignored = subprocess.run(["git", "check-ignore", ".codex/hooks.json"], cwd=tmp_path,
                             capture_output=True, text=True)
    assert ignored.returncode == 0
    visible = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                             cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert ".codex/hooks.json" not in visible
    assert "unowned.py" in visible
