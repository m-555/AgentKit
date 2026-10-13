"""F17: the manager packet stays readable however large the recovery audit grows.

`manager status`, `job_brief(include_history=True)` and the coordinator's
recovery prompt all embed the manager packet. A pilot project's epoch-47 audit report was
about 2 MB, so status printed 2 MB and an agent asking for history would have
received it in its context. The packet now summarises bulky fields;
`manager status --full` still prints everything.
"""
from __future__ import annotations

import json

from agentkit import cli, manager

JOB = "compact-job"
REPORT = json.dumps({"integration": {"head": "abc"}, "contracts": {"x": "y" * 1_500_000}})


def _recorded(conn):
    conn.execute(
        "INSERT INTO manager_state (job_id, epoch, acknowledged_epoch, audit_epoch, audit_digest, audit_report, "
        "authorized_signatures, updated_at) VALUES (?, 2, 2, 2, 'feedface', ?, ?, '2026-10-09T00:00:00+00:00')",
        (JOB, REPORT, json.dumps({str(i): "s" * 400 for i in range(40)})))


def test_the_packet_summarises_a_bulky_audit_report(conn):
    _recorded(conn)
    recovery = manager.packet(conn, JOB)["recovery"]
    assert len(json.dumps(recovery)) < 4000
    assert recovery["audit_digest"] == "feedface" and recovery["epoch"] == 2
    assert recovery["audit_report"] == {"omitted_characters": len(REPORT), "keys": ["contracts", "integration"],
                                        "full": f"agentkit manager status {JOB} --full"}
    assert recovery["authorized_signatures"]["omitted_characters"] > 2000


def test_full_keeps_every_character(conn):
    _recorded(conn)
    assert manager.packet(conn, JOB, full=True)["recovery"]["audit_report"] == REPORT


def test_status_is_compact_unless_full_is_asked(project_root, conn, monkeypatch, capsys):
    _recorded(conn)
    conn.commit()
    monkeypatch.chdir(project_root)
    assert cli.main(["manager", "status", JOB]) == 0
    compact = capsys.readouterr().out
    assert len(compact) < 6000 and "omitted_characters" in compact
    assert cli.main(["manager", "status", JOB, "--full"]) == 0
    assert len(capsys.readouterr().out) > len(REPORT)
