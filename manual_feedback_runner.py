"""Manual end-to-end Azure feedback append test.

Run with `python manual_feedback_runner.py` after exporting the
`AZURE_FEEDBACK_CONNECTION_STRING`, `AZURE_FEEDBACK_CONTAINER`, and
`AZURE_FEEDBACK_BLOB` environment variables. Optional CLI flags let you override
individual feedback fields; by default the script generates sensible sample
values. On success, the script prints the blob URI and the JSON payload that was
appended so you can confirm the record in Azure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional
from uuid import uuid4

from feedback_storage import FeedbackStorageError, build_feedback_store


FEEDBACK_FIELDS: Iterable[str] = [
    "timestamp",
    "session_id",
    "user_id",
    "feedback_source",
    "feedback_subject",
    "rating",
    "highlights",
    "improvements",
    "comment",
    "question",
    "answer",
    "context_json",
]


def _csv_option(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = [item.strip() for item in value.split(",")]
    return " | ".join(part for part in parts if part)


def _load_context(context_path: Optional[Path], inline_json: Optional[str]) -> str:
    if context_path:
        try:
            data = json.loads(context_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - manual script
            raise SystemExit(f"Failed to load context JSON from {context_path}: {exc}")
        return json.dumps(data, ensure_ascii=False)
    if inline_json:
        try:
            data = json.loads(inline_json)
        except Exception as exc:  # pragma: no cover - manual script
            raise SystemExit(f"Invalid inline context JSON: {exc}")
        return json.dumps(data, ensure_ascii=False)
    return ""


def _build_record(args: argparse.Namespace) -> Dict[str, str]:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record: Dict[str, str] = {
        "timestamp": timestamp,
        "session_id": args.session_id or f"manual-session-{uuid4()}"[:36],
        "user_id": args.user_id or "manual-tester",
        "feedback_source": args.feedback_source,
        "feedback_subject": args.feedback_subject,
        "rating": args.rating,
        "highlights": _csv_option(args.highlights),
        "improvements": _csv_option(args.improvements),
        "comment": args.comment,
        "question": args.question,
        "answer": args.answer,
        "context_json": _load_context(args.context_file, args.context_json),
    }
    return record


def _resolve_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        raise SystemExit(
            f"Environment variable {var_name} is required for Azure feedback storage."
        )
    return value


def _parse_args(argv: Optional[Iterable[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append a sample feedback record to the configured Azure append blob.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Override the session identifier (defaults to a generated UUID).",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Override the user identifier (defaults to 'manual-tester').",
    )
    parser.add_argument(
        "--feedback-source",
        default="manual-test",
        help="Short descriptor of where feedback originated.",
    )
    parser.add_argument(
        "--feedback-subject",
        default="Manual verification run",
        help="What the feedback refers to (displayed in dashboards).",
    )
    parser.add_argument(
        "--rating",
        default="Helpful",
        choices=["Helpful", "Needs improvement"],
        help="Overall rating to submit.",
    )
    parser.add_argument(
        "--highlights",
        default="Accurate and complete",
        help="Comma-separated highlight choices (converted to the Streamlit delimiter).",
    )
    parser.add_argument(
        "--improvements",
        default="",
        help="Comma-separated improvement choices (converted to the Streamlit delimiter).",
    )
    parser.add_argument(
        "--comment",
        default="Manual Azure feedback connectivity test.",
        help="Freeform comment to include.",
    )
    parser.add_argument(
        "--question",
        default="How does the Azure feedback pipeline behave?",
        help="Question field for parity with chat feedback entries.",
    )
    parser.add_argument(
        "--answer",
        default="This is a simulated answer submitted by the manual runner.",
        help="Answer field for parity with chat feedback entries.",
    )
    parser.add_argument(
        "--context-file",
        type=Path,
        default=None,
        help="Optional path to a JSON file used to populate context_json.",
    )
    parser.add_argument(
        "--context-json",
        default=None,
        help="Inline JSON string for context_json (ignored when --context-file is set).",
    )
    parser.add_argument(
        "--local-log",
        type=Path,
        default=Path("manual_feedback_log.ndjson"),
        help="Local NDJSON path required by FeedbackStore (not written when Azure succeeds).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload without sending it to Azure.",
    )
    return parser.parse_args(argv)


def _infer_account_name(connection_string: str) -> Optional[str]:
    parts = connection_string.split(";")
    for part in parts:
        key, sep, value = part.partition("=")
        if sep and key.strip().lower() == "accountname":
            return value.strip()
    return None


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parse_args(argv)

    connection = _resolve_env("AZURE_FEEDBACK_CONNECTION_STRING")
    container = _resolve_env("AZURE_FEEDBACK_CONTAINER")
    blob_name = _resolve_env("AZURE_FEEDBACK_BLOB")

    record = _build_record(args)

    if args.dry_run:
        print("Dry run - payload not sent")
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    store = build_feedback_store(FEEDBACK_FIELDS, args.local_log)

    try:
        store.append(record)
    except FeedbackStorageError as exc:
        print("Failed to append feedback:", exc)
        return 1

    account_name = _infer_account_name(connection)
    if account_name:
        blob_uri = f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}"
    else:
        blob_uri = f"https://(your-account).blob.core.windows.net/{container}/{blob_name}"
    print("Successfully appended feedback to Azure append blob!")
    print(f"Container: {container}")
    print(f"Blob: {blob_name}")
    print(f"Blob URI: {blob_uri}")
    print("Payload:")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print()
    print("Use Azure Storage Explorer or the portal to confirm the record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
