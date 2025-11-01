from __future__ import annotations

import html
import re
from typing import Any, List, Optional

import streamlit as st

from .feedback import FeedbackUI


def create_live_placeholder(container, idx: int, question_text: str):
    if container is None:
        return None
    cleaned_q = ' '.join((question_text or '').strip().split())
    col = container.columns(1)[0]
    with col:
        st.markdown(
            f"Q{idx + 1}: <strong>{html.escape(cleaned_q)}</strong>",
            unsafe_allow_html=True,
        )
        placeholder = st.empty()
        placeholder.info("Waiting for answer...")
    return placeholder


def _normalize_citation_entry(comment):
    def _clean(value):
        return str(value).strip() if value not in (None, "") else ""

    if not comment:
        return []

    if isinstance(comment, dict):
        if comment and all(str(k).isdigit() for k in comment.keys()):
            entries = []
            for key in sorted(comment.keys(), key=lambda k: int(str(k)) if str(k).isdigit() else str(k)):
                payload = comment[key]
                if isinstance(payload, dict):
                    payload = dict(payload)
                else:
                    payload = {"text": payload}
                payload.setdefault("label", key)
                entries.extend(_normalize_citation_entry(payload))
            return entries

        label = _clean(
            comment.get("label")
            or comment.get("id")
            or comment.get("index")
            or comment.get("key")
            or comment.get("citation")
        )
        source = _clean(
            comment.get("source")
            or comment.get("source_file")
            or comment.get("document")
            or comment.get("name")
            or comment.get("file")
        )
        page = _clean(comment.get("page") or comment.get("section") or comment.get("location") or comment.get("page_label"))
        snippet = _clean(comment.get("snippet") or comment.get("text") or comment.get("content") or comment.get("passage"))
        extra = _clean(comment.get("meta") or comment.get("note"))
        if not snippet and extra:
            snippet = extra
        elif snippet and extra:
            snippet = f"{snippet} ({extra})"
        return [(label, source, snippet, page)]

    if isinstance(comment, (list, tuple)):
        label = _clean(comment[0] if len(comment) > 0 else "")
        source = _clean(comment[1] if len(comment) > 1 else "")
        snippet = _clean(comment[2] if len(comment) > 2 else "")
        page = _clean(comment[4] if len(comment) > 4 else "")
        if not page:
            page = _clean(comment[3] if len(comment) > 3 else "")
        return [(label, source, snippet, page)]

    value = _clean(comment)
    if not value:
        return []
    return [("", "", value, "")]


def render_live_answer(
    placeholder,
    answer,
    comments,
    include_citations: bool,
    feedback: FeedbackUI,
    *,
    card_index: Optional[int] = None,
    question_text: Optional[str] = None,
    run_context: Optional[dict] = None,
    use_dialog: bool = False,
) -> None:
    if placeholder is None:
        return
    if isinstance(answer, dict):
        ans_text = answer.get("text", "")
    else:
        ans_text = str(answer or "")
    ans_text = ans_text.strip() or "_No answer generated._"

    placeholder.empty()
    with placeholder.container():
        st.markdown(f"**Answer:** {ans_text}")
        if include_citations:
            raw_items = comments if isinstance(comments, (list, tuple)) else [comments]
            normalized: List[Any] = []
            for item in raw_items or []:
                normalized.extend(_normalize_citation_entry(item))

            entries = []
            for label, source, snippet, page in normalized:
                if any([label, source, snippet, page]):
                    entries.append((label, source, snippet, page))

            for i, (label, source, snippet, page) in enumerate(entries, 1):
                title_parts = []
                if label:
                    title_parts.append(f"[{label}]")
                if source:
                    title_parts.append(source)
                if page:
                    title_parts.append(page)
                title = " — ".join(title_parts).strip() or f"Citation {i}"
                with st.expander(title, expanded=False):
                    body = (snippet or "").strip()
                    if body:
                        st.markdown(body)
                    else:
                        st.caption("No snippet provided.")

    if use_dialog:
        feedback.render_feedback_dialog(
            card_index=int(card_index or 0),
            question_text=str(question_text or ""),
            answer_text=ans_text,
            run_context=run_context,
        )
    else:
        feedback.render_card_feedback_form(
            card_index=int(card_index or 0),
            question=str(question_text or ""),
            answer_text=ans_text,
            run_context=run_context,
        )


__all__ = [
    "create_live_placeholder",
    "render_live_answer",
]
