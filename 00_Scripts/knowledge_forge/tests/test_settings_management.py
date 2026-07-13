from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_forge import app as app_module
from knowledge_forge import services
from knowledge_forge.db import connect, init_db


def use_database(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    monkeypatch.setattr(services, "init_db", lambda: None)
    services.seed_defaults()
    return database_path


def test_domain_crud_replaces_selected_rules_without_touching_tags(tmp_path, monkeypatch):
    database_path = use_database(tmp_path, monkeypatch)
    with connect(database_path) as conn:
        conn.execute("INSERT INTO tags(name) VALUES ('08_Art/Color')")

    domain_id = services.save_knowledge_domain(
        None, "Visual Craft", "wine", ["08_Art"], ["08_Art/Color"]
    )
    services.save_knowledge_domain(
        domain_id, "Visual Practice", "brass", ["08_Art"], []
    )
    services.set_knowledge_domain_enabled(domain_id, False)
    domains = services.list_knowledge_domains(enabled_only=False)
    domain = next(item for item in domains if item["domain"]["id"] == domain_id)

    assert domain["domain"]["name"] == "Visual Practice"
    assert domain["domain"]["enabled"] == 0
    assert [(rule["rule_type"], rule["match_value"]) for rule in domain["rules"]] == [
        ("main_category", "08_Art")
    ]

    services.delete_knowledge_domain(domain_id)
    with connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM knowledge_domains WHERE id=?", (domain_id,)).fetchone()[0] == 0


def test_prompt_restore_copies_history_into_new_active_version(tmp_path, monkeypatch):
    database_path = use_database(tmp_path, monkeypatch)
    services.create_prompt_version("custom", "old content")
    services.create_prompt_version("custom", "new active content")
    with connect(database_path) as conn:
        old_id = conn.execute(
            "SELECT id FROM prompts WHERE name='custom' ORDER BY id LIMIT 1"
        ).fetchone()[0]

    version = services.restore_prompt_version(old_id)

    with connect(database_path) as conn:
        active = conn.execute("SELECT * FROM prompts WHERE name='custom' AND active=1").fetchone()
        history_count = conn.execute("SELECT COUNT(*) FROM prompts WHERE name='custom'").fetchone()[0]
    assert active["version"] == version
    assert active["content"] != "new active content"
    assert history_count == 3


def test_settings_page_sections_and_prompt_restore_route(tmp_path, monkeypatch):
    database_path = use_database(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    pages = [client.get(f"/settings?section={section}") for section in ("model", "domains", "tags", "prompts", "system")]
    with connect(database_path) as conn:
        prompt_id = conn.execute("SELECT id FROM prompts ORDER BY id LIMIT 1").fetchone()[0]
    restored = client.post(f"/settings/prompts/{prompt_id}/restore", follow_redirects=False)

    assert all(page.status_code == 200 for page in pages)
    assert 'data-settings-section="domains"' in pages[1].text
    assert restored.status_code == 303
    assert restored.headers["location"] == "/settings?section=prompts"
