"""Feedback storage utilities that append to a local NDJSON file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable


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
        normalized = {key: row.get(key, "") for key in self._fieldnames}
        try:
            with self._local_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(normalized, ensure_ascii=False))
                handle.write("\n")
        except Exception as exc:  # pragma: no cover - file system issues
            raise FeedbackStorageError(
                f"Failed to append feedback to local log '{self._local_path}': {exc}"
            ) from exc


def build_feedback_store(fieldnames: Iterable[str], local_path: Path) -> FeedbackStore:
    return FeedbackStore(fieldnames=fieldnames, local_path=local_path)
