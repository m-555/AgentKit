"""Measuring which files will serialise your agents.

Two different risks are at work here and it is a mistake to merge them:

  * **collision** = churn x log2(lines). How likely two agents editing two
    unrelated features end up in the same file at the same time. A 8,000-line
    router that nothing imports still scores high, because every feature adds an
    endpoint to it.
  * **blast radius** = fan_in. How many other modules a change here can break.
    A 200-line `database.py` that 90 files import is dangerous for a different
    reason: it is easy to edit and expensive to get wrong.

    score = collision * (1 + sqrt(fan_in))

The square root is deliberate. Multiplying by `(1 + fan_in)` outright let a
widely-imported 400-line helper outrank an 8,000-line file that every feature has
to touch, which is precisely backwards for scheduling parallel work.

This is a triage tool, not a static analyser. Its job is to put the refactor
queue in a defensible order, which is more than "the architect had a feeling".
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SOURCE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".swift",
    ".vue", ".svelte",
}

EXCLUDE_PARTS = {
    "node_modules", ".venv", "venv", "dist", "build", "vendor", "__pycache__",
    ".git", "migrations", "generated", ".next", "out", "target", "Intermediate",
    "DerivedDataCache", "Binaries", "Saved",
}

# Fan-in is counted from import statements only. Counting bare identifiers looked
# tempting but scores every file that merely says "api" or "main" as a dependent,
# which inflates exactly the generic modules you least want to mis-rank.
_IMPORT_PATTERNS = (
    re.compile(r"^\s*from\s+([\w\.]+)\s+import\b", re.MULTILINE),      # python
    re.compile(r"^\s*import\s+([\w\.]+)", re.MULTILINE),               # python
    re.compile(r"""["']([^"'\n]+)["']\s*\)?\s*;?\s*$""", re.MULTILINE),  # js module specifier
    re.compile(r"#include\s*[\"<]([^\">]+)[\">]"),                     # c/c++
)
_JS_IMPORT_LINE = re.compile(
    r"""(?:^\s*import\b[^;\n]*?from\s*|(?:require|import)\s*\(\s*)["']([^"']+)["']""",
    re.MULTILINE,
)


@dataclass
class Hotspot:
    path: str
    lines: int
    churn: int
    fan_in: int
    collision: float
    score: float

    @property
    def verdict(self) -> str:
        """Why this file is on the list, which decides how you fix it."""
        if self.collision >= 200 and self.fan_in >= 15:
            return "split + stabilise interface"
        if self.collision >= 200:
            return "split by seam"
        if self.fan_in >= 25:
            return "freeze behind a contract"
        return "watch"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["collision"] = round(self.collision, 1)
        data["score"] = round(self.score, 1)
        data["verdict"] = self.verdict
        return data


def _git(args: list[str], root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            timeout=120, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _excluded(rel: str) -> bool:
    return any(part in EXCLUDE_PARTS for part in Path(rel).parts)


def tracked_sources(root: Path) -> list[str]:
    out = _git(["ls-files"], root)
    files = []
    for line in out.splitlines():
        rel = line.strip()
        if not rel or _excluded(rel):
            continue
        if Path(rel).suffix.lower() in SOURCE_SUFFIXES:
            files.append(rel)
    return files


def churn_counts(root: Path, days: int = 90) -> dict[str, int]:
    out = _git(["log", f"--since={days}.days", "--format=", "--name-only"], root)
    counts: dict[str, int] = {}
    for line in out.splitlines():
        rel = line.strip()
        if rel:
            counts[rel] = counts.get(rel, 0) + 1
    return counts


def _line_count(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _import_targets(text: str) -> set[str]:
    """The final segment of every module a file imports, lowercased.

    `from services.media_services import X` and `import ../services/api.js` both
    reduce to the segment that identifies the file, which is what we match against.
    """
    targets: set[str] = set()
    for pattern in (_IMPORT_PATTERNS[0], _IMPORT_PATTERNS[1]):
        for match in pattern.findall(text):
            for part in str(match).replace(",", " ").split():
                segment = part.split(".")[-1].strip()
                if segment:
                    targets.add(segment.lower())
    for match in _JS_IMPORT_LINE.findall(text):
        segment = str(match).replace("\\", "/").split("/")[-1]
        segment = re.sub(r"\.(?:js|jsx|ts|tsx|mjs|cjs|vue|svelte)$", "", segment)
        if segment:
            targets.add(segment.lower())
    for match in _IMPORT_PATTERNS[3].findall(text):
        segment = str(match).replace("\\", "/").split("/")[-1]
        targets.add(re.sub(r"\.(?:h|hpp)$", "", segment).lower())
    return targets


def _fan_in(candidates: list[str], all_files: list[str], root: Path) -> dict[str, int]:
    """Count how many files actually import each candidate.

    A cheap stand-in for a per-language import graph: good enough to rank a
    refactor queue, and honest about direction (who depends on this file).
    """
    stems: dict[str, str] = {}
    for rel in candidates:
        stem = Path(rel).stem
        if stem in ("index", "__init__", "mod"):
            stem = Path(rel).parent.name or stem
        if len(stem) >= 3:
            stems[rel] = stem.lower()

    counts: dict[str, int] = {rel: 0 for rel in candidates}
    if not stems:
        return counts

    wanted = set(stems.values())
    for rel in all_files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        imported = _import_targets(text) & wanted
        if not imported:
            continue
        for candidate, stem in stems.items():
            if candidate != rel and stem in imported:
                counts[candidate] += 1
    return counts


def analyse(
    root: str | Path, *, days: int = 90, limit: int = 20, candidate_pool: int = 60
) -> list[Hotspot]:
    root_path = Path(root)
    files = tracked_sources(root_path)
    if not files:
        return []
    churn = churn_counts(root_path, days)
    lines = {rel: _line_count(root_path / rel) for rel in files}

    # Cheap pre-rank so fan-in only runs on plausible candidates.
    def prelim(rel: str) -> float:
        return (churn.get(rel, 0) + 1) * math.log2(max(lines.get(rel, 1), 2))

    pool = sorted(files, key=prelim, reverse=True)[:candidate_pool]
    fan = _fan_in(pool, files, root_path)

    spots = []
    for rel in pool:
        collision = churn.get(rel, 0) * math.log2(max(lines.get(rel, 1), 2))
        fan_in = fan.get(rel, 0)
        spots.append(
            Hotspot(
                path=rel,
                lines=lines.get(rel, 0),
                churn=churn.get(rel, 0),
                fan_in=fan_in,
                collision=collision,
                score=collision * (1 + math.sqrt(fan_in)),
            )
        )
    spots.sort(key=lambda s: (s.score, s.lines), reverse=True)
    return spots[:limit]


def format_table(spots: list[Hotspot], root: str | Path = "") -> str:
    if not spots:
        return "No source files found (is this a git repository with commits?)."
    width = max(len(s.path) for s in spots)
    verdict_width = max(len(s.verdict) for s in spots)
    header = (
        f"{'file'.ljust(width)}  {'lines':>6} {'churn':>6} {'collide':>8} "
        f"{'fan-in':>7} {'score':>8}  action"
    )
    rows = [header, "-" * (len(header) + verdict_width - 6)]
    for spot in spots:
        rows.append(
            f"{spot.path.ljust(width)}  {spot.lines:>6} {spot.churn:>6} "
            f"{spot.collision:>8.0f} {spot.fan_in:>7} {spot.score:>8.0f}  {spot.verdict}"
        )
    rows += [
        "",
        "collide = churn(90d) x log2(lines)  -> two agents land in this file at once",
        "fan-in  = files that import it      -> how far a mistake here spreads",
        "score   = collide x (1 + sqrt(fan-in))",
        "",
        "split by seam         : cut into modules along its natural groups",
        "freeze behind contract: small but widely imported; stabilise its interface first",
    ]
    return "\n".join(rows)
