from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from knowledge_forge import app as app_module
from knowledge_forge import services
from knowledge_forge.db import connect, init_db


def _add_current_file(conn, name: str, category: str | None, sub_category: str | None = None) -> int:
    file_id = conn.execute(
        """
        INSERT INTO files(source_path, library_type, title, filename, main_category, sub_category, status)
        VALUES (?, 'standard', ?, ?, ?, ?, 'completed')
        """,
        (f"C:/{name}.md", name, f"{name}.md", category, sub_category),
    ).lastrowid
    source_id = conn.execute(
        "INSERT INTO knowledge_sources(source_file_id) VALUES (?)", (file_id,)
    ).lastrowid
    version_id = conn.execute(
        """
        INSERT INTO source_versions(source_id, upload_file_id, standard_file_id, status, available_at)
        VALUES (?, ?, ?, 'available', CURRENT_TIMESTAMP)
        """,
        (source_id, file_id, file_id),
    ).lastrowid
    conn.execute(
        "UPDATE knowledge_sources SET current_version_id=? WHERE id=?",
        (version_id, source_id),
    )
    return int(file_id)


def _tag(conn, file_id: int, name: str, *, status: str = "auto_accepted") -> None:
    tag_id = conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,)).lastrowid
    if not tag_id:
        tag_id = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
    conn.execute(
        """
        INSERT INTO tag_assignments(target_type, target_id, tag_id, scope, status)
        VALUES ('file', ?, ?, 'file_strong', ?)
        """,
        (file_id, tag_id, status),
    )


def test_knowledge_map_uses_current_active_standard_files_and_limits_nodes(tmp_path, monkeypatch):
    database_path = tmp_path / "forge.db"
    init_db(database_path, tmp_path / "managed")
    with connect(database_path) as conn:
        art_files = [_add_current_file(conn, f"art-{index}", "08_Art") for index in range(7)]
        for index, file_id in enumerate(art_files):
            _tag(conn, file_id, f"08_Art/主题{index}")
        _tag(conn, art_files[0], "08_Art/已拒绝", status="user_rejected")

        sales_file = _add_current_file(conn, "sales", "03_Sales", "销售实战")
        _add_current_file(conn, "media", "02_Media", "内容运营")
        _add_current_file(conn, "coding", "04_Coding", "Python")
        _add_current_file(conn, "finance", "01_Finance", "风控")
        _add_current_file(conn, "ops", "05_Ops", "绩效")
        _add_current_file(conn, "law", "07_Law", "合规")
        _add_current_file(conn, "uncategorized", None)

        old_file = _add_current_file(conn, "old-art", "08_Art")
        source = conn.execute(
            "SELECT id FROM knowledge_sources WHERE source_file_id=?", (old_file,)
        ).fetchone()[0]
        replacement = conn.execute(
            """
            INSERT INTO files(source_path, library_type, title, filename, main_category, status)
            VALUES ('C:/new-sales.md', 'standard', 'new-sales', 'new-sales.md', '03_Sales', 'completed')
            """
        ).lastrowid
        current_version = conn.execute(
            """
            INSERT INTO source_versions(source_id, upload_file_id, standard_file_id, status, available_at)
            VALUES (?, ?, ?, 'available', CURRENT_TIMESTAMP)
            """,
            (source, replacement, replacement),
        ).lastrowid
        conn.execute(
            "UPDATE knowledge_sources SET current_version_id=? WHERE id=?", (current_version, source)
        )

        recycled = _add_current_file(conn, "recycled-art", "08_Art")
        conn.execute(
            "UPDATE knowledge_sources SET deleted_at=CURRENT_TIMESTAMP WHERE source_file_id=?",
            (recycled,),
        )
        conn.commit()

    monkeypatch.setattr(services, "connect", lambda: connect(database_path))

    result = services.knowledge_map()

    assert len(result["nodes"]) == 6
    assert result["nodes"][0]["key"] == "08_Art"
    assert result["nodes"][0]["count"] == 7
    assert len(result["nodes"][0]["children"]) == 4
    assert all(child["label"] != "已拒绝" for child in result["nodes"][0]["children"])
    assert result["nodes"][0]["url"] == f"/library?category={quote('08_Art')}"
    sales = next(node for node in result["nodes"] if node["key"] == "03_Sales")
    assert sales["count"] == 2
    assert sales["children"][0]["label"] == "销售实战"
    assert result["unclassified_count"] == 1
    assert all(node["key"] not in {"07_Law", "未分类"} for node in result["nodes"])


def test_dashboard_renders_expandable_constellation(monkeypatch):
    monkeypatch.setattr(
        app_module.services,
        "dashboard_stats",
        lambda: {
            "stats": {"files": 1, "standard": 1, "sop": 0, "insight": 0, "chunks": 2, "review": 0, "packs": 0},
            "categories": [],
            "jobs": [],
            "packs": [],
            "unclassified_count": 3,
            "knowledge_map": [
                {
                    "key": "08_Art",
                    "label": "艺术",
                    "count": 1,
                    "url": "/library?category=08_Art",
                    "children": [
                        {
                            "key": "08_Art/光影",
                            "label": "光影",
                            "count": 1,
                            "url": "/library?tag=08_Art%2F%E5%85%89%E5%BD%B1",
                            "source": "tag",
                        }
                    ],
                }
            ],
        },
    )

    response = TestClient(app_module.app).get("/")

    assert response.status_code == 200
    assert 'data-constellation' in response.text
    assert 'data-root-index="0"' in response.text
    assert 'aria-expanded="false"' in response.text
    assert "光影" in response.text
    assert "3" in response.text
