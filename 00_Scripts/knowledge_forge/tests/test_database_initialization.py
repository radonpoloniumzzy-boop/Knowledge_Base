from __future__ import annotations

from knowledge_forge.db import connect, init_db


def test_database_initialization_uses_isolated_paths_and_migrations(tmp_path):
    database_path = tmp_path / "knowledge.db"
    managed_data_dir = tmp_path / "managed"

    report = init_db(database_path, managed_data_dir)

    with connect(database_path) as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES ('test-isolation', 'temporary')
            """
        )

    assert report.to_version == 5
    assert version == 5
    assert database_path.exists()
