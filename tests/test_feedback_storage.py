import json
import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Optional, Tuple
from uuid import uuid4

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


MANUAL_AZURE_TEST_CONFIG = {
    "connection_string": "",
    "container": "",
    "blob_name": "",
}


def _resolve_live_azure_config() -> Optional[Tuple[str, str, str]]:
    connection = os.getenv("AZURE_FEEDBACK_CONNECTION_STRING") or MANUAL_AZURE_TEST_CONFIG["connection_string"].strip()
    container = os.getenv("AZURE_FEEDBACK_CONTAINER") or MANUAL_AZURE_TEST_CONFIG["container"].strip()
    blob_name = os.getenv("AZURE_FEEDBACK_BLOB") or MANUAL_AZURE_TEST_CONFIG["blob_name"].strip()

    if connection and container and blob_name:
        return connection, container, blob_name
    return None


@pytest.fixture(autouse=True)
def clear_azure_env(monkeypatch):
    monkeypatch.delenv("AZURE_FEEDBACK_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AZURE_FEEDBACK_CONTAINER", raising=False)
    monkeypatch.delenv("AZURE_FEEDBACK_BLOB", raising=False)


def test_feedback_store_requires_azure_configuration(tmp_path: Path):
    module = load_feedback_module()
    store = module.FeedbackStore(fieldnames=FIELDNAMES, local_path=tmp_path / "unused.ndjson")

    with pytest.raises(module.FeedbackStorageError) as excinfo:
        store.append({
            "timestamp": "2025-01-01T00:00:00Z",
            "session_id": "sess-1",
            "user_id": "user-1",
            "feedback_source": "chat",
        })

    assert "not configured" in str(excinfo.value)


def test_feedback_store_azure_append(monkeypatch, tmp_path: Path):
    connection = "UseDevelopmentStorage=true"
    container = "feedback"
    blob_name = "feedback-log.ndjson"

    monkeypatch.setenv("AZURE_FEEDBACK_CONNECTION_STRING", connection)
    monkeypatch.setenv("AZURE_FEEDBACK_CONTAINER", container)
    monkeypatch.setenv("AZURE_FEEDBACK_BLOB", blob_name)

    fake_blocks: list[str] = []

    class FakeContainerClient:
        def __init__(self) -> None:
            self.created = False

        def create_container(self) -> None:
            self.created = True

    class FakeBlobServiceClient:
        def __init__(self, conn: str) -> None:
            self.conn = conn
            self.container_client = FakeContainerClient()

        @classmethod
        def from_connection_string(cls, conn: str):
            return cls(conn)

        def get_container_client(self, name: str) -> FakeContainerClient:
            self.container_name = name
            return self.container_client

    class FakeAppendBlobClient:
        instances = []

        def __init__(self, conn: str, cont: str, blob: str) -> None:
            self.conn = conn
            self.container = cont
            self.blob = blob
            self.exists = False
            self.blocks = fake_blocks
            FakeAppendBlobClient.instances.append(self)

        @classmethod
        def from_connection_string(cls, conn: str, cont: str, blob: str):
            return cls(conn, cont, blob)

        def get_blob_properties(self):  # pragma: no cover - probe path
            if not self.exists:
                raise Exception("missing")

        def create_append_blob(self):
            self.exists = True

        def append_block(self, payload: str):
            fake_blocks.append(payload)

    class FakeResourceExistsError(Exception):
        pass

    azure_pkg = types.ModuleType("azure")
    azure_core = types.ModuleType("azure.core")
    azure_core_ex = types.ModuleType("azure.core.exceptions")
    azure_core_ex.ResourceExistsError = FakeResourceExistsError
    azure_storage = types.ModuleType("azure.storage")
    azure_storage_blob = types.ModuleType("azure.storage.blob")
    azure_storage_blob.BlobServiceClient = FakeBlobServiceClient
    azure_storage_blob.AppendBlobClient = FakeAppendBlobClient

    azure_storage.blob = azure_storage_blob
    azure_pkg.core = azure_core
    azure_pkg.storage = azure_storage
    azure_core.exceptions = azure_core_ex

    monkeypatch.setitem(sys.modules, "azure", azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_ex)
    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_storage_blob)

    module = load_feedback_module()
    local_file = tmp_path / "feedback.ndjson"
    store = module.FeedbackStore(fieldnames=FIELDNAMES, local_path=local_file)

    record = {
        "timestamp": "2025-01-01T00:02:00Z",
        "session_id": "sess-azure",
        "user_id": "user-azure",
        "feedback_source": "chat",
    }
    store.append(record)

    expected_payload = json.dumps(record, ensure_ascii=False) + "\n"
    assert fake_blocks == [expected_payload]
    assert not local_file.exists()
    assert store.azure_error is None


