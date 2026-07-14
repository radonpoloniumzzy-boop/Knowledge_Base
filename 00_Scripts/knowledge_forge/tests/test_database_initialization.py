from __future__ import annotations

from pathlib import Path

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

    assert report.to_version == 13
    assert version == 13
    assert database_path.exists()


def test_launcher_checks_online_model_transport_dependency():
    launcher = (Path(__file__).parents[2] / "start_knowledge_forge.ps1").read_text(encoding="utf-8")

    assert "'httpx'" in launcher
