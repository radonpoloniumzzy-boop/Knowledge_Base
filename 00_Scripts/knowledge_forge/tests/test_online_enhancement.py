import json

import pytest

from knowledge_forge.db import connect, init_db
from knowledge_forge.enhancement import (
    ConfiguredEnhancementAdapter,
    EnhancementConfigurationError,
)


def configured_database(tmp_path, offline="false"):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    values = {
        "active_provider": "Test provider",
        "api_base_url": "https://example.test/v1",
        "model_name": "test-model",
        "api_key_env": "TEST_MODEL_KEY",
        "offline_mode": offline,
    }
    with connect(database_path) as conn:
        conn.executemany("INSERT INTO settings(key, value) VALUES (?, ?)", values.items())
    return database_path


def test_online_adapter_generates_artifact_and_structured_classification(tmp_path, monkeypatch):
    database_path = configured_database(tmp_path)
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    calls = []

    def request(config, messages, json_mode=False):
        calls.append((config, messages, json_mode))
        if json_mode:
            return json.dumps({
                "category": "08_Art",
                "tags": [{"name": "08_Art/Color", "confidence": 0.91, "evidence": "color chapter"}],
            })
        return "# Online insight\n\nUseful result"

    adapter = ConfiguredEnhancementAdapter(database_path, request_fn=request)
    classification = adapter.classify("Color", "Source text")
    artifact = adapter.generate("insight", "Color", "Source text", "Extract insights")

    assert classification.category == "08_Art"
    assert classification.tags[0].confidence == 0.91
    assert artifact.content.startswith("# Online insight")
    assert calls[0][0]["model_name"] == "test-model"
    assert calls[0][2] is True
    assert calls[1][2] is False


def test_online_adapter_requires_configured_key_and_connection_test_is_safe(tmp_path, monkeypatch):
    database_path = configured_database(tmp_path)
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    adapter = ConfiguredEnhancementAdapter(database_path, request_fn=lambda *_args, **_kwargs: "OK")

    with pytest.raises(EnhancementConfigurationError, match="TEST_MODEL_KEY"):
        adapter.test_connection()


def test_offline_mode_never_calls_remote_transport(tmp_path):
    database_path = configured_database(tmp_path, offline="true")
    adapter = ConfiguredEnhancementAdapter(
        database_path,
        request_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("remote called")),
    )

    artifact = adapter.generate("insight", "Lesson", "Useful lesson content", "prompt")

    assert "离线草稿" in artifact.content
