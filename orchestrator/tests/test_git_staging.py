"""Exact Git staging respects ignored data and literal path names."""
import subprocess

import pytest

from agentkit import git_staging, repo


def test_ignored_untracked_file_is_not_force_added(project_root):
    ignore = project_root / ".gitignore"
    ignore.write_text(ignore.read_text() + "\ngenerated/\n")
    generated = project_root / "generated"
    generated.mkdir()
    (generated / "secret.txt").write_text("private input")
    with pytest.raises(ValueError, match="Host staging rejected"):
        git_staging.stage(project_root, ["generated/secret.txt"])
    assert not repo.staged_files(project_root)


def test_literal_path_and_deletion_are_staged(project_root):
    path = project_root / "services/a[b].py"
    path.write_text("VALUE = 1\n")
    git_staging.stage(project_root, ["services/a[b].py"])
    subprocess.run(["git", "commit", "-m", "literal"], cwd=project_root, check=True, capture_output=True)
    path.unlink()
    git_staging.stage(project_root, ["services/a[b].py"])
    assert repo.staged_files(project_root) == ["services/a[b].py"]