def test_feedback_store_azure_append_failure(monkeypatch, tmp_path: Path):
    connection = "UseDevelopmentStorage=true"
    container = "feedback"
    blob_name = "feedback-log.ndjson"

    monkeypatch.setenv("AZURE_FEEDBACK_CONNECTION_STRING", connection)
    monkeypatch.setenv("AZURE_FEEDBACK_CONTAINER", container)
    monkeypatch.setenv("AZURE_FEEDBACK_BLOB", blob_name)

    class FailingAppendBlobClient:
        def __init__(self, conn: str, cont: str, blob: str) -> None:
            self.conn = conn
            self.container = cont
            self.blob = blob

        @classmethod
        def from_connection_string(cls, conn: str, cont: str, blob: str):
            return cls(conn, cont, blob)

        def get_blob_properties(self):  # pragma: no cover
            raise Exception("missing")

        def create_append_blob(self):
            pass

        def append_block(self, payload: str):
            raise RuntimeError("simulated failure")

    class FakeBlobServiceClient:
        def __init__(self, conn: str) -> None:
            self.conn = conn

        @classmethod
        def from_connection_string(cls, conn: str):
            return cls(conn)

        def get_container_client(self, name: str):
            class _Container:
                def create_container(self_inner):
                    pass

            self.container_name = name
            return _Container()

    class FakeResourceExistsError(Exception):
        pass

    azure_pkg = types.ModuleType("azure")
    azure_core = types.ModuleType("azure.core")
    azure_core_ex = types.ModuleType("azure.core.exceptions")
    azure_core_ex.ResourceExistsError = FakeResourceExistsError
    azure_storage = types.ModuleType("azure.storage")
    azure_storage_blob = types.ModuleType("azure.storage.blob")
    azure_storage_blob.BlobServiceClient = FakeBlobServiceClient
    azure_storage_blob.AppendBlobClient = FailingAppendBlobClient

    azure_storage.blob = azure_storage_blob
    azure_pkg.core = azure_core
    azure_pkg.storage = azure_storage
    azure_core.exceptions = azure_core_ex

    monkeypatch.setitem(sys.modules, "azure", azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_ex)
    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_storage_blob)

    module = load_feedback_module()
    store = module.FeedbackStore(fieldnames=FIELDNAMES, local_path=tmp_path / "local.ndjson")

    with pytest.raises(module.FeedbackStorageError) as excinfo:
        store.append({
            "timestamp": "2025-01-01T00:03:00Z",
            "session_id": "sess-fail",
            "user_id": "user-fail",
            "feedback_source": "chat",
        })

    assert "simulated failure" in str(excinfo.value)


@pytest.mark.live_azure
def test_feedback_store_live_azure(tmp_path: Path):
    if os.getenv("RUN_LIVE_AZURE_TEST") != "1":
        pytest.skip("Set RUN_LIVE_AZURE_TEST=1 to enable live Azure test")

    resolved = _resolve_live_azure_config()
    if not resolved:
        pytest.skip(
            "Azure feedback configuration missing. Set env vars or populate MANUAL_AZURE_TEST_CONFIG."
        )

    connection, container, blob_name = resolved

    try:
        from azure.storage.blob import BlobServiceClient
    except Exception as exc:  # pragma: no cover - depends on environment
        pytest.skip(f"Azure SDK not available: {exc}")

    module = load_feedback_module()
    local_file = tmp_path / "feedback.ndjson"
    store = module.FeedbackStore(fieldnames=FIELDNAMES, local_path=local_file)

    unique_session = f"azure-test-{uuid4()}"
    record = {
        "timestamp": "2025-01-01T00:05:00Z",
        "session_id": unique_session,
        "user_id": "integration-test",
        "feedback_source": "integration",
    }

    store.append(record)

    if store.azure_error:
        pytest.fail(f"Azure append failed: {store.azure_error}")

    assert not local_file.exists()

    blob_client = BlobServiceClient.from_connection_string(connection).get_blob_client(
        container=container, blob=blob_name
    )
    contents = blob_client.download_blob().readall().decode("utf-8")

    assert unique_session in contents
