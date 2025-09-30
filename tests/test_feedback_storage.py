import json
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_feedback_module():
    module_name = "feedback_storage"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "feedback_storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


FIELDNAMES = [
    "timestamp",
    "session_id",
    "user_id",
    "feedback_source",
]


def test_feedback_store_appends_records(tmp_path: Path):
    module = load_feedback_module()
    log_path = tmp_path / "feedback" / "log.ndjson"
    store = module.FeedbackStore(fieldnames=FIELDNAMES, local_path=log_path)

    record = {
        "timestamp": "2025-01-01T00:00:00Z",
        "session_id": "sess-1",
        "user_id": "user-1",
        "feedback_source": "chat",
    }
    store.append(record)

    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(contents) == 1
    assert json.loads(contents[0]) == record


def test_feedback_store_fills_missing_fields(tmp_path: Path):
    module = load_feedback_module()
    log_path = tmp_path / "feedback.ndjson"
    store = module.FeedbackStore(fieldnames=FIELDNAMES, local_path=log_path)

    store.append({"timestamp": "2025-02-01T00:00:00Z"})

    payload = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert payload == {
        "timestamp": "2025-02-01T00:00:00Z",
        "session_id": "",
        "user_id": "",
        "feedback_source": "",
    }


def test_feedback_store_raises_on_io_error(tmp_path: Path, monkeypatch):
    module = load_feedback_module()
    log_path = tmp_path / "log.ndjson"
    store = module.FeedbackStore(fieldnames=FIELDNAMES, local_path=log_path)

    def boom(*args, **kwargs):  # pragma: no cover - intentionally raises
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", boom, raising=False)

    with pytest.raises(module.FeedbackStorageError) as excinfo:
        store.append({"timestamp": "2025-03-01T00:00:00Z"})

    assert "disk full" in str(excinfo.value)
