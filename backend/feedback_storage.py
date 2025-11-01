"""Feedback storage utilities for logging feedback records.

The default implementation appends newline-delimited JSON rows to a local
file. When Azure Blob configuration is present, records are also written to an
append blob so that feedback is centralized remotely.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional


class FeedbackStorageError(RuntimeError):
    """Raised when feedback persistence fails."""


class FeedbackStore:
    """Feedback storage that appends rows to a local NDJSON file."""

    def __init__(
        self,
        fieldnames: Iterable[str],
        local_path: Path,
    ) -> None:
        self._fieldnames = list(fieldnames)
        self._local_path = local_path
        self._local_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, row: Dict[str, str]) -> None:
        normalized = _normalize_row(self._fieldnames, row)
        try:
            with self._local_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(normalized, ensure_ascii=False))
                handle.write("\n")
        except Exception as exc:  # pragma: no cover - file system issues
            raise FeedbackStorageError(
                f"Failed to append feedback to local log '{self._local_path}': {exc}"
            ) from exc


class AzureFeedbackStore:
    """Feedback storage that mirrors feedback rows to Azure Blob Storage."""

    def __init__(
        self,
        fieldnames: Iterable[str],
        local_path: Path,
        connection_string: str,
        container_name: str,
        blob_name: str,
    ) -> None:
        self._fieldnames = list(fieldnames)
        self._local_store = FeedbackStore(fieldnames=fieldnames, local_path=local_path)
        self._connection_string = connection_string
        self._container_name = container_name
        self._blob_name = blob_name
        self._init_lock = threading.Lock()
        self._append_lock = threading.Lock()
        self._blob_client = None

    def _ensure_client(self) -> None:
        if self._blob_client is not None:
            return
        with self._init_lock:
            if self._blob_client is not None:
                return
            try:
                from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
                from azure.storage.blob import BlobServiceClient
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise FeedbackStorageError(
                    "Azure feedback storage requested but azure-storage-blob is not installed."
                ) from exc

            try:
                service_client = BlobServiceClient.from_connection_string(self._connection_string)
                container_client = service_client.get_container_client(self._container_name)
                try:
                    container_client.get_container_properties()
                except ResourceNotFoundError:
                    try:
                        container_client.create_container()
                    except ResourceExistsError:
                        pass

                blob_client = container_client.get_blob_client(self._blob_name)
                if not hasattr(blob_client, "create_append_blob"):
                    raise FeedbackStorageError(
                        "Installed azure-storage-blob package must support append blobs (update to >=12.8)."
                    )
                try:
                    blob_client.get_blob_properties()
                except ResourceNotFoundError:
                    blob_client.create_append_blob()
            except Exception as exc:  # pragma: no cover - network / auth issues
                raise FeedbackStorageError(
                    f"Failed to initialize Azure feedback blob '{self._container_name}/{self._blob_name}': {exc}"
                ) from exc

            self._blob_client = blob_client

    def append(self, row: Dict[str, str]) -> None:
        normalized = _normalize_row(self._fieldnames, row)
        payload = json.dumps(normalized, ensure_ascii=False) + "\n"
        self._ensure_client()

        assert self._blob_client is not None  # for type-checkers

        if not hasattr(self._blob_client, "append_block"):
            raise FeedbackStorageError(
                "Installed azure-storage-blob package must expose append_block on blob clients."
            )

        with self._append_lock:
            try:
                self._blob_client.append_block(payload.encode("utf-8"))
            except Exception as exc:  # pragma: no cover - network / auth issues
                raise FeedbackStorageError(
                    f"Failed to append feedback to Azure blob '{self._blob_name}': {exc}"
                ) from exc

        # Mirror to the local log to retain parity with the previous behaviour.
        self._local_store.append(normalized)


def _normalize_row(fieldnames: Iterable[str], row: Dict[str, str]) -> Dict[str, str]:
    return {key: row.get(key, "") for key in fieldnames}


def build_feedback_store(fieldnames: Iterable[str], local_path: Path) -> FeedbackStore:
    connection_string = _resolve_connection_string()
    container_name = os.getenv("AZURE_FEEDBACK_CONTAINER")
    blob_name = os.getenv("AZURE_FEEDBACK_BLOB", local_path.name)

    if connection_string and container_name:
        return AzureFeedbackStore(
            fieldnames=fieldnames,
            local_path=local_path,
            connection_string=connection_string,
            container_name=container_name,
            blob_name=blob_name,
        )

    return FeedbackStore(fieldnames=fieldnames, local_path=local_path)


def _resolve_connection_string() -> Optional[str]:
    explicit = os.getenv("AZURE_FEEDBACK_CONNECTION_STRING")
    if explicit:
        return explicit
    fallback = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if fallback:
        return fallback
    return None
