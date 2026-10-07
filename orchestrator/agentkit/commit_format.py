"""Optional deterministic line endings for audited source before host commits."""
from pathlib import Path

TEXT_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".json", ".yaml", ".yml", ".md"})
WORKER_NOTE = ("Line endings are normalized by the host when it commits: write normal text and never "
               "check, convert or rewrite a file for its line endings, whatever the task text says.")


def worker_note(project):
    """What a worker must know when the host owns line endings, so a spec's "CRLF" sends nobody chasing it."""
    return WORKER_NOTE if (getattr(project, "raw", None) or {}).get("source_line_endings") else None


def normalize(project, worktree, paths, guard):
    setting = project.raw.get("source_line_endings")
    if setting is None:
        return
    if setting not in ("lf", "crlf"):
        raise ValueError("source_line_endings must be lf or crlf")
    work = Path(worktree).resolve(strict=True)
    for relative in paths:
        path = work / relative
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(work):
            raise PermissionError("Source formatter cannot follow links outside audited files")
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result = data.replace(b"\r\n", b"\n")
        if setting == "crlf":
            result = result.replace(b"\n", b"\r\n")
        if result != data:
            guard()
            path.write_bytes(result)
