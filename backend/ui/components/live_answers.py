from __future__ import annotations

import html
from typing import Any, List, Optional, Sequence

import streamlit as st

from .feedback import FeedbackUI


def create_live_placeholder(container, idx: int, question_text: str):
    if container is None:
        return None
    # Streamlit cannot mutate text in-place, so we pre-render a placeholder card
    # for each question and update it once the async answer arrives.
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


def _clean_text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


_LABEL_KEYS = ("label", "id", "index", "key", "citation")
_SOURCE_KEYS = ("source", "source_file", "document", "name", "file")
_PAGE_KEYS = ("page", "section", "location", "page_label")
_SNIPPET_KEYS = ("snippet", "text", "content", "passage")
_EXTRA_KEYS = ("meta", "note")

CitationEntry = tuple[str, str, str, str]


def _sort_citation_key(raw_key: Any):
    key_as_str = str(raw_key)
    return (0, int(key_as_str)) if key_as_str.isdigit() else (1, key_as_str)


def _normalize_indexed_dict(comment: dict) -> List[CitationEntry]:
    entries: List[CitationEntry] = []
    for key in sorted(comment.keys(), key=_sort_citation_key):
        payload = comment[key]
        if isinstance(payload, dict):
            payload = dict(payload)
        else:
            payload = {"text": payload}
        payload.setdefault("label", key)
        entries.extend(_normalize_citation_entry(payload))
    return entries


def _first_clean_value(comment: dict, keys) -> str:
    for key in keys:
        cleaned = _clean_text(comment.get(key))
        if cleaned:
            return cleaned
    return ""


def _normalize_plain_dict(comment: dict) -> List[CitationEntry]:
    label = _first_clean_value(comment, _LABEL_KEYS)
    source = _first_clean_value(comment, _SOURCE_KEYS)
    page = _first_clean_value(comment, _PAGE_KEYS)

    snippet = _first_clean_value(comment, _SNIPPET_KEYS)
    extra = _first_clean_value(comment, _EXTRA_KEYS)
    if not snippet and extra:
        snippet = extra
    elif snippet and extra:
        snippet = f"{snippet} ({extra})"

    return [(label, source, snippet, page)]


def _normalize_sequence(comment: Sequence[Any]) -> List[CitationEntry]:
    label = _clean_text(comment[0]) if len(comment) > 0 else ""
    source = _clean_text(comment[1]) if len(comment) > 1 else ""
    snippet = _clean_text(comment[2]) if len(comment) > 2 else ""
    page = _clean_text(comment[4]) if len(comment) > 4 else ""
    if not page:
        page = _clean_text(comment[3]) if len(comment) > 3 else ""
    return [(label, source, snippet, page)]


def _normalize_scalar(comment: Any) -> List[CitationEntry]:
    value = _clean_text(comment)
    if not value:
        return []
    return [("", "", value, "")]


def _normalize_citation_entry(comment) -> List[CitationEntry]:
    # Accept a wide range of citation shapes (dict, list, scalar) so older runs
    # and new pipelines alike render consistently.
    if not comment:
        return []

    if isinstance(comment, dict):
        if comment and all(str(k).isdigit() for k in comment.keys()):
            return _normalize_indexed_dict(comment)
        return _normalize_plain_dict(comment)

    if isinstance(comment, (list, tuple)):
        return _normalize_sequence(comment)

    return _normalize_scalar(comment)


def _resolve_answer_text(answer: Any) -> str:
    if isinstance(answer, dict):
        raw_text = answer.get("text", "")
    else:
        raw_text = str(answer or "")
    return raw_text.strip() or "_No answer generated._"


def _collect_citations(comments: Any) -> List[CitationEntry]:
    raw_items = comments if isinstance(comments, (list, tuple)) else [comments]
    normalized: List[CitationEntry] = []
    for item in raw_items or []:
        normalized.extend(_normalize_citation_entry(item))
    return [entry for entry in normalized if any(entry)]


def _build_citation_title(label: str, source: str, page: str, fallback_index: int) -> str:
    parts = []
    if label:
        parts.append(f"[{label}]")
    if source:
        parts.append(source)
    if page:
        parts.append(page)
    return " — ".join(parts).strip() or f"Citation {fallback_index}"


def _render_citations_section(comments: Any) -> None:
    entries = _collect_citations(comments)
    for idx, (label, source, snippet, page) in enumerate(entries, 1):
        title = _build_citation_title(label, source, page, idx)
        with st.expander(title, expanded=False):
            body = (snippet or "").strip()
            if body:
                st.markdown(body)
            else:
                st.caption("No snippet provided.")


def _render_feedback_section(
    feedback: FeedbackUI,
    *,
    use_dialog: bool,
    card_index: Optional[int],
    question_text: Optional[str],
    answer_text: str,
    run_context: Optional[dict],
) -> None:
    index = int(card_index or 0)
    question_value = str(question_text or "")
    if use_dialog:
        feedback.render_feedback_dialog(
            card_index=index,
            question_text=question_value,
            answer_text=answer_text,
            run_context=run_context,
        )
    else:
        feedback.render_card_feedback_form(
            card_index=index,
            question=question_value,
            answer_text=answer_text,
            run_context=run_context,
        )


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

    ans_text = _resolve_answer_text(answer)

    placeholder.empty()
    with placeholder.container():
        st.markdown(f"**Answer:** {ans_text}")
        if include_citations:
            _render_citations_section(comments)

    _render_feedback_section(
        feedback,
        use_dialog=use_dialog,
        card_index=card_index,
        question_text=question_text,
        answer_text=ans_text,
        run_context=run_context,
    )


__all__ = [
    "create_live_placeholder",
    "render_live_answer",
]
