"""Opening current state must not compete for a writer's lock."""
from agentkit import db


def test_current_database_connects_while_another_connection_writes(tmp_path, monkeypatch):
    writer = db.connect(tmp_path)
    monkeypatch.setattr(db, 'BUSY_TIMEOUT_MS', 25)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("INSERT INTO meta(key,value) VALUES('held', 'uncommitted')")
        reader = db.connect(tmp_path)
        try:
            assert reader.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
            assert reader.execute("SELECT value FROM meta WHERE key='held'").fetchone() is None
        finally:
            reader.close()
    finally:
        writer.rollback()
        writer.close()


def test_missing_version_is_repaired_without_losing_runtime_state(tmp_path):
    original = db.connect(tmp_path)
    original.execute("DELETE FROM meta WHERE key='schema_version'")
    original.execute("INSERT INTO meta(key,value) VALUES('saved', 'preserve')")
    original.close()
    current = db.connect(tmp_path)
    try:
        assert current.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
        assert current.execute("SELECT value FROM meta WHERE key='saved'").fetchone()[0] == 'preserve'
    finally:
        current.close()
