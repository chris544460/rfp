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


class FeedbackStore:
    """Feedback storage that requires Azure Blob Storage when configured."""

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

        connection_string = os.getenv(connection_string_env)
        container_name = os.getenv(container_env)
        blob_name = os.getenv(blob_env)

        self._azure: Optional[AzureBlobFeedbackStore] = None
        self.azure_error: Optional[str] = None
        self._azure_configured = bool(connection_string and container_name and blob_name)

        if self._azure_configured:
            try:
                self._azure = AzureBlobFeedbackStore(
                    self._fieldnames,
                    connection_string or "",
                    container_name or "",
                    blob_name or "",
                )
            except Exception as exc:
                self.azure_error = str(exc)

    def append(self, row: Dict[str, str]) -> None:
        normalized = {key: row.get(key, "") for key in self._fieldnames}
        if not self._azure_configured:
            raise FeedbackStorageError(
                "Azure feedback storage is not configured. Set AZURE_FEEDBACK_CONNECTION_STRING, "
                "AZURE_FEEDBACK_CONTAINER, and AZURE_FEEDBACK_BLOB."
            )
        if self._azure is None:
            raise FeedbackStorageError(
                self.azure_error or "Azure feedback storage is not available"
            )
        try:
            self._azure.append(normalized)
        except FeedbackStorageError as exc:
            self.azure_error = str(exc)
            raise


def build_feedback_store(fieldnames: Iterable[str], local_path: Path) -> FeedbackStore:
    return FeedbackStore(fieldnames=fieldnames, local_path=local_path)
