from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

import streamlit as st

from backend.persistent_state import (
    clear_latest_doc_run,
    load_latest_doc_run,
    save_latest_doc_run,
)

from frontend.utils import save_uploaded_file


def initialize_session_state() -> None:
    """Populate Streamlit session defaults and restore persisted context."""

    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid4())

    st.session_state.setdefault("chat_feedback_submitted", {})
    st.session_state.setdefault("doc_feedback_submitted", False)
    st.session_state.setdefault("doc_card_feedback_submitted", {})
    st.session_state.setdefault("latest_doc_run", None)
    st.session_state.setdefault("doc_processing_state", "idle")
    st.session_state.setdefault("doc_processing_result", None)
    st.session_state.setdefault("doc_processing_error", None)
    st.session_state.setdefault("doc_file_ready", False)
    st.session_state.setdefault("doc_file_info", None)
    st.session_state.setdefault("doc_file_token", None)
    st.session_state.setdefault("doc_extracted_questions", None)
    st.session_state.setdefault("doc_questions_answered", False)
    st.session_state.setdefault("doc_answers_payload", None)
    st.session_state.setdefault("doc_job", None)
    st.session_state.setdefault("suspend_autorefresh", False)
    st.session_state.setdefault("feedback_dialog_target", None)

    try:
        persist_key = st.session_state.get("current_user_id", st.session_state.session_id)
        restored = load_latest_doc_run(persist_key)
        if restored and not st.session_state.get("latest_doc_run"):
            st.session_state.latest_doc_run = restored
    except Exception:
        # Persistence is best effort; avoid surfacing extra toasts here.
        pass


def reset_doc_downloads() -> None:
    st.session_state["doc_downloads"] = {}


def trigger_rerun() -> None:
    """Request Streamlit to rerun regardless of API availability."""

    rerun_fn = getattr(st, "experimental_rerun", None)
    if callable(rerun_fn):
        rerun_fn()
        return
    rerun_fn = getattr(st, "rerun", None)
    if callable(rerun_fn):
        rerun_fn()
        return
    raise RuntimeError("Streamlit rerun API unavailable; update Streamlit to a newer version.")


def reset_doc_workflow(*, clear_file: bool = False) -> None:
    """Clear cached document workflow artifacts while optionally keeping the file reference."""

    if clear_file:
        st.session_state["doc_file_ready"] = False
        st.session_state["doc_file_info"] = None
        st.session_state["doc_file_token"] = None
    st.session_state["doc_extracted_questions"] = None
    st.session_state["doc_questions_answered"] = False
    st.session_state["doc_answers_payload"] = None
    st.session_state["doc_processing_state"] = "idle"
    st.session_state["doc_processing_result"] = None
    st.session_state["doc_processing_error"] = None
    st.session_state["doc_feedback_submitted"] = False
    st.session_state["doc_card_feedback_submitted"] = {}
    st.session_state["doc_processing_started_at"] = None
    st.session_state["doc_processing_finished_at"] = None
    reset_doc_downloads()


def remember_uploaded_file(uploaded_file, upload_token: str) -> None:
    """Persist core metadata for the uploaded file in session state."""

    previous = st.session_state.get("doc_file_info") or {}
    previous_path = previous.get("path")
    if previous_path:
        try:
            Path(previous_path).unlink(missing_ok=True)
        except Exception:
            pass

    stored_path = save_uploaded_file(uploaded_file)
    suffix = Path(uploaded_file.name).suffix.lower()
    st.session_state["doc_file_ready"] = True
    st.session_state["doc_file_info"] = {
        "name": uploaded_file.name,
        "path": stored_path,
        "suffix": suffix,
        "size": getattr(uploaded_file, "size", None),
        "uploaded_at": datetime.utcnow().isoformat(),
        "token": upload_token,
    }
    st.session_state["doc_file_token"] = upload_token
    reset_doc_workflow(clear_file=False)


def store_doc_download(
    key: str,
    *,
    label: str,
    data: bytes,
    file_name: str,
    mime: Optional[str] = None,
    order: int = 0,
) -> None:
    bucket: Dict[str, Dict[str, Any]] = st.session_state.setdefault("doc_downloads", {})
    bucket[key] = {
        "label": label,
        "data": data,
        "file_name": file_name,
        "mime": mime,
        "order": order,
    }


def render_doc_downloads(target=None) -> None:
    downloads: Dict[str, Dict[str, Any]] = st.session_state.get("doc_downloads") or {}
    if not downloads:
        return
    target = target or st
    for key, meta in sorted(downloads.items(), key=lambda item: item[1].get("order", 0)):
        target.download_button(
            meta["label"],
            meta["data"],
            file_name=meta["file_name"],
            mime=meta.get("mime"),
            key=f"doc_download_{key}",
        )


__all__ = [
    "initialize_session_state",
    "reset_doc_downloads",
    "trigger_rerun",
    "reset_doc_workflow",
    "remember_uploaded_file",
    "store_doc_download",
    "render_doc_downloads",
    "clear_latest_doc_run",
    "save_latest_doc_run",
]
