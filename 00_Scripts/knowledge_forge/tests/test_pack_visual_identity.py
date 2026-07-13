from __future__ import annotations

import json

from fastapi.testclient import TestClient

from knowledge_forge import app as app_module
from knowledge_forge import services
from knowledge_forge.db import connect, init_db


def test_pack_visual_identity_is_suggested_persisted_and_validated(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))

    pack_id = services.save_pack_recipe(
        None, "Painter", "", "08_Art/光影", emblem_color="#3f74c7"
    )
    with connect(database_path) as conn:
        created = conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
    assert created["emblem_color"] == "#3F74C7"
    assert created["archetype_key"] == "bard"

    services.save_pack_recipe(pack_id, "Renamed", "", "08_Art/光影")
    with connect(database_path) as conn:
        renamed = conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
    assert renamed["emblem_color"] == "#3F74C7"
    assert renamed["archetype_key"] == "bard"

    services.save_pack_recipe(
        pack_id, "Renamed", "", "08_Art/光影",
        emblem_color="#ffffff", archetype_key="wizard",
    )
    with connect(database_path) as conn:
        fallback = conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
    assert fallback["emblem_color"] == services.DEFAULT_PACK_COLOR
    assert fallback["archetype_key"] == services.DEFAULT_PACK_ARCHETYPE


def test_visual_identity_is_not_written_to_export_recipe(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))

    pack_id = services.save_pack_recipe(
        None, "Risk", "", "01_Finance", emblem_color="#9A654B", archetype_key="warden"
    )
    with connect(database_path) as conn:
        recipe = json.loads(conn.execute("SELECT recipe_json FROM packs WHERE id=?", (pack_id,)).fetchone()[0])

    assert "emblem_color" not in recipe
    assert "archetype_key" not in recipe


def test_pack_http_keeps_selected_identity_across_create_edit_and_delete(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    client = TestClient(app_module.app)

    created = client.post(
        "/packs/create",
        data={
            "name": "Market Artist",
            "selected_tags": "08_Art",
            "emblem_color": "#B05F8F",
            "archetype_key": "bard",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/packs?selected=1"

    page = client.get("/packs?selected=1")
    assert page.status_code == 200
    assert 'style="--emblem-color: #B05F8F"' in page.text
    assert "吟游诗人" in page.text
    assert 'aria-current="true"' in page.text

    saved = client.post(
        "/packs/1/save",
        data={
            "name": "Renamed Artist",
            "selected_tags": "08_Art",
            "emblem_color": "#9A654B",
            "archetype_key": "merchant",
        },
        follow_redirects=False,
    )
    assert saved.headers["location"] == "/packs?selected=1"
    with connect(database_path) as conn:
        row = conn.execute(
            "SELECT name, emblem_color, archetype_key FROM packs WHERE id=1"
        ).fetchone()
    assert tuple(row) == ("Renamed Artist", "#9A654B", "merchant")

    deleted = client.post("/packs/1/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/packs"
