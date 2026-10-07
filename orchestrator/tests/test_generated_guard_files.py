"""Legacy projects must not mistake host-installed guards for worker edits."""
import subprocess
from pathlib import Path

from agentkit import audit, db, guard_files
from agentkit import repo as repository
from agentkit.adapters.codex import CodexAdapter
from agentkit.config import load_project


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_legacy_worktree_excludes_untracked_guards_but_audits_forced_staging(project_root):
    root = project_root
    ignore = root / ".gitignore"
    ignore.write_text("# legacy ignore rules\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "legacy ignore")
    git(root, "worktree", "add", "-b", "worker", str(root.parent / "worker"))
    work = root.parent / "worker"
    c = db.connect(root)
    task = db.create_task(c, title="owned source", kind="SAFE_PARALLEL",
                          owned_paths=["services/media.py"])
    db.update_task(c, task, base_sha=git(work, "rev-parse", "HEAD"), worktree=str(work))
    project = load_project(root)
    original_ignore = ignore.read_bytes()
    CodexAdapter().install_guards(work, {"id": task}, root)
    CodexAdapter().install_guards(work, {"id": task}, root)
    assert ignore.read_bytes() == original_ignore
    assert (work / ".gitignore").read_bytes() == original_ignore
    assert not repository.changed_files(work)
    assert audit.audit_worktree(c, project, work, task, record=False).clean
    exclude = Path(git(work, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = work / exclude
    assert exclude.read_text().splitlines().count("/.codex/hooks.json") == 1
    git(work, "add", "-f", ".codex/hooks.json")
    assert not audit.audit_staged(c, project, work, task).clean
    git(work, "commit", "-qm", "force staged guard")
    assert not audit.audit_branch(c, project, work, task, db.get_task(c, task)["base_sha"]).clean
    (work / ".codex/hooks.json").write_text("{}", encoding="utf-8")
    assert not audit.audit_worktree(c, project, work, task, record=False).clean
    c.close()


def test_adapter_probe_without_git_is_supported(tmp_path):
    guard_files.exclude_untracked(tmp_path, ".codex/hooks.json")
    assert not (tmp_path / ".git").exists()
