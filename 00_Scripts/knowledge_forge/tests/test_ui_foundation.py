from __future__ import annotations

from fastapi.testclient import TestClient

from knowledge_forge import app as app_module


def test_dashboard_loads_shared_guild_ui_assets(monkeypatch):
    monkeypatch.setattr(
        app_module.services,
        "dashboard_stats",
        lambda: {
            "stats": {"files": 0, "standard": 0, "sop": 0, "insight": 0, "chunks": 0, "review": 0},
            "categories": [],
            "jobs": [],
            "packs": [],
        },
    )
    client = TestClient(app_module.app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'body data-page="dashboard"' in response.text
    assert 'src="/static/guild-ui.js"' in response.text
    assert 'href="/static/guild-icons.svg#dashboard"' in response.text


def test_shared_ui_assets_are_served():
    client = TestClient(app_module.app)

    script = client.get("/static/guild-ui.js")
    icons = client.get("/static/guild-icons.svg")

    assert script.status_code == 200
    assert "initSharedUi" in script.text
    assert icons.status_code == 200
    assert 'id="archetype-bard"' in icons.text
    assert 'id="archetype-adventurer"' in icons.text
