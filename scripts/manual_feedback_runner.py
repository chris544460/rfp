#!/usr/bin/env python3
"""Manual helper to append feedback locally (Streamlit-compatible schema)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional
from uuid import uuid4

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


def _load_context(
    context_path: Optional[Path],
    inline_json: Optional[str],
) -> str:
    if context_path:
        try:
            data = json.loads(context_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"Failed to load context JSON from {context_path}: {exc}")
        return json.dumps(data, ensure_ascii=False)
    if inline_json:
        try:
            data = json.loads(inline_json)
        except Exception as exc:
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


def _parse_args(argv: Optional[Iterable[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append a sample feedback record to a local NDJSON log.",
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
        default="Manual feedback connectivity test.",
        help="Freeform comment to include.",
    )
    parser.add_argument(
        "--question",
        default="How does the feedback pipeline behave?",
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
        help="Local NDJSON path that mirrors the Streamlit feedback log.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload without saving it.",
    )
    parser.add_argument(
        "--show-traceback",
        action="store_true",
        help="Print the full traceback if writing the local file fails.",
    )
    return parser.parse_args(argv)


def _append_local_log(local_path: Path, record: Dict[str, str]) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with local_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        handle.write("\n")


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parse_args(argv)
    record = _build_record(args)

    if args.dry_run:
        print("Dry run - payload not saved")
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    try:
        _append_local_log(args.local_log, record)
    except Exception as exc:  # pragma: no cover - file system issues
        print(f"Failed to write feedback to {args.local_log}: {exc}")
        if args.show_traceback:
            import traceback  # noqa: TCH001

            traceback.print_exc()
        return 1

    print(f"Feedback saved to {args.local_log.resolve()}")
    print("Payload:")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
