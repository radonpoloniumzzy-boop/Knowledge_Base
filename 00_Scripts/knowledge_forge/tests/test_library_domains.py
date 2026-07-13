from __future__ import annotations

from knowledge_forge import services
from knowledge_forge.db import connect, init_db


def test_domain_filter_allows_cross_domain_membership_and_paginates(tmp_path, monkeypatch):
    db = tmp_path / "knowledge.db"
    init_db(db, tmp_path / "managed")
    with connect(db) as conn:
        for index in range(55):
            file_id = conn.execute(
                "INSERT INTO files(source_path,library_type,title,filename,main_category,status) VALUES (?, 'standard', ?, ?, '08_Art', 'completed')",
                (f"C:/{index}.md", f"Art {index:02}", f"{index}.md"),
            ).lastrowid
            for tag in (["08_Art/色彩", "03_Sales/表达"] if index == 0 else ["08_Art/色彩"]):
                conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
                tag_id = conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()[0]
                conn.execute("INSERT INTO tag_assignments(target_type,target_id,tag_id,scope) VALUES ('file',?,?,'file_strong')", (file_id, tag_id))
        conn.commit()
    monkeypatch.setattr(services, "connect", lambda: connect(db))
    domains = services.list_knowledge_domains()
    art = next(item for item in domains if item["domain"]["name"] == "艺术")
    sales = next(item for item in domains if item["domain"]["name"] == "销售")

    first = services.search_library_page(domain=art["domain"]["id"], page_size=25, page=1, sort="title_asc")
    last = services.search_library_page(domain=art["domain"]["id"], page_size=25, page=3, sort="title_asc")

    assert art["count"] == 55
    assert sales["count"] == 1
    assert first.total == 55 and first.page_count == 3 and len(first.items) == 25
    assert len(last.items) == 5
    assert first.items[0]["title"] == "Art 00"


def test_invalid_page_size_and_sort_use_safe_defaults(tmp_path, monkeypatch):
    db = tmp_path / "knowledge.db"
    init_db(db, tmp_path / "managed")
    monkeypatch.setattr(services, "connect", lambda: connect(db))

    result = services.search_library_page(page=-3, page_size=999, sort="drop table")

    assert result.page == 1
    assert result.page_size == 50
    assert result.page_count == 1
