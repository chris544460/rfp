from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

import streamlit as st

CHAT_HIGHLIGHT_OPTIONS = [
    "Accurate and complete",
    "Actionable guidance",
    "Clear explanation",
    "Helpful citations",
    "Fast response",
]
CHAT_IMPROVEMENT_OPTIONS = [
    "Incorrect information",
    "Missing details",
    "Not relevant",
    "Formatting issues",
    "Slow response",
]
DOC_HIGHLIGHT_OPTIONS = [
    "Captured required fields",
    "Accurate answers",
    "Helpful citations",
    "Easy to download",
]
DOC_IMPROVEMENT_OPTIONS = [
    "Incorrect answers",
    "Missing responses",
    "Formatting issues",
    "Download problems",
    "Slow processing",
]


class FeedbackUI:
    """Encapsulates chat and document feedback rendering."""

    def __init__(
        self,
        *,
        log_feedback: Callable[[Dict[str, Any]], None],
        get_current_user: Callable[[], str],
        serialize_list: Callable[[Optional[List[str]]], str],
        format_context: Callable[[Dict[str, Any]], str],
    ) -> None:
        self._log_feedback = log_feedback
        self._get_current_user = get_current_user
        self._serialize_list = serialize_list
        self._format_context = format_context

    # ── Chat feedback ────────────────────────────────────────────────────────

    def render_chat_feedback_form(
        self,
        *,
        message_index: int,
        question: Optional[str],
        answer: str,
        message_payload: Dict[str, Any],
    ) -> None:
        submitted_map = st.session_state.setdefault("chat_feedback_submitted", {})
        feedback_key = f"chat_{message_index}"
        if submitted_map.get(feedback_key):
            st.caption("Feedback recorded — thank you!")
            return
        with st.expander("How was this answer?", expanded=False):
            rating_key = f"chat_rating_{message_index}"
            rating_choice = st.radio(
                "Overall",
                ["Helpful", "Needs improvement"],
                horizontal=True,
                key=rating_key,
            )
            highlights_key = f"chat_highlights_{message_index}"
            improvements_key = f"chat_improvements_{message_index}"
            with st.form(f"chat_feedback_form_{message_index}"):
                highlights: List[str] = []
                improvements: List[str] = []
                if rating_choice == "Helpful":
                    st.session_state.pop(improvements_key, None)
                    highlights = st.multiselect(
                        "Highlights",
                        CHAT_HIGHLIGHT_OPTIONS,
                        key=highlights_key,
                    )
                else:
                    st.session_state.pop(highlights_key, None)
                    improvements = st.multiselect(
                        "Opportunities",
                        CHAT_IMPROVEMENT_OPTIONS,
                        key=improvements_key,
                    )
                comment = st.text_area(
                    "Additional comments",
                    placeholder="What should we know?",
                    key=f"chat_comment_{message_index}",
                )
                submitted = st.form_submit_button("Submit feedback", use_container_width=True)
                if submitted:
                    record = {
                        "timestamp": datetime.utcnow().isoformat(),
                        "session_id": st.session_state.get("session_id", str(uuid4())),
                        "user_id": self._get_current_user(),
                        "feedback_source": "chat",
                        "feedback_subject": f"assistant_message_{message_index}",
                        "rating": "positive" if rating_choice == "Helpful" else "needs_improvement",
                        "highlights": self._serialize_list(highlights),
                        "improvements": self._serialize_list(improvements),
                        "comment": comment.strip(),
                        "question": question or "",
                        "answer": answer,
                        "context_json": self._format_context(
                            {
                                "chat_history": st.session_state.get("chat_messages", []),
                                "message_index": message_index,
                                "message_payload": message_payload,
                            }
                        ),
                    }
                    self._log_feedback(record)
                    submitted_map[feedback_key] = True
                    st.success("Feedback saved — thank you!")

    # ── Document feedback ───────────────────────────────────────────────────

    def render_card_feedback_form(
        self,
        *,
        card_index: int,
        question: str,
        answer_text: str,
        run_context: Optional[dict] = None,
        container: Optional[Any] = None,
        title: str = "How was this answer?",
    ) -> bool:
        submitted_map = st.session_state.setdefault("doc_card_feedback_submitted", {})
        run_name = (run_context or {}).get("uploaded_name") or "document"
        feedback_key = f"{run_name}_card_{int(card_index)}"
        if submitted_map.get(feedback_key):
            target = container or st
            target.caption("Feedback recorded — thank you!")
            return False

        context_manager = container or st.expander(title, expanded=False)
        with context_manager:
            rating_key = f"card_rating_{feedback_key}"
            rating_choice = st.radio(
                "Overall",
                ["Helpful", "Needs improvement"],
                horizontal=True,
                key=rating_key,
            )
            highlights_key = f"card_highlights_{feedback_key}"
            improvements_key = f"card_improvements_{feedback_key}"
            with st.form(f"card_feedback_form_{feedback_key}"):
                highlights: List[str] = []
                improvements: List[str] = []
                if rating_choice == "Helpful":
                    st.session_state.pop(improvements_key, None)
                    highlights = st.multiselect(
                        "Highlights",
                        DOC_HIGHLIGHT_OPTIONS,
                        key=highlights_key,
                    )
                else:
                    st.session_state.pop(highlights_key, None)
                    improvements = st.multiselect(
                        "Opportunities",
                        DOC_IMPROVEMENT_OPTIONS,
                        key=improvements_key,
                    )
                comment = st.text_area(
                    "Additional comments",
                    placeholder="Optional details…",
                    key=f"card_comment_{feedback_key}",
                )
                submitted = st.form_submit_button("Submit feedback", use_container_width=True)
                if submitted:
                    record = {
                        "timestamp": datetime.utcnow().isoformat(),
                        "session_id": st.session_state.get("session_id", str(uuid4())),
                        "user_id": self._get_current_user(),
                        "feedback_source": "document_card",
                        "feedback_subject": f"{run_name}_card_{int(card_index) + 1}",
                        "rating": "positive" if rating_choice == "Helpful" else "needs_improvement",
                        "highlights": self._serialize_list(highlights),
                        "improvements": self._serialize_list(improvements),
                        "comment": comment.strip(),
                        "question": question or "",
                        "answer": answer_text,
                        "context_json": self._format_context(
                            {
                                "run": run_context or {},
                                "card_index": int(card_index),
                            }
                        ),
                    }
                    self._log_feedback(record)
                    submitted_map[feedback_key] = True
                    st.success("Feedback saved — thank you!")
                    return True
        return False

    def render_feedback_dialog(
        self,
        *,
        card_index: int,
        question_text: str,
        answer_text: str,
        run_context: Optional[dict],
    ) -> None:
        run_name = (run_context or {}).get("uploaded_name") or "document"
        feedback_key = f"{run_name}_card_{card_index}"
        submitted_map = st.session_state.setdefault("doc_card_feedback_submitted", {})
        if submitted_map.get(feedback_key):
            st.caption("Feedback recorded — thank you!")
            return

        button_key = f"feedback_btn_{feedback_key}"
        dialog_factory = getattr(st, "dialog", None)
        has_dialog = callable(dialog_factory)

        if st.button("Feedback", key=button_key):
            if has_dialog:
                st.session_state["feedback_dialog_target"] = feedback_key
                st.session_state["suspend_autorefresh"] = True
            else:
                submitted = self.render_card_feedback_form(
                    card_index=card_index,
                    question=question_text,
                    answer_text=answer_text,
                    run_context=run_context,
                    title="How was this answer?",
                )
                if submitted:
                    submitted_map[feedback_key] = True
                return

        if not has_dialog:
            self.render_card_feedback_form(
                card_index=card_index,
                question=question_text,
                answer_text=answer_text,
                run_context=run_context,
                title="How was this answer?",
            )
            return

        active_target = st.session_state.get("feedback_dialog_target")
        if active_target == feedback_key:
            dialog_key = f"feedback_dialog_{feedback_key}"
            with dialog_factory("How was this answer?", key=dialog_key):
                container = st.container()
                submitted = self.render_card_feedback_form(
                    card_index=card_index,
                    question=question_text,
                    answer_text=answer_text,
                    run_context=run_context,
                    container=container,
                    title="How was this answer?",
                )
                close_clicked = st.button("Close", key=f"{dialog_key}_close")
                if submitted or close_clicked:
                    st.session_state["feedback_dialog_target"] = None
                    st.session_state["suspend_autorefresh"] = False


__all__ = [
    "CHAT_HIGHLIGHT_OPTIONS",
    "CHAT_IMPROVEMENT_OPTIONS",
    "DOC_HIGHLIGHT_OPTIONS",
    "DOC_IMPROVEMENT_OPTIONS",
    "FeedbackUI",
]
