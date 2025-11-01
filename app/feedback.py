from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from rfp.feedback_storage import build_feedback_store, FeedbackStorageError

LOCAL_FEEDBACK_FILE = Path("feedback_log.ndjson")
FEEDBACK_FIELDS = [
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


class FeedbackManager:
    """Provides common feedback utilities and persistence hooks for the UI."""

    def __init__(self, store) -> None:
        self._store = store

    # ---- Feedback UI callbacks -------------------------------------------------

    def log(self, record: Dict[str, Any]) -> None:
        try:
            self._store.append(record)
        except FeedbackStorageError as exc:
            st.error(f"Unable to save feedback: {exc}")

    def get_current_user(self) -> str:
        return st.session_state.get("current_user_id", "demo_user")

    @staticmethod
    def serialize_list(items: Optional[List[str]]) -> str:
        if not items:
            return ""
        return " | ".join(item.strip() for item in items if item)

    @staticmethod
    def format_context(context: Dict[str, Any]) -> str:
        try:
            return json.dumps(context, ensure_ascii=False)
        except Exception:
            return ""


def build_feedback_manager() -> FeedbackManager:
    """Initialize the persistent feedback store and wrap it in a manager."""

    try:
        store = build_feedback_store(FEEDBACK_FIELDS, LOCAL_FEEDBACK_FILE)
    except FeedbackStorageError as exc:
        st.error(f"Feedback storage is unavailable: {exc}")
        st.stop()
    return FeedbackManager(store)


__all__ = ["FeedbackManager", "build_feedback_manager"]
