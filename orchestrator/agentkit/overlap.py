"""Predicting whether two tasks will collide, before either worker starts.

Blocking a collision at edit time is late: both workers have already spent
tokens and one of them has to be unwound. This module predicts the modification
set up front from five sources and serialises when it cannot be confident.

Invariant 12: **uncertainty serialises.** A false serialisation costs some
wall-clock time; a false parallel costs a corrupted merge and an afternoon. The
asymmetry is not close, so every ambiguous case resolves the same way.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, globs, repo

HIGH, MEDIUM, LOW = "high", "medium", "low"
_CONFIDENCE_ORDER = {HIGH: 3, MEDIUM: 2, LOW: 1}

#: Historical coupling thresholds (§7.1).
COCHANGE_RATIO = 0.30
COCHANGE_MIN_COMMITS = 3


@dataclass
class Prediction:
    task_id: int
    paths: set[str] = field(default_factory=set)
    sources: dict[str, list[str]] = field(default_factory=dict)
    confidence: str = HIGH

    def add(self, source: str, paths: list[str], confidence: str) -> None:
        new = [p for p in paths if p]
        if not new:
            return
        self.paths.update(new)
        self.sources.setdefault(source, []).extend(new)
        if _CONFIDENCE_ORDER[confidence] < _CONFIDENCE_ORDER[self.confidence]:
            self.confidence = confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "paths": sorted(self.paths),
            "sources": {k: sorted(set(v)) for k, v in self.sources.items()},
            "confidence": self.confidence,
        }


@dataclass
class OverlapDecision:
    parallel: bool
    reason: str
    code: str
    overlapping: list[str] = field(default_factory=list)
    left: Prediction | None = None
    right: Prediction | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parallel": self.parallel, "reason": self.reason, "code": self.code,
            "overlapping": self.overlapping,
            "left": self.left.to_dict() if self.left else None,
            "right": self.right.to_dict() if self.right else None,
        }


# ------------------------------------------------------------------ co-change


def cochange_map(root: str | Path, days: int = 180, limit: int = 4000) -> dict[str, dict[str, int]]:
    """How often each pair of files changes in the same commit.

    Catches couplings no static analysis sees — a route file and the service it
    always ships with, a model and its serialiser.
    """
    out = repo.commit_file_sets(root, days=days, limit=limit)
    pair_counts: dict[str, dict[str, int]] = {}
    for files in out:
        unique = sorted(set(files))
        if len(unique) < 2 or len(unique) > 40:   # huge commits carry no signal
            continue
        for i, a in enumerate(unique):
            bucket = pair_counts.setdefault(a, {})
            for b in unique[i + 1:]:
                bucket[b] = bucket.get(b, 0) + 1
                pair_counts.setdefault(b, {})[a] = pair_counts[b].get(a, 0) + 1
    return pair_counts


def coupled_files(
    root: str | Path, targets: list[str], *, cache: dict[str, Any] | None = None
) -> list[str]:
    """Files historically changed alongside `targets` often enough to matter."""
    store = cache if cache is not None else {}
    if "pairs" not in store:
        store["pairs"] = cochange_map(root)
        store["totals"] = repo.file_commit_counts(root)
    pairs: dict[str, dict[str, int]] = store["pairs"]
    totals: dict[str, int] = store["totals"]

    coupled: set[str] = set()
    for target in targets:
        total = totals.get(target, 0)
        if total < COCHANGE_MIN_COMMITS:
            continue
        for other, together in (pairs.get(target) or {}).items():
            if together < COCHANGE_MIN_COMMITS:
                continue
            if together / total >= COCHANGE_RATIO:
                coupled.add(other)
    return sorted(coupled - set(targets))


# ----------------------------------------------------------------- prediction


def predict(
    conn: sqlite3.Connection,
    root: str | Path,
    task: dict[str, Any],
    *,
    cache: dict[str, Any] | None = None,
    use_history: bool = True,
) -> Prediction:
    """The set of paths this task is likely to write."""
    prediction = Prediction(task_id=int(task["id"]))

    declared = [str(p) for p in (task.get("expected_write") or task.get("owned_paths") or [])]
    prediction.add("declared", declared, HIGH)

    if not declared and task.get("kind") in ("RESEARCH", "REVIEW"):
        prediction.confidence = HIGH
        prediction.sources["read_only"] = []
        return prediction

    if not declared:
        prediction.confidence = LOW
        prediction.sources.setdefault("undeclared", [])
        return prediction

    if use_history:
        try:
            coupled = coupled_files(root, _literal_paths(declared), cache=cache)
        except Exception:
            coupled = []
        prediction.add("cochange", coupled, MEDIUM)

    if str(task.get("kind")) in ("CONTRACT_CHANGE", "DECOUPLE", "HOTSPOT"):
        # These change shared shapes, so importers are in scope by implication.
        importers = _importers(root, _literal_paths(declared), cache=cache)
        prediction.add("import_graph", importers, MEDIUM)

    return prediction


def _literal_paths(patterns: list[str]) -> list[str]:
    """Only concrete file paths participate in history lookups."""
    return [p for p in patterns if "*" not in p and "?" not in p]


def _importers(
    root: str | Path, targets: list[str], *, cache: dict[str, Any] | None = None
) -> list[str]:
    from .hotspots import _import_targets, tracked_sources

    store = cache if cache is not None else {}
    if "imports" not in store:
        files = tracked_sources(Path(root))
        index: dict[str, set[str]] = {}
        for rel in files:
            try:
                text = (Path(root) / rel).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for token in _import_targets(text):
                index.setdefault(token, set()).add(rel)
        store["imports"] = index
    index = store["imports"]

    found: set[str] = set()
    for target in targets:
        stem = Path(target).stem.lower()
        if stem in ("index", "__init__", "mod"):
            stem = Path(target).parent.name.lower()
        if len(stem) >= 3:
            found.update(index.get(stem, set()))
    return sorted(found - set(targets))


# ------------------------------------------------------------------- decision


def can_run_together(
    conn: sqlite3.Connection,
    root: str | Path,
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    cache: dict[str, Any] | None = None,
) -> OverlapDecision:
    """§7.2. Serialise unless both predictions are confident and disjoint."""
    cache = cache if cache is not None else {}
    p_left = predict(conn, root, left, cache=cache)
    p_right = predict(conn, root, right, cache=cache)

    clashes = _overlapping(p_left.paths, p_right.paths)
    if clashes:
        return OverlapDecision(
            False,
            f"predicted write sets overlap on {', '.join(clashes[:3])}"
            f"{'…' if len(clashes) > 3 else ''}",
            "predicted_write_overlap", clashes, p_left, p_right,
        )

    for writer, reader in ((left, right), (right, left)):
        if str(writer.get("kind")) == "CONTRACT_CHANGE":
            reads = [str(p) for p in (reader.get("expected_read") or [])]
            writes = p_left.paths if writer is left else p_right.paths
            shared = _overlapping(set(writes), set(reads))
            if shared:
                return OverlapDecision(
                    False,
                    f"task {reader['id']} reads a contract task {writer['id']} is changing "
                    f"({', '.join(shared[:3])})",
                    "reader_of_changing_contract", shared, p_left, p_right,
                )

    if LOW in (p_left.confidence, p_right.confidence):
        ancestor = _shared_module(p_left.paths, p_right.paths)
        if ancestor:
            return OverlapDecision(
                False,
                f"low-confidence prediction and both tasks touch `{ancestor}`",
                "low_confidence_same_module", [ancestor], p_left, p_right,
            )
        if not p_left.paths or not p_right.paths:
            return OverlapDecision(
                False,
                "at least one task has not declared expected_paths, so overlap cannot be ruled out",
                "undeclared_scope", [], p_left, p_right,
            )

    return OverlapDecision(
        True, "predicted write sets are disjoint", "disjoint", [], p_left, p_right,
    )


def _overlapping(left: set[str], right: set[str]) -> list[str]:
    found: set[str] = set()
    for a in left:
        for b in right:
            if globs.overlaps(a, b):
                found.add(a if "*" not in a else b)
    return sorted(found)


def _shared_module(left: set[str], right: set[str], depth: int = 2) -> str | None:
    """Do both sets live under the same top-level module?"""
    def prefixes(paths: set[str]) -> set[str]:
        out = set()
        for path in paths:
            parts = [p for p in str(path).replace("\\", "/").split("/") if p and "*" not in p]
            if parts:
                out.add("/".join(parts[:depth]))
        return out

    shared = prefixes(left) & prefixes(right)
    return sorted(shared)[0] if shared else None


def schedulable_set(
    conn: sqlite3.Connection,
    root: str | Path,
    candidates: list[dict[str, Any]],
    *,
    limit: int | None = 3,
    role_limits: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """Greedily pick a mutually-compatible batch.

    Returns `(chosen, deferred)`, where each deferred entry is `(task, reason)`.

    The reason travels with the task on purpose. Both exits from this loop leave a
    task unstarted, but they mean opposite things: one is a *prediction about file
    collisions*, the other is simply the worker cap. Reporting the cap as a
    collision — which is what a single shared message did — sends people hunting
    for a conflict between tasks that do not share a single path, and contradicts
    `agentkit why serialized`, which correctly finds no serialisation event
    because none was recorded.
    """
    cache: dict[str, Any] = {}
    chosen: list[dict[str, Any]] = []
    deferred: list[tuple[dict[str, Any], str]] = []

    running = db.list_tasks(conn, ("LEASED", "RUNNING", "VERIFYING"))
    pools = role_limits or {}
    counts = {role: sum(task.get("role") == role for task in running) for role in pools}

    for candidate in candidates:
        role = candidate.get("role")
        if role in pools and counts[role] >= pools[role]:
            deferred.append((candidate, "configured worker role slots are occupied"))
            continue
        # `limit=None` is the unlimited mode: every compatible candidate is chosen.
        if limit is not None and len(chosen) >= limit:
            deferred.append((
                candidate,
                f"worker limit reached ({limit} slot(s) available, already filled); "
                "no path conflict was predicted",
            ))
            continue
        blocked = False
        for other in (*running, *chosen):
            decision = can_run_together(conn, root, candidate, other, cache=cache)
            if not decision.parallel:
                db.log_event(
                    conn, int(candidate["id"]), "serialization_decision",
                    cause=decision.reason,
                    effect=f"deferred behind task {other['id']}",
                    detail=decision.to_dict(),
                )
                deferred.append((
                    candidate,
                    f"{decision.reason} (behind task {other['id']})",
                ))
                blocked = True
                break
        if not blocked:
            chosen.append(candidate)
            if role in counts:
                counts[role] += 1
    return chosen, deferred
