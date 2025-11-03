#!/usr/bin/env python3
"""Simple CLI to verify Azure Blob Storage connectivity."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Optional

from backend.utils.dotenv import load_dotenv

try:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import BlobServiceClient
except ImportError as exc:  # pragma: no cover - optional dependency
    print("ERROR: Missing Azure SDK dependency. Install with: pip install azure-storage-blob")
    sys.exit(1)


def _resolve_connection_string(explicit: Optional[str]) -> str:
    """Pick the first non-empty connection string option."""
    if explicit:
        return explicit
    env_candidates = [
        os.getenv("AZURE_FEEDBACK_CONNECTION_STRING"),
        os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
    ]
    for candidate in env_candidates:
        if candidate:
            return candidate
    raise SystemExit(
        "Azure connection string missing. Pass --connection-string or set AZURE_FEEDBACK_CONNECTION_STRING / AZURE_STORAGE_CONNECTION_STRING."
    )


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that an Azure Blob Storage connection string is valid.",
    )
    parser.add_argument(
        "--connection-string",
        help="Azure Storage connection string (falls back to AZURE_FEEDBACK_CONNECTION_STRING or AZURE_STORAGE_CONNECTION_STRING).",
    )
    parser.add_argument(
        "--container",
        help="Optional container name to verify accessibility.",
    )
    parser.add_argument(
        "--write-probe",
        action="store_true",
        help="Attempt to upload and delete a temporary blob to check write permissions (requires --container).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print additional account metadata when available.",
    )
    return parser.parse_args(argv)


def _check_container(service: BlobServiceClient, container_name: str) -> None:
    container_client = service.get_container_client(container_name)
    try:
        container_client.get_container_properties()
    except ResourceNotFoundError as exc:
        raise SystemExit(f"Container '{container_name}' not found or inaccessible: {exc}")


def _check_write_access(service: BlobServiceClient, container_name: str) -> None:
    container_client = service.get_container_client(container_name)
    blob_name = f"azure-connection-check-write-probe-{uuid.uuid4().hex}"
    blob_client = container_client.get_blob_client(blob_name)
    payload = b"azure connection check write probe"

    try:
        blob_client.upload_blob(payload, length=len(payload))
    except Exception as exc:
        raise SystemExit(
            f"Failed to upload probe blob to container '{container_name}'. This credential likely lacks write permissions: {exc}"
        )

    print(
        f"Successfully uploaded probe blob '{blob_name}' to container '{container_name}'. Write access appears to be working."
    )

    try:
        blob_client.delete_blob()
    except ResourceNotFoundError:
        pass
    except Exception as exc:
        print(
            f"WARNING: Unable to delete probe blob '{blob_name}' from container '{container_name}': {exc}",
            file=sys.stderr,
        )


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    # Load environment variables from a .env file before resolving credentials.
    load_dotenv(override=False)
    connection_string = _resolve_connection_string(args.connection_string)

    try:
        service_client = BlobServiceClient.from_connection_string(connection_string)
        account_info = service_client.get_account_information()
    except Exception as exc:
        raise SystemExit(f"Failed to connect to Azure Blob Storage: {exc}")

    account_kind = account_info.get("accountKind", "unknown")
    sku_name = account_info.get("skuName", "unknown")
    print(f"Connected to Azure Blob Storage account (kind={account_kind}, sku={sku_name}).")

    if args.verbose and account_info:
        for key, value in sorted(account_info.items()):
            print(f"  {key}: {value}")

    if args.container:
        _check_container(service_client, args.container)
        print(f"Verified access to container '{args.container}'.")

        if args.write_probe:
            _check_write_access(service_client, args.container)
    elif args.write_probe:
        raise SystemExit("--write-probe requires --container to be set.")


if __name__ == "__main__":
    main()
