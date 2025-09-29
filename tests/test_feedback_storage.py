import json
from pathlib import Path

import pytest

import importlib.util

ROOT = Path(__file__).resolve().parents[1]


def load_feedback_store():
    spec = importlib.util.spec_from_file_location(
        "feedback_storage", ROOT / "feedback_storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module.FeedbackStore


FeedbackStore = load_feedback_store()


FIELDNAMES = [
    "timestamp",
    "session_id",
    "user_id",
    "feedback_source",
]


@pytest.fixture(autouse=True)
def clear_azure_env(monkeypatch):
    monkeypatch.delenv("AZURE_FEEDBACK_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AZURE_FEEDBACK_CONTAINER", raising=False)
    monkeypatch.delenv("AZURE_FEEDBACK_BLOB", raising=False)


def test_feedback_store_appends_ndjson(tmp_path: Path):
    target = tmp_path / "feedback.ndjson"
    store = FeedbackStore(fieldnames=FIELDNAMES, local_path=target)

    store.append({
        "timestamp": "2025-01-01T00:00:00Z",
        "session_id": "sess-1",
        "user_id": "user-1",
        "feedback_source": "chat",
    })
    store.append({
        "timestamp": "2025-01-01T00:01:00Z",
        "session_id": "sess-2",
        "user_id": "user-2",
        "feedback_source": "document",
    })

    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first, second = map(json.loads, lines)

    assert first == {
        "timestamp": "2025-01-01T00:00:00Z",
        "session_id": "sess-1",
        "user_id": "user-1",
        "feedback_source": "chat",
    }
    assert second == {
        "timestamp": "2025-01-01T00:01:00Z",
        "session_id": "sess-2",
        "user_id": "user-2",
        "feedback_source": "document",
    }
