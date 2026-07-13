from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .migrations import MigrationReport, MigrationRunner


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "Knowledge_Forge"
DB_PATH = DATA_DIR / "knowledge_forge.db"


def connect(database_path: Path = DB_PATH) -> sqlite3.Connection:
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(
    database_path: Path = DB_PATH,
    managed_data_dir: Path | None = None,
) -> MigrationReport:
    database_path = Path(database_path)
    data_dir = Path(managed_data_dir) if managed_data_dir is not None else database_path.parent
    return MigrationRunner(database_path, data_dir).migrate()


def scalar(query: str, params: Iterable[Any] = ()) -> Any:
    with connect() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
        if row is None:
            return None
        return row[0]


def rows(query: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(query, tuple(params)).fetchall()
