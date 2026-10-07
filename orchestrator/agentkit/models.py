"""User-ranked model routing; model rejection never cools an entire account."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta

from . import db, processes, providers


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    model: str
    effort: str | None
    rank: int

    def to_dict(self):
        return asdict(self)


PROFILES = {
    "astra": Profile("astra", "codex", "gpt-6-astra", "high", 0),
    "sol": Profile("sol", "codex", "gpt-6.1-sol", "high", 1),
    "opus": Profile("opus", "claude-code", "claude-opus-5-5", "high", 2),
    "sonnet": Profile("sonnet", "claude-code", "claude-sonnet-5", "high", 3),
    "qwen": Profile("qwen", "local-opencode", "local/qwen3.8-27b-q8-tuber", None, 4),
}
CONTROL = ("astra", "opus")
HIGH_RISK = ("HOTSPOT", "CONTRACT_CHANGE", "DECOUPLE")
VERIFIED_AT = "2026-09-30"
# Approved upgrades only: model discovery must never silently choose another tier.
# Explicit project pins and a job's persisted coordinator bypass these upgrades.
SUPERSEDED = {"sol": ("gpt-5.6-sol", "gpt-6-sol"), "opus": ("claude-opus-5",)}


def profile(project, name: str, *, control=False) -> Profile:
    if name not in PROFILES:
        raise ValueError(f"unknown model profile: {name}")
    result = PROFILES[name]
    settings = (getattr(project, "raw", {}).get("model_policy") or {}).get("profiles", {}).get(name, {})
    result = replace(result, model=settings.get("model", result.model), effort=settings.get("effort", result.effort))
    if not settings.get("pinned", False) and result.model in SUPERSEDED.get(name, ()):
        result = replace(result, model=PROFILES[name].model)
    if control and name not in CONTROL:
        raise ValueError("only Astra or Opus may coordinate or approve work")
    if result.model == "gpt-6.1-sol" and result.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("GPT-6.1 Sol requires low, medium, high, xhigh or max effort; none/minimal are unsupported")
    return result


def enabled(project, name):
    return (getattr(project, "raw", {}).get("model_policy") or {}).get("profiles", {}).get(name, {}).get("enabled", True)


def usable(conn, project, candidate: Profile, *, pinned=False) -> bool:
    if (not pinned and not enabled(project, candidate.name)) or not providers.is_available(conn, candidate.provider):
        return False
    row = conn.execute("SELECT retry_at FROM model_unavailable WHERE provider=? AND model=?",
                       (candidate.provider, candidate.model)).fetchone()
    return not row or (db.parse_ts(row["retry_at"]) or datetime.max.replace(tzinfo=UTC)) <= datetime.now(UTC)


def reject(conn, provider, model, reason):
    conn.execute("INSERT INTO model_unavailable VALUES(?,?,?,?) ON CONFLICT(provider,model) DO UPDATE SET reason=excluded.reason,retry_at=excluded.retry_at",
                 (provider, model, reason[:1000], (datetime.now(UTC) + timedelta(hours=1)).isoformat()))
    db.log_event(conn, None, "model_unavailable", cause=reason, detail={"provider": provider, "model": model})


def worker_candidates(project, task, trigger=None):
    if task.get("role") in ("coordinator", "reviewer"):
        raise ValueError("coordinator and reviewer are supervisor-controlled roles, not worker tasks")
    from . import policy
    pinned = policy.for_task(project, task)
    if pinned is not None:
        # An explicit assignment never falls through the ranked list: only its
        # listed fallbacks, and only for the failure class they were approved for.
        return pinned.candidates(trigger)
    requested = task.get("model_profile")
    easy = task.get("complexity", "standard") == "easy" and task["kind"] not in HIGH_RISK
    names = ["sol", "opus", "sonnet"] if easy else ["sol", "opus"]
    # Free local work is a deliberate coordinator choice, never an automatic
    # downgrade of a complex task when cloud quotas are spent.
    if requested:
        if requested == "astra" or requested not in PROFILES:
            raise ValueError("worker profile must be sol, opus, sonnet or qwen")
        if requested in ("sonnet", "qwen") and not easy:
            raise ValueError("Sonnet and Qwen require an easy task without a high-risk kind")
        if requested not in ("sonnet", "qwen") or not task.get("attempts", 0):
            names = [requested, *[name for name in names if name != requested]]
    return [profile(project, name) for name in names if enabled(project, name)]


def allowed_workers(conn, project, task):
    """Worker candidates after the last recorded failure class is applied."""
    from . import policy
    candidates = worker_candidates(project, task, policy.active_triggers(conn, project, task))
    transfer = conn.execute("SELECT target_provider,target_model,evidence FROM handoffs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                            (task.get("id"),)).fetchone()
    if transfer and policy.for_task(project, task) is not None:
        import json
        evidence = json.loads(transfer["evidence"])
        assignment = policy.for_task(project, task)
        assert assignment is not None
        approved = [assignment.primary, *[f.profile for f in assignment.fallbacks]]
        return [p for p in approved if p.provider == transfer["target_provider"]
                and p.model == transfer["target_model"] and p.effort == evidence.get("target_effort")]
    return candidates


def waiting_provider(conn, project, task, cooling):
    """The cooling account an unlaunchable task is actually waiting for."""
    for candidate in allowed_workers(conn, project, task):
        if candidate.provider in cooling or not providers.is_available(conn, candidate.provider):
            return candidate.provider
    return None


def _rejected(conn, candidate) -> bool:
    row = conn.execute("SELECT retry_at FROM model_unavailable WHERE provider=? AND model=?",
                       (candidate.provider, candidate.model)).fetchone()
    return bool(row) and (db.parse_ts(row["retry_at"]) or datetime.max.replace(tzinfo=UTC)) > datetime.now(UTC)


def choose_worker(conn, project, task, capabilities, *, unavailable=None):
    from . import policy
    cooling = unavailable or {}
    reasons = []
    waiting = False
    pinned = policy.for_task(project, task) is not None
    for candidate in allowed_workers(conn, project, task):
        if candidate.provider == "local-opencode" and task["kind"] != "RESEARCH":
            reasons.append("qwen: read-only research until filesystem isolation is implemented")
            continue
        caps = capabilities.get(candidate.provider)
        if not caps or not caps.can_run(task["kind"]):
            reasons.append(f"{candidate.name}: runtime lacks measured capabilities for {task['kind']}")
            continue
        if pinned and _rejected(conn, candidate):
            # A rejected pinned model needs a decision, not an hourly retry loop.
            reasons.append(f"{candidate.name}: pinned model {candidate.model} was rejected by "
                           f"{candidate.provider}; approve a fallback for MODEL_UNAVAILABLE or change the assignment")
            continue
        if candidate.provider in cooling or not usable(conn, project, candidate):
            waiting = True
            reasons.append(f"{candidate.name} ({candidate.provider}): model or account unavailable")
            continue
        if candidate.provider == "local-opencode" and any(p["provider"] == candidate.provider for p in processes.active(conn)):
            reasons.append("qwen: single GPU worker is occupied")
            continue
        return candidate, f"{candidate.name} (rank {candidate.rank}) selected for {task.get('complexity', 'standard')} task"
    prefix = "provider unavailable: " if waiting else "no eligible model: "
    return None, prefix + "; ".join(reasons)


def control_candidates(project, job, purpose):
    from . import policy
    if purpose == "coordinator" and job.get("coordinator_model"):
        pinned = job["coordinator_model"]
        return [Profile(**pinned)]
    providers_allowed = job.get("reviewers", []) if purpose != "coordinator" else (
        [job["coordinator"]] if job["coordinator"] != "auto" else ["codex", "claude-code"])
    role_name = "coordinator" if purpose == "coordinator" else "reviewer"
    explicit = policy.role(project, role_name)
    if explicit is not None:
        if explicit.primary.provider not in providers_allowed:
            raise ValueError(f"model_policy.roles.{role_name} uses {explicit.primary.provider}, but job "
                             f"{job['id']} allows {', '.join(providers_allowed) or 'no provider'} for that role")
        # Exactly the configured model; never the legacy Astra/Opus list.
        return [explicit.primary]
    return [profile(project, name, control=True) for name in CONTROL
            if PROFILES[name].provider in providers_allowed]


def for_launch(project, task, provider, role):
    from . import policy
    pinned = policy.coordinator_pin(project, task) if role == "coordinator" else None
    if task.get("_model_selection"):
        selected = Profile(**task["_model_selection"])
        if selected.provider != provider:
            raise ValueError("selected model belongs to a different runtime")
        if role in ("coordinator", "reviewer") and not policy.control_allowed(project, role, selected, pinned):
            raise ValueError("control roles require the job's persisted coordinator pin, the exact "
                             "model_policy.roles entry, or (with neither) Astra or Opus at configured effort (default high)")
        return selected
    if role in ("coordinator", "reviewer"):
        explicit = None if pinned else policy.role(project, role)
        candidates = [Profile(**pinned)] if pinned else [explicit.primary] if explicit else [
            profile(project, name, control=True) for name in CONTROL]
    else:
        candidates = worker_candidates(project, {"kind": "SAFE_PARALLEL", **task})
    candidate = next((p for p in candidates if p.provider == provider), None)
    if candidate is None:
        raise ValueError(f"no authorized model for {role} on {provider}")
    return candidate


def catalog(project):
    """Profile defaults; explicit role and assignment pins are in `policy.describe`."""
    return [{**profile(project, name).to_dict(), "enabled": enabled(project, name),
             "verified_at": VERIFIED_AT, "supersedes": list(SUPERSEDED.get(name, ())),
             "control_effort": profile(project, name, control=True).effort if name in CONTROL else None} for name in PROFILES]
