"""Feedback storage utilities supporting Azure Blob Storage (NDJSON)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterable, Optional


try:  # Optional Azure dependency
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import AppendBlobClient, BlobServiceClient
except Exception:  # pragma: no cover - azure extras optional
    AppendBlobClient = None  # type: ignore
    BlobServiceClient = None  # type: ignore
    ResourceExistsError = Exception  # type: ignore


class FeedbackStorageError(RuntimeError):
    """Raised when feedback persistence fails."""


class AzureBlobFeedbackStore:
    """Append feedback records to an Azure Append Blob."""

    def __init__(
        self,
        fieldnames: Iterable[str],
        connection_string: str,
        container_name: str,
        blob_name: str,
    ) -> None:
        if AppendBlobClient is None:
            raise FeedbackStorageError(
                "azure-storage-blob package is required for Azure feedback storage"
            )

        self._fieldnames = list(fieldnames)
        self._connection_string = connection_string
        self._container_name = container_name
        self._blob_name = blob_name
        self._append_client: Optional[AppendBlobClient] = None

    def _client(self) -> AppendBlobClient:
        if self._append_client is None:
            service_client = BlobServiceClient.from_connection_string(
                self._connection_string
            )
            container_client = service_client.get_container_client(self._container_name)
            try:
                container_client.create_container()
            except ResourceExistsError:
                pass

            append_client = AppendBlobClient.from_connection_string(
                self._connection_string,
                self._container_name,
                self._blob_name,
            )
            try:
                append_client.get_blob_properties()
            except Exception:
                append_client.create_append_blob()

            self._append_client = append_client
        return self._append_client

    def append(self, row: Dict[str, str]) -> None:
        payload = self._serialize_row(row)
        try:
            self._client().append_block(payload + "\n")
        except Exception as exc:
            raise FeedbackStorageError(
                f"Failed to append feedback to Azure blob '{self._container_name}/{self._blob_name}': {exc}"
            ) from exc

    def _serialize_row(self, row: Dict[str, str]) -> str:
        ordered = {key: row.get(key, "") for key in self._fieldnames}
        return json.dumps(ordered, ensure_ascii=False)


class LocalFeedbackStore:
    """Fallback feedback store writing NDJSON locally."""

    def __init__(self, fieldnames: Iterable[str], path: Path) -> None:
        self._fieldnames = list(fieldnames)
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            with self._path.open("w", encoding="utf-8") as fp:
                fp.write("")

    def append(self, row: Dict[str, str]) -> None:
        ordered = {key: row.get(key, "") for key in self._fieldnames}
        with self._path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(ordered, ensure_ascii=False) + "\n")


class FeedbackStore:
    """Coordinator that prefers Azure but falls back to local CSV."""

    def __init__(
        self,
        fieldnames: Iterable[str],
        local_path: Path,
        *,
        connection_string_env: str = "AZURE_FEEDBACK_CONNECTION_STRING",
        container_env: str = "AZURE_FEEDBACK_CONTAINER",
        blob_env: str = "AZURE_FEEDBACK_BLOB",
    ) -> None:
        self._fieldnames = list(fieldnames)
        self._local = LocalFeedbackStore(fieldnames, local_path)

        connection_string = os.getenv(connection_string_env)
        container_name = os.getenv(container_env)
        blob_name = os.getenv(blob_env)

        self.azure_error: Optional[str] = None
        if connection_string and container_name and blob_name:
            try:
                self._azure = AzureBlobFeedbackStore(
                    self._fieldnames,
                    connection_string,
                    container_name,
                    blob_name,
                )
            except Exception as exc:
                self.azure_error = str(exc)
                self._azure = None
        else:
            self._azure = None

    def append(self, row: Dict[str, str]) -> None:
        normalized = {key: row.get(key, "") for key in self._fieldnames}
        if self._azure is not None:
            try:
                self._azure.append(normalized)
                return
            except FeedbackStorageError as exc:
                self.azure_error = str(exc)
        self._local.append(normalized)


def build_feedback_store(fieldnames: Iterable[str], local_path: Path) -> FeedbackStore:
    return FeedbackStore(fieldnames=fieldnames, local_path=local_path)
