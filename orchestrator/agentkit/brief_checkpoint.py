"""Worker-facing checkpoint metadata; complete diagnostics remain durable."""


def compact(mechanical):
    keys = ("kind", "reason", "head_sha", "base_sha", "generation", "branch",
            "dirty_files", "staged_files", "blocker")
    result = {key: mechanical[key] for key in keys if key in mechanical}
    result["gates_run"] = [{key: gate.get(key) for key in ("level", "passed", "head_sha")}
                           for gate in mechanical.get("gates_run", [])]
    result["evidence_location"] = "durable mechanical checkpoint and gate results"
    return result


def scope(brief, mechanical):
    """Drop stale progress claims, not intent, authority or historical evidence."""
    last = brief.get("last_checkpoint") or {}
    if last.get("kind") == "mechanical":
        brief["last_checkpoint"] = compact(last)
    semantic = brief.get("last_semantic_checkpoint")
    head = (mechanical or {}).get("payload", {}).get("head_sha")
    if semantic and head and semantic.get("head_sha") and semantic["head_sha"] != head:
        payload = semantic.get("payload") or {}
        brief["last_semantic_checkpoint"] = {**semantic, "payload": {
            key: payload[key] for key in ("decisions", "assumptions", "blockers") if key in payload},
            "stale_progress_omitted": True}
