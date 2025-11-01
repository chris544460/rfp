#!/usr/bin/env python3

"""Streamlit application entrypoint extracted from the original
notebook so it can be maintained as a normal module."""

from __future__ import annotations

import subprocess
import sys

import streamlit as st

from design import APP_NAME, StyleCSS, StyleColors, display_aladdin_logos_and_app_title


def configure_page() -> None:
    """Configure Streamlit and apply the shared design system."""

    st.set_page_config(
        page_title=APP_NAME,
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    StyleCSS.set_css_styling()
    StyleCSS.set_plotly_template(
        "aladdin",
        list(StyleColors.DATAVIZ_COLORS),
        set_as_default=True,
    )
    display_aladdin_logos_and_app_title()


configure_page()

@st.cache_resource
def install_packages(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

SETUP_VERSION = "2025-09-azure-feedback"

packages = [
    "certifi",
    "charset-normalizer",
    "faiss-cpu",
    "idna",
    "numpy",
    "packaging",
    "python-dotenv",
    "requests",
    "urllib3",
    "pyarrow",
    "PyPDF2",
    "python-docx",
    "spacy",
    "azure-storage-blob",
]

def ensure_packages() -> None:
    if st.session_state.get("setup_version") == SETUP_VERSION:
        return
    progress_placeholder = st.empty()
    total = len(packages)
    for i, package in enumerate(packages, start=1):
        try:
            install_packages(package)
        except subprocess.CalledProcessError:
            progress_placeholder.empty()
            st.error("Something went wrong while setting things up. Please try again or contact support.")
            return
        percent = int(i / total * 100)
        message = f"Setting up step {i} of {total}..."
        progress_placeholder.info(f"{message} ({percent}% complete)")
    progress_placeholder.success("Setup complete.")
    st.session_state["setup_version"] = SETUP_VERSION
    st.toast("You're all set! Choose 'Upload document' to load an RFP or 'Ask a question' to chat. Provide any required API keys in the sidebar.")

ensure_packages()
import os
import tempfile
import json
import re
import io
import html
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from uuid import uuid4
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any
from answer_composer import CompletionsClient, get_openai_completion
from cli_streamlit_app import _resolve_concurrency
import my_module
from my_module import _classify_intent, _detect_followup, gen_answer
from feedback_storage import build_feedback_store, FeedbackStorageError
from persistent_state import load_latest_doc_run, save_latest_doc_run, clear_latest_doc_run
from services import DocumentFiller, QuestionExtractor, Responder
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
try:
    feedback_store = build_feedback_store(FEEDBACK_FIELDS, LOCAL_FEEDBACK_FILE)
except FeedbackStorageError as exc:
    st.error(f"Feedback storage is unavailable: {exc}")
    st.stop()


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

    try:
        persist_key = st.session_state.get("current_user_id", st.session_state.session_id)
        restored = load_latest_doc_run(persist_key)
        if restored and not st.session_state.get("latest_doc_run"):
            st.session_state.latest_doc_run = restored
    except Exception:
        # Persistence is best-effort; surfacing toasts here would be noisy.
        pass

def get_current_user() -> str:
    return st.session_state.get("current_user_id", "demo_user")
def serialize_list(items: Optional[List[str]]) -> str:
    if not items:
        return ""
    return " | ".join(item.strip() for item in items if item)
def log_feedback(record: dict) -> None:
    try:
        feedback_store.append(record)
    except FeedbackStorageError as exc:
        st.error(f"Unable to save feedback: {exc}")
def format_context(context: dict) -> str:
    try:
        return json.dumps(context, ensure_ascii=False)
    except Exception:
        return ""
def render_chat_feedback_form(
    message_index: int,
    question: Optional[str],
    answer: str,
    message_payload: dict,
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
                    "user_id": get_current_user(),
                    "feedback_source": "chat",
                    "feedback_subject": f"assistant_message_{message_index}",
                    "rating": "positive" if rating_choice == "Helpful" else "needs_improvement",
                    "highlights": serialize_list(highlights),
                    "improvements": serialize_list(improvements),
                    "comment": comment.strip(),
                    "question": question or "",
                    "answer": answer,
                    "context_json": format_context(
                        {
                            "chat_history": st.session_state.get("chat_messages", []),
                            "message_index": message_index,
                            "message_payload": message_payload,
                        }
                    ),
                }
                log_feedback(record)
                submitted_map[feedback_key] = True
                st.success("Feedback saved — thank you!")


def render_card_feedback_form(
    *,
    card_index: int,
    question: str,
    answer_text: str,
    run_context: Optional[dict] = None,
) -> None:
    """Render a compact feedback form below a Q/A card.

    Uses document-level highlight/improvement tags and records a feedback
    entry with source 'document_card'.
    """
    submitted_map = st.session_state.setdefault("doc_card_feedback_submitted", {})
    run_name = (run_context or {}).get("uploaded_name") or "document"
    feedback_key = f"{run_name}_card_{int(card_index)}"
    if submitted_map.get(feedback_key):
        st.caption("Feedback recorded — thank you!")
        return
    with st.expander("How was this answer?", expanded=False):
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
                    "user_id": get_current_user(),
                    "feedback_source": "document_card",
                    "feedback_subject": f"{run_name}_card_{int(card_index) + 1}",
                    "rating": "positive" if rating_choice == "Helpful" else "needs_improvement",
                    "highlights": serialize_list(highlights),
                    "improvements": serialize_list(improvements),
                    "comment": comment.strip(),
                    "question": question or "",
                    "answer": answer_text,
                    "context_json": format_context({
                        "run": run_context or {},
                        "card_index": int(card_index),
                    }),
                }
                log_feedback(record)
                submitted_map[feedback_key] = True
                st.success("Feedback saved — thank you!")


def _shorten_question_label(text: str) -> str:
    cleaned = (text or '').strip()
    if not cleaned:
        return 'Pending question'
    cleaned = ' '.join(cleaned.split())
    return cleaned[:87] + '...' if len(cleaned) > 90 else cleaned


def _create_live_placeholder(container, idx: int, question_text: str):
    if container is None:
        return None
    # Render a simple card-like container using a single-column layout.
    # Avoid a top-level dropdown; instead show the question header inline.
    col = container.columns(1)[0]
    # Show full question (no truncation) and bold only the question text.
    cleaned_q = ' '.join((question_text or '').strip().split())
    with col:
        st.markdown(
            f"Q{idx + 1}: <strong>{html.escape(cleaned_q)}</strong>",
            unsafe_allow_html=True,
        )
        placeholder = st.empty()
        placeholder.info('Waiting for answer...')
    return placeholder


def _normalize_citation_entry(comment):
    def _clean(value):
        return str(value).strip() if value not in (None, '') else ''

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

        label = _clean(comment.get('label') or comment.get('id') or comment.get('index') or comment.get('key') or comment.get('citation'))
        source = _clean(comment.get('source') or comment.get('source_file') or comment.get('document') or comment.get('name') or comment.get('file'))
        page = _clean(comment.get('page') or comment.get('section') or comment.get('location') or comment.get('page_label'))
        snippet = _clean(comment.get('snippet') or comment.get('text') or comment.get('content') or comment.get('passage'))
        extra = _clean(comment.get('meta') or comment.get('note'))
        if not snippet and extra:
            snippet = extra
        elif snippet and extra:
            snippet = f"{snippet} ({extra})"
        return [(label, source, snippet, page)]

    if isinstance(comment, (list, tuple)):
        label = _clean(comment[0] if len(comment) > 0 else '')
        source = _clean(comment[1] if len(comment) > 1 else '')
        snippet = _clean(comment[2] if len(comment) > 2 else '')
        page = _clean(comment[4] if len(comment) > 4 else '')
        if not page:
            page = _clean(comment[3] if len(comment) > 3 else '')
        return [(label, source, snippet, page)]

    value = _clean(comment)
    if not value:
        return []
    return [('', '', value, '')]


def _render_live_answer(placeholder, answer, comments, include_citations: bool, *, card_index: Optional[int] = None, question_text: Optional[str] = None, run_context: Optional[dict] = None) -> None:
    if placeholder is None:
        return
    if isinstance(answer, dict):
        ans_text = answer.get('text', '')
    else:
        ans_text = str(answer or '')
    ans_text = ans_text.strip() or '_No answer generated._'

    placeholder.empty()
    with placeholder.container():
        st.markdown(f"**Answer:** {ans_text}")
        if not include_citations:
            return

        raw_items = comments if isinstance(comments, (list, tuple)) else [comments]
        normalized = []
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
            title = ' — '.join(title_parts).strip() or f"Citation {i}"
            with st.expander(title, expanded=False):
                body = (snippet or '').strip()
                if body:
                    st.markdown(body)
                else:
                    st.caption('No snippet provided.')

        # Per-card feedback
        try:
            render_card_feedback_form(
                card_index=int(card_index or 0),
                question=str(question_text or ''),
                answer_text=ans_text,
                run_context=run_context,
            )
        except Exception:
            pass




def _is_table_slot(slot: dict) -> bool:
    locator = slot.get("answer_locator") or {}
    return isinstance(locator, dict) and locator.get("type") == "table_cell"


def _sanitize_table_answer(answer) -> str:

    if isinstance(answer, dict):
        text = str(answer.get("text", ""))
    else:
        text = str(answer or "")
    text = re.sub(r"\[\d+\]", "", text)

    def _collapse_table_like(line: str) -> str:
        working = line.replace("	", " | ").strip()
        if not working:
            return ""
        if set(working) <= {"|", ":", "-", " ", "+", "="}:
            return ""
        if "|" in working:
            segments = [seg.strip(" -") for seg in working.strip("|").split("|")]
            segments = [seg for seg in segments if seg and set(seg) != {'-'}]
            working = " ".join(segments)
        working = working.lstrip("-•*→•").strip()
        return working
    
    parts = []
    for raw_line in text.splitlines():
        collapsed = _collapse_table_like(raw_line)
        if collapsed:
            parts.append(collapsed)
    
    prose = " ".join(parts)
    prose = re.sub(r"\s+", " ", prose).strip()
    if not prose:
        prose = "No information found."
    if not prose.endswith(('.', '!', '?')):
        prose += '.'
    return prose



def _reset_doc_downloads() -> None:
    st.session_state["doc_downloads"] = {}


def _reset_doc_workflow(*, clear_file: bool = False) -> None:
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
    _reset_doc_downloads()


def _remember_uploaded_file(uploaded_file, upload_token: str) -> None:
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
    _reset_doc_workflow(clear_file=False)


def _get_cached_input_path() -> Optional[str]:
    """Return the cached uploaded file path if it still exists on disk."""
    info = st.session_state.get("doc_file_info") or {}
    path = info.get("path")
    if not path:
        return None
    if not Path(path).exists():
        return None
    return path


def _store_doc_download(
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


def _render_doc_downloads(target=None) -> None:
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

def render_saved_qa_pairs(run_context: Optional[dict]) -> None:
    """Render saved Q/A pairs from a completed run with the same card style,
    including citation expanders when available.
    """
    if not run_context:
        return
    pairs = run_context.get("qa_pairs") or []
    if not pairs:
        return
    include_citations = bool(run_context.get("include_citations"))
    st.markdown("### Questions and answers")
    qa_box = st.container()
    for idx, pair in enumerate(pairs):
        q_text = (pair.get("question") or "").strip()
        placeholder = _create_live_placeholder(qa_box, idx, q_text)
        ans_payload = pair.get("answer")
        comments = pair.get("comments") or []
        if not comments and isinstance(ans_payload, dict):
            raw_comments = ans_payload.get('citations') or ans_payload.get('comments') or []
            comments = raw_comments
        _render_live_answer(
            placeholder,
            ans_payload,
            comments,
            include_citations and (isinstance(ans_payload, dict) or bool(comments)),
            card_index=idx,
            question_text=q_text,
            run_context=run_context,
        )

def render_document_feedback_section(run_context: Optional[dict]) -> None:
    if not run_context:
        return
    submitted = st.session_state.get("doc_feedback_submitted", False)
    expander_label = "Share feedback on this document run"
    with st.expander(expander_label, expanded=not submitted):
        if submitted:
            st.caption("Feedback recorded — thank you!")
            return
        with st.form("document_feedback_form"):
            rating_choice = st.radio(
                "Overall",
                ["Helpful", "Needs improvement"],
                horizontal=True,
            )
            highlights = st.multiselect(
                "What worked well?",
                DOC_HIGHLIGHT_OPTIONS,
            )
            improvements = st.multiselect(
                "What should we improve?",
                DOC_IMPROVEMENT_OPTIONS,
            )
            comment = st.text_area("Additional comments", placeholder="Optional details…")
            submitted_form = st.form_submit_button("Submit feedback", use_container_width=True)
            if submitted_form:
                record = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "session_id": st.session_state.get("session_id", str(uuid4())),
                    "user_id": get_current_user(),
                    "feedback_source": "document",
                    "feedback_subject": run_context.get("uploaded_name", "document_run"),
                    "rating": "positive" if rating_choice == "Helpful" else "needs_improvement",
                    "highlights": serialize_list(highlights),
                    "improvements": serialize_list(improvements),
                    "comment": comment.strip(),
                    "question": "",
                    "answer": "",
                    "context_json": format_context(run_context),
                }
                log_feedback(record)
                st.session_state.doc_feedback_submitted = True
                st.success("Feedback saved — thank you!")
MODEL_DESCRIPTIONS = {
    "gpt-4.1-nano-2025-04-14_research": "Lighter, faster model",
    "o3-2025-04-16_research": "Slower, reasoning model",
}
MODEL_SHORT_NAMES = {
    "gpt-4.1-nano-2025-04-14_research": "4.1",
    "o3-2025-04-16_research": "o3",
}
MODEL_OPTIONS = list(MODEL_DESCRIPTIONS.keys())
FOLLOWUP_DEFAULT_MODEL = "gpt-4.1-nano-2025-04-14_research"
DEFAULT_MODEL = "o3-2025-04-16_research"
DOC_DEFAULT_MODEL = "o3-2025-04-16_research"
try:
    DEFAULT_INDEX = MODEL_OPTIONS.index(DEFAULT_MODEL)
except ValueError:
    DEFAULT_INDEX = 0
    DEFAULT_MODEL = MODEL_OPTIONS[0]
def load_fund_tags() -> List[str]:
    path = Path('structured_extraction/parsed_json_outputs/embedding_data.json')
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return []
    tags = {t for item in data for t in item.get('metadata', {}).get('tags', [])}
    return sorted(tags)
class OpenAIClient:
    def __init__(self, model: str):
        self.model = model
    def get_completion(self, prompt: str, json_output: bool = False):
        return get_openai_completion(prompt, self.model, json_output=json_output)
def save_uploaded_file(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded_file.read())
    tmp.flush()
    return tmp.name
def select_top_preapproved_answers(question: str, hits: List[dict], limit: int = 5) -> List[dict]:
    """Use the Aladdin completions client to pick the most relevant pre-approved answers."""
    if len(hits) <= limit:
        return hits
    formatted = []
    for idx, hit in enumerate(hits, 1):
        snippet = (hit.get("snippet") or "").strip().replace("", " ")
        if len(snippet) > 500:
            snippet = snippet[:497] + "..."
        source = hit.get("source") or "unknown"
        score = hit.get("score")
        if isinstance(score, (int, float)):
            score_repr = f"{score:.3f}"
        else:
            score_repr = str(score) if score is not None else "unknown"
        date = hit.get("date") or "unknown"
        formatted.append(
            f"{idx}. Source: {source}\nScore: {score_repr}\nDate: {date}\nSnippet: {snippet}"
        )
    prompt = (
        "You are ranking pre-approved RFP answers for how well they address a user's question. "
        f"Return a JSON object with a 'selections' array containing up to {limit} items. "
        "Each selection must include an 'index' (1-based) pointing to the candidate and a 'reason' in one or two sentences "
        "explaining how the candidate addresses the user's question."
        f"\n\nQuestion: {question}"
        "\n\nCandidates:\n" + "\n\n".join(formatted)
    )
    model_name = os.environ.get("ALADDIN_RERANK_MODEL", "o3-2025-04-16_research")
    try:
        client = CompletionsClient(model=model_name)
        content, _ = client.get_completion(prompt, json_output=True)
        data = json.loads(content or "{}")
    except Exception as exc:
        print(f"select_top_preapproved_answers failed with {model_name}: {exc}")
        return hits[:limit]
    selected: List[dict] = []
    seen = set()
    def add_hit(position: int, reason: Optional[str] = None) -> None:
        if not isinstance(position, int):
            return
        if not (1 <= position <= len(hits)):
            return
        if position in seen:
            return
        seen.add(position)
        hit_data = dict(hits[position - 1])
        if reason:
            cleaned = " ".join(str(reason).strip().split())
            if cleaned:
                hit_data["selection_reason"] = cleaned
        hit_data.setdefault("selected_by_model", model_name)
        selected.append(hit_data)
    selections = (
        data.get("selections")
        or data.get("choices")
        or data.get("ranked")
        or data.get("results")
        or []
    )
    if isinstance(selections, dict):
        for value in selections.values():
            if isinstance(value, list):
                selections = value
                break
    if isinstance(selections, list):
        for entry in selections:
            if len(selected) == limit:
                break
            reason = None
            idx_value = None
            if isinstance(entry, dict):
                reason = entry.get("reason") or entry.get("rationale") or entry.get("why")
                idx_value = (
                    entry.get("index")
                    or entry.get("idx")
                    or entry.get("rank")
                    or entry.get("position")
                )
            else:
                idx_value = entry
            try:
                pos = int(idx_value)
            except (TypeError, ValueError):
                continue
            add_hit(pos, reason)
    if len(selected) < limit:
        indices = data.get("top_indices") or data.get("top") or data.get("indices") or []
        if isinstance(indices, (list, tuple)):
            for idx in indices:
                if len(selected) == limit:
                    break
                try:
                    pos = int(idx)
                except (TypeError, ValueError):
                    continue
                add_hit(pos, None)
    if len(selected) < limit:
        for position in range(1, len(hits) + 1):
            if len(selected) == limit:
                break
            if position in seen:
                continue
            add_hit(position, None)
    if not selected:
        return hits[:limit]
    return selected[:limit]
def main():
    st.title("RFP Responder")
    initialize_session_state()
    st.markdown(
        """
        <style>
        div.block-container{
            max-width: 900px;
            padding: 32px 48px;
        }
        div[data-testid="stChatMessage"]{
            border-radius: 0;
            border: 1px solid #d4d6db;
            padding: 16px;
            margin-bottom: 16px;
            box-shadow: 0 2px 6px rgba(17, 24, 39, 0.08);
        }
        div[data-testid="stChatMessage-user"]{
            background-color: #f2f5f8;
        }
        div[data-testid="stChatMessage-assistant"]{
            background-color: #ffffff;
        }
        div[data-testid="stChatInput"] textarea{
            border-radius: 0;
            border: 1px solid #d4d6db;
            padding: 12px;
        }
        @keyframes shimmer{
            0%{background-position:-1000px 0;}
            100%{background-position:1000px 0;}
        }
        .shimmer{
            background:linear-gradient(90deg,#d0d0d0 0%,#b0b0b0 50%,#d0d0d0 100%);
            background-size:1000px 100%;
            animation:shimmer 2s infinite linear;
            -webkit-background-clip:text;
            -webkit-text-fill-color:transparent;
        }
        .hit-card{
            border:1px solid #d4d6db;
            border-radius:0;
            padding:18px 22px;
            background-color:#f5f6f8;
            margin-top:18px;
            box-shadow: 0 2px 6px rgba(17, 24, 39, 0.08);
        }
        .hit-meta{
            font-size:0.85rem;
            color:#525760;
            margin-bottom:12px;
        }
        .hit-reason{
            font-size:0.9rem;
            color:#30343b;
            margin-bottom:12px;
        }
        .hit-reason-label{
            font-weight:600;
            color:#1f232a;
            margin-right:6px;
        }
        .hit-snippet{
            font-size:0.95rem;
            line-height:1.6;
            color:#111827;
        }
        .hit-score{
            font-size:0.8rem;
            color:#6b7280;
            margin-top:6px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    view_mode = "User"
    input_mode = st.radio("How would you like to proceed?", ["Upload document", "Ask a question"], index=1, horizontal=True)
    doc_default_model = DOC_DEFAULT_MODEL if DOC_DEFAULT_MODEL in MODEL_OPTIONS else MODEL_OPTIONS[DEFAULT_INDEX]
    llm_model = doc_default_model
    framework_env = os.getenv("ANSWER_FRAMEWORK")
    if framework_env:
        if view_mode == "Developer":
            st.info(f"Using framework from ANSWER_FRAMEWORK: {framework_env}")
        framework = framework_env
    else:
        framework = st.selectbox("Framework", ["aladdin", "openai"], index=0, help="Choose backend for language model.")
    if framework == "aladdin":
        for key, label in [
            ("aladdin_studio_api_key", "Aladdin Studio API key"),
            ("defaultWebServer", "Default Web Server"),
            ("aladdin_user", "Aladdin user"),
            ("aladdin_passwd", "Aladdin password"),
        ]:
            if os.getenv(key):
                if view_mode == "Developer":
                    st.info(f"{key} loaded from environment")
            else:
                val = st.text_input(label, type="password" if "passwd" in key or "api_key" in key else "default")
                if val:
                    os.environ[key] = val
    else:
        if os.getenv("OPENAI_API_KEY"):
            if view_mode == "Developer":
                st.info("OPENAI_API_KEY loaded from environment")
        else:
            api_key = st.text_input("OpenAI API key", type="password", help="API key for OpenAI.")
            if api_key:
                os.environ["OPENAI_API_KEY"] = api_key
    if input_mode == "Upload document":
        uploaded = st.file_uploader(
            "Upload document",
            type=["pdf", "docx", "xls", "xlsx"],
            help="Upload the RFP or question file (PDF, Word, or Excel).",
        )
        if uploaded and Path(uploaded.name).suffix.lower() not in [".pdf", ".docx", ".xls", ".xlsx"]:
            st.warning("Only PDF, Word, or Excel documents are supported.")
            uploaded = None
        if uploaded is not None:
            size_attr = getattr(uploaded, "size", None)
            upload_token = f"{uploaded.name}:{size_attr}:{getattr(uploaded, 'type', '')}"
            if st.session_state.get("doc_file_token") != upload_token:
                _remember_uploaded_file(uploaded, upload_token)
            try:
                uploaded.seek(0)
            except Exception:
                pass
    else:
        uploaded = None
    file_info = st.session_state.get("doc_file_info") or {}
    document_ready = bool(st.session_state.get("doc_file_ready") and file_info)
    if input_mode == "Upload document" and document_ready:
        size_bytes = file_info.get("size")
        if isinstance(size_bytes, (int, float)) and size_bytes > 0:
            if size_bytes < 1024:
                size_display = f"{int(size_bytes)} bytes"
            elif size_bytes < 1024 * 1024:
                size_display = f"{size_bytes / 1024:.1f} KB"
            else:
                size_display = f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            size_display = "size unavailable"
        st.caption(f"Current document: **{file_info.get('name', 'unknown')}** ({size_display})")
        if st.button("Clear current document", key="clear_current_document", help="Forget the uploaded document and reset progress."):
            path_to_remove = file_info.get("path")
            if path_to_remove:
                try:
                    Path(path_to_remove).unlink(missing_ok=True)
                except Exception:
                    pass
            _reset_doc_workflow(clear_file=True)
            st.success("Document cleared. Upload a new file to start again.")
            try:
                st.rerun()
            except Exception:
                st.stop()
    if view_mode == "Developer":
        st.info("Search mode fixed to 'both'")
        search_mode = "both"
        fund = st.selectbox(
            "Fund", [""] + load_fund_tags(), index=0,
            help="Filter answers for a specific fund or strategy.",
        )
        llm_model = st.selectbox(
            "LLM model",
            MODEL_OPTIONS,
            index=DEFAULT_INDEX,
            format_func=lambda m: f"{m} - {MODEL_DESCRIPTIONS[m]}",
            help="Model name for generating answers.",
        )
        k_max_hits = st.number_input("Hits per question", value=10, help="Maximum documents retrieved per question.")
        min_confidence = st.number_input("Min confidence", value=0.0, help="Minimum score for retrieved documents.")
        docx_as_text = st.checkbox("Treat DOCX as text", value=False)
        docx_write_mode = st.selectbox("DOCX write mode", ["fill", "replace", "append"], index=0)
        extra_uploads = st.file_uploader(
            "Additional documents",
            type=["pdf", "docx", "xls", "xlsx"],
            accept_multiple_files=True,
            help="Additional PDF or Word documents to include in search.",
        )
        if extra_uploads:
            valid_files = []
            invalid_files = []
            for f in extra_uploads:
                if Path(f.name).suffix.lower() in [".pdf", ".docx", ".xls", ".xlsx"]:
                    valid_files.append(f)
                else:
                    invalid_files.append(f.name)
            if invalid_files:
                st.warning("Unsupported file types were ignored: " + ", ".join(invalid_files))
            extra_uploads = valid_files
    else:
        search_mode = "both"
        fund = st.selectbox(
            "Fund", [""] + load_fund_tags(), index=0,
            help="Select fund or strategy context for better answers.",
        )
        k_max_hits = 20
        min_confidence = 0.0
        docx_as_text = False
        docx_write_mode = "fill"
        extra_uploads = st.file_uploader(
            "Additional documents",
            type=["pdf", "docx", "xls", "xlsx"],
            accept_multiple_files=True,
            help="Additional PDF or Word documents to include in search.",
        )
        if extra_uploads:
            valid_files = []
            invalid_files = []
            for f in extra_uploads:
                if Path(f.name).suffix.lower() in [".pdf", ".docx", ".xls", ".xlsx"]:
                    valid_files.append(f)
                else:
                    invalid_files.append(f.name)
            if invalid_files:
                st.warning("Unsupported file types were ignored: " + ", ".join(invalid_files))
            extra_uploads = valid_files
    with st.expander("More options"):
        if view_mode == "User":
            llm_model = st.selectbox(
                "Model",
                MODEL_OPTIONS,
                index=MODEL_OPTIONS.index(llm_model),
                format_func=lambda m: f"{MODEL_SHORT_NAMES[m]} - {MODEL_DESCRIPTIONS[m]}",
                help="Choose which model generates answers.",
            )
        length_opt = st.selectbox("Answer length", ["auto", "short", "medium", "long"], index=3)
        approx_words = st.text_input("Approx words", value="", help="Approximate words per answer (optional).")
        include_env = os.getenv("RFP_INCLUDE_COMMENTS")
        if include_env is not None:
            include_citations = include_env != "0"
            st.info(f"Using include citations from RFP_INCLUDE_COMMENTS: {include_citations}")
        else:
            include_citations = st.checkbox("Include citations", value=True)
        show_live = st.checkbox("Show questions and answers during processing", value=True)
    if input_mode == "Ask a question":
        extra_docs = [save_uploaded_file(f) for f in extra_uploads] if extra_uploads else None
        llm = CompletionsClient(model=llm_model) if framework == "aladdin" else OpenAIClient(model=llm_model)
        responder = Responder(
            llm_client=llm,
            search_mode=search_mode,
            fund=fund,
            k=int(k_max_hits),
            length=length_opt,
            approx_words=int(approx_words) if approx_words else None,
            min_confidence=float(min_confidence),
            include_citations=include_citations,
            extra_docs=extra_docs or [],
        )
        my_module._llm_client = llm
        response_mode = st.radio(
            "Response style",
            ["Generate answer", "Closest pre-approved answers"],
            index=0,
            horizontal=True,
            help="Switch between generating an answer or browsing the closest approved responses.",
        )
        if "chat_messages" not in st.session_state:
            st.session_state.chat_messages = []
        if "question_history" not in st.session_state:
            st.session_state.question_history = []
        sidebar = st.sidebar.container()
        # Sidebar tools
        with sidebar:
            st.markdown("### Tools")
            if st.button("Clear chat history", key="clear_chat_history", help="Remove all chat messages and context."):
                st.session_state.chat_messages = []
                st.session_state.question_history = []
                try:
                    my_module.QUESTION_HISTORY.clear()
                except Exception:
                    pass
                st.success("Chat history cleared.")
                try:
                    st.rerun()
                except Exception:
                    pass
        sidebar.markdown("### References")
        answer_idx = 0
        last_user_message = None
        for idx, msg in enumerate(st.session_state.chat_messages):
            with st.chat_message(msg["role"]):
                if msg["role"] == "user":
                    st.markdown(msg.get("content", ""))
                    last_user_message = msg.get("content", "")
                    continue
                if "hits" in msg:
                    st.markdown(msg.get("content", ""))
                    hits = msg.get("hits") or []
                    answer_summary = msg.get("content", "")
                    if hits:
                        summary_lines = []
                        for i, hit in enumerate(hits, 1):
                            snippet = html.escape(hit.get("snippet", ""))
                            source_name = html.escape(hit.get("source", "Unknown"))
                            meta_parts = [f"<strong>{i}. {source_name}</strong>"]
                            score_val = hit.get("score")
                            if isinstance(score_val, (int, float)):
                                meta_parts.append(f"Score {score_val:.3f}")
                            elif score_val:
                                meta_parts.append(f"Score {html.escape(str(score_val))}")
                            date_val = hit.get("date")
                            if date_val:
                                meta_parts.append(html.escape(str(date_val)))
                            meta_line = " · ".join(meta_parts)
                            reason_text = hit.get("selection_reason")
                            reason_block = ""
                            if reason_text:
                                cleaned_reason = " ".join(str(reason_text).strip().split())
                                if cleaned_reason:
                                    reason_block = (
                                        f'<div class="hit-reason"><span class="hit-reason-label">Model rationale:</span>'
                                        f"{html.escape(cleaned_reason)}</div>"
                                    )
                            st.markdown(
                                f"""
                                <div class="hit-card">
                                    <div class="hit-meta">{meta_line}</div>
                                    {reason_block}
                                    <div class="hit-snippet">{snippet}</div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                            snippet_plain = (hit.get("snippet") or "").strip()
                            if snippet_plain:
                                summary_lines.append(f"{i}. {snippet_plain}")
                    else:
                        st.info(msg.get("empty_message", "No relevant answers found in the approved library."))
                    if hits:
                        joined = "\n".join(summary_lines)
                        if joined:
                            answer_summary = f"{answer_summary}\n{joined}".strip()
                    render_chat_feedback_form(
                        message_index=idx,
                        question=last_user_message,
                        answer=answer_summary,
                        message_payload=msg,
                    )
                    continue
                st.markdown(msg.get("content", ""))
                if msg.get("model"):
                    name = MODEL_SHORT_NAMES.get(msg["model"], msg["model"]) if view_mode == "User" else msg["model"]
                    st.caption(f"Model: {name}")
                if view_mode == "Developer" and msg.get("debug"):
                    st.expander("Debug info").markdown(f"```\n{msg['debug']}\n```")
                if msg["role"] == "assistant" and "hits" not in msg:
                    answer_idx += 1
                    sidebar.markdown(f"**Answer {answer_idx}**")
                    citations_map = msg.get("citations") or {}
                    if citations_map:
                        for lbl, cite in citations_map.items():
                            source_name = (cite.get('source_file') or 'Unknown').strip() or 'Unknown'
                            exp_label = f"[{lbl}] {source_name}"
                            with sidebar.expander(exp_label):
                                section_value = (cite.get('section') or '').strip()
                                if section_value:
                                    st.markdown(f"**Section:** {section_value}**")
                                snippet_text = (cite.get('text') or '').strip()
                                if snippet_text:
                                    st.markdown(snippet_text)
                                else:
                                    st.caption("Snippet not available.")
                    else:
                        sidebar.caption("No source details returned.")
                    render_chat_feedback_form(
                        message_index=idx,
                        question=last_user_message,
                        answer=msg.get("content", ""),
                        message_payload=msg,
                    )
        if prompt := st.chat_input("Ask a question"):
            st.chat_message("user").markdown(prompt)
            st.session_state.chat_messages.append({"role": "user", "content": prompt})
            history = list(st.session_state.get("question_history", []))
            if response_mode == "Closest pre-approved answers":
                with st.chat_message("assistant"):
                    container = st.container()
                    status_placeholder = container.empty()
                    def _format_preapproved_status(raw: str) -> str:
                        text = (raw or "").strip()
                        lower = text.lower()
                        if "search" in lower:
                            return "Stage 1/3 - Searching approved library..."
                        if "candidate" in lower or "filter" in lower:
                            return "Stage 2/3 - Filtering matches..."
                        if any(token in lower for token in ("complete", "finished", "done", "ready")):
                            return "Stage 3/3 - Finalizing results..."
                        return text or "Working..."

                    def _set_preapproved_status(message: str, final: bool = False) -> None:
                        display = message or "Working..."
                        if final:
                            status_placeholder.success(display)
                        else:
                            status_placeholder.markdown(
                                f'<span class="shimmer">{display}</span>',
                                unsafe_allow_html=True,
                            )

                    def update_status(msg: str) -> None:
                        formatted = _format_preapproved_status(msg)
                        lower = (msg or "").lower()
                        final = any(token in lower for token in ("complete", "finished", "done", "ready"))
                        _set_preapproved_status(formatted, final=final)

                    _set_preapproved_status("Stage 1/3 - Searching approved library...")
                    rows = responder.get_context(prompt, progress=update_status)
                    _set_preapproved_status("Stage 3/3 - Results ready.", final=True)
                    hits_payload = []
                    ranking_placeholder = container.empty()
                    if rows:
                        ranking_placeholder.markdown(
                            '<span class="shimmer">Composing ranked answers...</span>',
                            unsafe_allow_html=True,
                        )
                        for i, (lbl, src_name, snippet, score, date_str) in enumerate(rows, 1):
                            hits_payload.append(
                                {
                                    "label": lbl,
                                    "source": src_name,
                                    "snippet": snippet,
                                    "score": score,
                                    "date": date_str,
                                    "original_index": i,
                                }
                            )
                        hits_to_show = select_top_preapproved_answers(prompt, hits_payload)
                        ranking_placeholder.empty()
                        container.markdown("**Closest pre-approved answers**")
                        model_used = os.environ.get("ALADDIN_RERANK_MODEL", "o3-2025-04-16_research")
                        summary_lines = []
                        for display_idx, hit in enumerate(hits_to_show, 1):
                            snippet_html = html.escape(hit.get("snippet", ""))
                            snippet_plain = (hit.get("snippet") or "").strip()
                            if snippet_plain:
                                summary_lines.append(f"{display_idx}. {snippet_plain}")
                            source_html = html.escape(hit.get("source") or "Unknown")
                            meta_parts = [f"<strong>{display_idx}. {source_html}</strong>"]
                            score = hit.get("score")
                            if isinstance(score, (int, float)):
                                meta_parts.append(f"Score {score:.3f}")
                            elif score:
                                meta_parts.append(f"Score {html.escape(str(score))}")
                            date_str = hit.get("date")
                            if date_str:
                                meta_parts.append(html.escape(str(date_str)))
                            original_idx = hit.get("original_index")
                            if original_idx and original_idx != display_idx:
                                meta_parts.append(f"Orig #{original_idx}")
                            meta_line = " · ".join(meta_parts)
                            hit["rank"] = display_idx
                            hit.setdefault("selected_by_model", model_used)
                            reason_text = hit.get("selection_reason")
                            reason_block = ""
                            if reason_text:
                                cleaned_reason = " ".join(str(reason_text).strip().split())
                                if cleaned_reason:
                                    reason_block = (
                                        f'<div class="hit-reason"><span class="hit-reason-label">Model rationale:</span>'
                                        f"{html.escape(cleaned_reason)}</div>"
                                    )
                            container.markdown(
                                f"""
                                <div class="hit-card">
                                    <div class="hit-meta">{meta_line}</div>
                                    {reason_block}
                                    <div class="hit-snippet">{snippet_html}</div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                        msg = {"role": "assistant", "content": "**Closest pre-approved answers**", "hits": hits_to_show}
                        answer_summary = msg.get("content", "")
                        if summary_lines:
                            answer_summary = f"{answer_summary}\n" + "\n".join(summary_lines)
                        answer_summary = answer_summary.strip()
                        new_index = len(st.session_state.chat_messages)
                        render_chat_feedback_form(
                            message_index=new_index,
                            question=prompt,
                            answer=answer_summary,
                            message_payload=msg,
                        )
                    else:
                        empty_message = "No relevant answers found in the approved library."
                        container.info(empty_message)
                        msg = {
                            "role": "assistant",
                            "content": "Closest pre-approved answers",
                            "hits": [],
                            "empty_message": empty_message,
                        }
                        new_index = len(st.session_state.chat_messages)
                        render_chat_feedback_form(
                            message_index=new_index,
                            question=prompt,
                            answer=empty_message,
                            message_payload=msg,
                        )
            else:
                with st.chat_message("assistant"):
                    status_placeholder = st.empty()
                    message_placeholder = st.empty()

                    def _format_answer_status(raw: str) -> str:
                        text = (raw or "").strip()
                        lower = text.lower()
                        if "context" in lower or "history" in lower:
                            return "Stage 1/4 - Analyzing conversation context..."
                        if "search" in lower:
                            return "Stage 2/4 - Searching knowledge base..."
                        if "candidate" in lower or "filter" in lower:
                            return "Stage 3/4 - Filtering snippets..."
                        if "generating" in lower:
                            return "Stage 4/4 - Generating answer..."
                        if any(token in lower for token in ("complete", "finished", "done", "ready")):
                            return "Stage 4/4 - Answer ready."
                        return text or "Working..."

                    def _set_answer_status(message: str, final: bool = False) -> None:
                        display = message or "Working..."
                        if final:
                            status_placeholder.success(display)
                        else:
                            status_placeholder.markdown(
                                f'<span class="shimmer">{display}</span>',
                                unsafe_allow_html=True,
                            )

                    def update_status(msg: str) -> None:
                        formatted = _format_answer_status(msg)
                        lower = (msg or "").lower()
                        final = any(token in lower for token in ("complete", "finished", "done", "ready"))
                        _set_answer_status(formatted, final=final)

                    _set_answer_status("Stage 1/4 - Analyzing conversation context...")
                    intent = _classify_intent(prompt, history)
                    follow = _detect_followup(prompt, history) if intent == "follow_up" else []
                    buf = io.StringIO() if view_mode == "Developer" else None
                    response_model = llm_model
                    restore_client = None
                    call_fn = gen_answer if intent == "follow_up" else responder.answer
                    try:
                        if intent == "follow_up" and view_mode != "Developer":
                            response_model = FOLLOWUP_DEFAULT_MODEL
                            followup_llm = (
                                llm
                                if response_model == llm_model
                                else (
                                    CompletionsClient(model=response_model)
                                    if framework == "aladdin"
                                    else OpenAIClient(model=response_model)
                                )
                            )
                            restore_client = my_module._llm_client
                            my_module._llm_client = followup_llm
                        if buf:
                            with contextlib.redirect_stdout(buf):
                                ans = call_fn(prompt, progress=update_status)
                        else:
                            ans = call_fn(prompt, progress=update_status)
                    finally:
                        if restore_client is not None:
                            my_module._llm_client = restore_client
                    _set_answer_status("Stage 4/4 - Answer ready.", final=True)
                    debug_text = (
                        f"Intent: {intent}",
                        f"Follow-up indices: {follow}",
                        f"{buf.getvalue()}"
                        if buf
                        else ""
                    )
                    text = ans.get("text", "") if isinstance(ans, dict) else ans
                    citations = ans.get("citations", {}) if isinstance(ans, dict) else {}
                    message_placeholder.markdown(text)
                    label = MODEL_SHORT_NAMES.get(response_model, response_model) if view_mode == "User" else response_model
                    st.caption(f"Model: {label}")
                    if view_mode == "Developer":
                        st.expander("Debug info").markdown(f"```\n{debug_text}\n```")
                    if intent != "follow_up":
                        my_module.QUESTION_HISTORY.append(prompt)
                        my_module.QA_HISTORY.append({"question": prompt, "answer": text, "citations": []})
                    payload = {"role": "assistant", "content": text, "citations": citations, "model": response_model}
                    side_idx = sum(
                        1 for m in st.session_state.get("chat_messages", []) if m.get("role") == "assistant"
                    ) + 1
                    sidebar.markdown(f"**Answer {side_idx}**")
                    if citations:
                        for lbl, cite in citations.items():
                            source_name = (cite.get('source_file') or 'Unknown').strip() or 'Unknown'
                            with sidebar.expander(f"[{lbl}] {source_name}"):
                                section_value = (cite.get('section') or '').strip()
                                if section_value:
                                    st.markdown(f"**Section:** {section_value}**")
                                snippet_text = (cite.get('text') or '').strip()
                                st.markdown(snippet_text if snippet_text else "_Snippet not available._")
                    else:
                        sidebar.caption("No source details returned.")
                    new_index = len(st.session_state.chat_messages)
                    render_chat_feedback_form(
                        message_index=new_index,
                        question=prompt,
                        answer=text,
                        message_payload=payload,
                    )
                msg = payload
                if view_mode == "Developer":
                    msg["debug"] = debug_text
            st.session_state.chat_messages.append(msg)
            history.append(prompt)
            st.session_state.question_history = history
    else:
        run_clicked = st.button("Run")
        processing_state = st.session_state.get("doc_processing_state", "idle")
        processing_result = st.session_state.get("doc_processing_result")
        processing_error = st.session_state.get("doc_processing_error")
        st.session_state.setdefault("doc_downloads", {})
        downloads_container = st.container()
        file_info = st.session_state.get("doc_file_info") or {}
        document_ready = bool(st.session_state.get("doc_file_ready") and file_info)
        cached_path = _get_cached_input_path()

        if processing_state == "started" and not run_clicked:
            st.info("Document processing is in progress. Please wait while the run completes.")
        if processing_state == "error" and processing_error and not run_clicked:
            st.error(f"Document processing failed: {processing_error}")
            st.session_state.doc_processing_state = "idle"
            st.session_state.doc_processing_error = None
            st.session_state.doc_processing_result = None
        if processing_state == "finished" and processing_result is not None and not run_clicked:
            st.success("Document processing completed. You can download the results below or start another run.")
            render_saved_qa_pairs(processing_result)

        if run_clicked:
            if processing_state == "started":
                st.warning("A document run is already in progress. Please wait for it to finish.")
                st.stop()
            if not document_ready:
                st.warning("Please upload a document before running.")
                st.stop()
            if not fund:
                st.warning("Please select a fund or strategy before running.")
                st.stop()
            if cached_path is None:
                st.warning("The uploaded document is no longer available. Please upload it again.")
                _reset_doc_workflow(clear_file=True)
                st.stop()

            answers_ready = bool(st.session_state.get("doc_questions_answered") and processing_result)
            if answers_ready:
                st.session_state.doc_processing_state = "finished"
                st.info("Using saved answers from the previous run.")
                render_saved_qa_pairs(processing_result)
                render_document_feedback_section(processing_result)
                with st.container():
                    col1, col2 = st.columns([1, 6])
                    with col1:
                        if st.button("Clear saved run", key="clear_saved_run_cached", help="Remove the last run from memory and disk."):
                            st.session_state.latest_doc_run = None
                            st.session_state.doc_feedback_submitted = False
                            _reset_doc_workflow(clear_file=False)
                            _reset_doc_downloads()
                            try:
                                _persist_key = st.session_state.get("current_user_id", st.session_state.get("session_id", ""))
                                clear_latest_doc_run(_persist_key)
                            except Exception:
                                pass
                            st.success("Saved run cleared.")
                            try:
                                st.rerun()
                            except Exception:
                                pass
                _render_doc_downloads(downloads_container)
            else:
                st.session_state.doc_processing_state = "started"
                st.session_state.doc_processing_result = None
                st.session_state.doc_processing_error = None
                st.session_state.doc_processing_started_at = datetime.utcnow().isoformat()
                st.session_state.doc_feedback_submitted = False
                st.session_state.latest_doc_run = None
                _reset_doc_downloads()
                run_context: Optional[Dict[str, Any]] = None
                phase_placeholder = st.empty()
                sub_placeholder = st.empty()
                dev_placeholder = st.empty()
                dev_logs: List[str] = []
                state = {"step": 0, "phase": None}
                suffix = (file_info.get("suffix") or Path(file_info.get("name", "")).suffix or "").lower()
                base_steps = 1
                if suffix in (".xlsx", ".xls"):
                    branch_steps = 4
                elif suffix == ".docx" and not docx_as_text:
                    branch_steps = 3
                else:
                    branch_steps = 3
                total_steps = base_steps + branch_steps + 1
                step_bar = st.progress(0)

                def log_step(dev_msg: str, user_msg: Optional[str] = None) -> None:
                    if user_msg and user_msg != state["phase"]:
                        state["phase"] = user_msg
                        phase_placeholder.markdown(f"**{state['phase']}**")
                        sub_placeholder.empty()
                    sub_placeholder.markdown(dev_msg)
                    if view_mode == "Developer":
                        dev_logs.append(f"{state['phase']}: {dev_msg}")
                        dev_placeholder.markdown("\n".join(f"{i + 1}. {m}" for i, m in enumerate(dev_logs)))
                    state["step"] += 1
                    step_bar.progress(state["step"] / total_steps, text=state["phase"])

                try:
                    log_step("Loading cached document", "Preparing document...")
                    input_path = cached_path
                    extra_docs = [save_uploaded_file(f) for f in extra_uploads] if extra_uploads else None
                    llm = CompletionsClient(model=llm_model) if framework == "aladdin" else OpenAIClient(model=llm_model)
                    responder = Responder(
                        llm_client=llm,
                        search_mode=search_mode,
                        fund=fund,
                        k=int(k_max_hits),
                        length=length_opt,
                        approx_words=int(approx_words) if approx_words else None,
                        min_confidence=float(min_confidence),
                        include_citations=include_citations,
                        extra_docs=extra_docs or [],
                    )
                    extractor = QuestionExtractor(llm)
                    filler = DocumentFiller()
                    questions_state = st.session_state.get("doc_extracted_questions")
                    answers_payload = st.session_state.get("doc_answers_payload")

                    if suffix in (".xlsx", ".xls"):
                        if not questions_state or questions_state.get("mode") != "excel":
                            log_step("Analyzing workbook", "Reading workbook...")
                            questions = extractor.extract(input_path)
                            schema = extractor.last_details.get("schema") or []
                            payload = [dict(entry) for entry in questions]
                            st.session_state["doc_extracted_questions"] = {
                                "mode": "excel",
                                "questions": payload,
                                "schema": schema,
                            }
                            st.session_state["doc_answers_payload"] = [None] * len(payload)
                        else:
                            log_step("Reusing extracted questions", "Using cached workbook questions.")
                        questions_state = st.session_state.get("doc_extracted_questions") or {}
                        questions_list = questions_state.get("questions") or []
                        schema = questions_state.get("schema") or []
                        answers_payload = st.session_state.get("doc_answers_payload")
                        if answers_payload is None or len(answers_payload) != len(questions_list):
                            answers_payload = [None] * len(questions_list)
                            st.session_state["doc_answers_payload"] = answers_payload
                        total_qs = len(questions_list)
                        qa_results: List[Dict[str, Any]] = []
                        bundle: Dict[str, Any] = {"downloads": [], "qa_pairs": []}
                        if total_qs == 0:
                            st.info("No questions detected in this workbook.")
                        else:
                            log_step("Generating answers", "Creating responses...")
                            progress_container = st.container()
                            progress_bar = progress_container.progress(0.0)
                            qa_box = st.container() if show_live else None
                            placeholders = [None] * total_qs
                            if show_live and qa_box is not None:
                                for idx, entry in enumerate(questions_list):
                                    placeholders[idx] = _create_live_placeholder(
                                        qa_box,
                                        idx,
                                        entry.get("question", ""),
                                    )
                            answered_count = sum(
                                1
                                for item in answers_payload or []
                                if item and item.get("status") == "answered"
                            )
                            if total_qs:
                                progress_bar.progress(answered_count / total_qs, text=f"{answered_count}/{total_qs}")

                            pending_indices: List[int] = []
                            for idx, entry in enumerate(questions_list):
                                existing = answers_payload[idx] if answers_payload and idx < len(answers_payload) else None
                                q_text = entry.get("question", "")
                                if existing and existing.get("status") == "answered":
                                    if show_live and placeholders[idx] is not None:
                                        payload = existing.get("result_payload") or existing
                                        comments_payload = existing.get("raw_comments") or []
                                        _render_live_answer(
                                            placeholders[idx],
                                            payload,
                                            comments_payload,
                                            include_citations,
                                            card_index=idx,
                                            question_text=q_text,
                                        )
                                    continue
                                pending_indices.append(idx)

                            if pending_indices:
                                worker_limit = _resolve_concurrency(None) or len(pending_indices)
                                worker_limit = max(1, min(worker_limit, len(pending_indices)))
                                with ThreadPoolExecutor(max_workers=worker_limit) as pool:
                                    future_map = {
                                        pool.submit(
                                            responder.answer,
                                            questions_list[idx].get("question", ""),
                                            include_citations=include_citations,
                                        ): idx
                                        for idx in pending_indices
                                    }
                                    for fut in as_completed(future_map):
                                        idx = future_map[fut]
                                        q_text = questions_list[idx].get("question", "")
                                        result = fut.result()
                                        qa_entry = dict(questions_list[idx])
                                        qa_entry["answer"] = result["text"]
                                        qa_entry["citations"] = result["citations"]
                                        qa_entry["raw_comments"] = result.get("raw_comments", [])
                                        qa_entry["status"] = "answered"
                                        qa_entry["result_payload"] = result
                                        answers_payload[idx] = qa_entry
                                        st.session_state["doc_answers_payload"] = answers_payload
                                        answered_count += 1
                                        progress_bar.progress(
                                            answered_count / total_qs, text=f"{answered_count}/{total_qs}"
                                        )
                                        if show_live and placeholders[idx] is not None:
                                            _render_live_answer(
                                                placeholders[idx],
                                                result,
                                                qa_entry.get("raw_comments") or [],
                                                include_citations,
                                                card_index=idx,
                                                question_text=q_text,
                                            )
                            qa_results = []
                            for item in answers_payload or []:
                                if not item or item.get("status") != "answered":
                                    continue
                                cleaned = dict(item)
                                cleaned.pop("status", None)
                                cleaned.pop("result_payload", None)
                                qa_results.append(cleaned)
                            if qa_results:
                                bundle = filler.build_excel_bundle(
                                    source_path=input_path,
                                    schema=schema,
                                    qa_results=qa_results,
                                    include_citations=include_citations,
                                )
                                for download in bundle["downloads"]:
                                    _store_doc_download(
                                        download["key"],
                                        label=download["label"],
                                        data=download["data"],
                                        file_name=download["file_name"],
                                        mime=download.get("mime"),
                                        order=download.get("order", 0),
                                    )
                                _render_doc_downloads(downloads_container)
                        completed = total_qs == 0 or len(qa_results) == total_qs
                        st.session_state["doc_questions_answered"] = completed
                        st.session_state["doc_answers_payload"] = answers_payload
                        run_context = {
                            "mode": "excel",
                            "uploaded_name": file_info.get("name"),
                            "fund": fund,
                            "search_mode": search_mode,
                            "include_citations": include_citations,
                            "length": length_opt,
                            "approx_words": approx_words,
                            "extra_documents": [f.name for f in extra_uploads] if extra_uploads else [],
                            "qa_pairs": bundle.get("qa_pairs", []),
                            "schema": questions_state.get("schema") or [],
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    elif suffix == ".docx" and not docx_as_text:
                        if not questions_state or questions_state.get("mode") != "docx_slots":
                            log_step("Extracting slots from DOCX", "Analyzing document...")
                            extractor.extract(input_path)
                            details = extractor.last_details
                            slots_payload = details.get("slots_payload") or {}
                            skipped = details.get("skipped_slots") or []
                            heuristic = details.get("heuristic_skips") or []
                            slot_list = slots_payload.get("slots") or []
                            st.session_state["doc_extracted_questions"] = {
                                "mode": "docx_slots",
                                "slots_payload": slots_payload,
                                "skipped_slots": skipped,
                                "heuristic_skips": heuristic,
                                "questions": slot_list,
                            }
                            st.session_state["doc_answers_payload"] = [None] * len(slot_list)
                        else:
                            log_step("Reusing extracted slots", "Using cached DOCX slot data.")
                        questions_state = st.session_state.get("doc_extracted_questions") or {}
                        slots_payload = questions_state.get("slots_payload") or {}
                        slot_list = slots_payload.get("slots") or []
                        skipped = questions_state.get("skipped_slots") or []
                        heuristic = questions_state.get("heuristic_skips") or []
                        if skipped or heuristic:
                            st.warning(f"Skipped {len(skipped) + len(heuristic)} question(s) that cannot be answered automatically.")
                            with st.expander("View skipped questions", expanded=False):
                                for entry in skipped:
                                    reason = entry.get("reason") or "unspecified"
                                    label = entry.get("question_text") or "[blank question text]"
                                    st.markdown(f"- **{label.strip()}** — {reason}")
                                for entry in heuristic:
                                    reason = entry.get("reason", "unspecified")
                                    label = entry.get("question_text") or "[blank question text]"
                                    st.markdown(f"- **{label.strip()}** — {reason}")
                        answers_payload = st.session_state.get("doc_answers_payload")
                        if answers_payload is None or len(answers_payload) != len(slot_list):
                            answers_payload = [None] * len(slot_list)
                            st.session_state["doc_answers_payload"] = answers_payload
                        total_qs = len(slot_list)
                        qa_results = []
                        bundle = {"downloads": [], "qa_pairs": [], "skipped_slots": skipped, "heuristic_skips": heuristic}
                        if total_qs == 0:
                            st.info("No questions detected in this document.")
                        else:
                            log_step("Generating answers", "Creating responses...")
                            progress_container = st.container()
                            progress_bar = progress_container.progress(0.0)
                            qa_box = st.container() if show_live else None
                            placeholders = [None] * total_qs
                            if show_live and qa_box is not None:
                                for idx, slot in enumerate(slot_list):
                                    placeholders[idx] = _create_live_placeholder(
                                        qa_box,
                                        idx,
                                        (slot.get("question_text") or "").strip(),
                                    )
                            answered_count = sum(
                                1
                                for item in answers_payload or []
                                if item and item.get("status") == "answered"
                            )
                            if total_qs:
                                progress_bar.progress(answered_count / total_qs, text=f"{answered_count}/{total_qs}")

                            pending_indices: List[int] = []
                            for idx, slot in enumerate(slot_list):
                                cached = answers_payload[idx] if answers_payload and idx < len(answers_payload) else None
                                q_text = (slot.get("question_text") or "").strip()
                                if cached and cached.get("status") == "answered":
                                    if show_live and placeholders[idx] is not None:
                                        display_payload = cached.get("result_payload")
                                        comments_payload = cached.get("raw_comments") or []
                                        _render_live_answer(
                                            placeholders[idx],
                                            display_payload,
                                            comments_payload,
                                            include_citations and not _is_table_slot(slot),
                                            card_index=idx,
                                            question_text=q_text,
                                        )
                                    continue
                                pending_indices.append(idx)

                            if pending_indices:
                                worker_limit = _resolve_concurrency(None) or len(pending_indices)
                                worker_limit = max(1, min(worker_limit, len(pending_indices)))
                                with ThreadPoolExecutor(max_workers=worker_limit) as pool:
                                    future_map = {
                                        pool.submit(
                                            responder.answer,
                                            (slot_list[idx].get("question_text") or "").strip(),
                                            include_citations=include_citations,
                                        ): idx
                                        for idx in pending_indices
                                    }
                                    for fut in as_completed(future_map):
                                        idx = future_map[fut]
                                        slot = slot_list[idx]
                                        q_text = (slot.get("question_text") or "").strip()
                                        slot_id = slot.get("id") or f"slot_{idx + 1}"
                                        result = fut.result()
                                        if _is_table_slot(slot):
                                            sanitized = _sanitize_table_answer(result)
                                            qa_entry = {
                                                "question": q_text,
                                                "slot_id": slot_id,
                                                "answer": sanitized,
                                                "citations": {},
                                                "raw_comments": [],
                                                "status": "answered",
                                                "result_payload": sanitized,
                                            }
                                            display_payload = sanitized
                                            comments_payload: List[Any] = []
                                        else:
                                            qa_entry = {
                                                "question": q_text,
                                                "slot_id": slot_id,
                                                "answer": result["text"],
                                                "citations": result["citations"],
                                                "raw_comments": result.get("raw_comments", []),
                                                "status": "answered",
                                                "result_payload": result,
                                            }
                                            display_payload = result
                                            comments_payload = qa_entry["raw_comments"]
                                        answers_payload[idx] = qa_entry
                                        st.session_state["doc_answers_payload"] = answers_payload
                                        answered_count += 1
                                        progress_bar.progress(
                                            answered_count / total_qs, text=f"{answered_count}/{total_qs}"
                                        )
                                        if show_live and placeholders[idx] is not None:
                                            _render_live_answer(
                                                placeholders[idx],
                                                display_payload,
                                                comments_payload,
                                                include_citations and not _is_table_slot(slot),
                                                card_index=idx,
                                                question_text=q_text,
                                            )
                            qa_results = []
                            for item in answers_payload or []:
                                if not item or item.get("status") != "answered":
                                    continue
                                cleaned = dict(item)
                                cleaned.pop("status", None)
                                cleaned.pop("result_payload", None)
                                qa_results.append(cleaned)
                            if qa_results:
                                bundle = filler.build_docx_slot_bundle(
                                    source_path=input_path,
                                    slots_payload=slots_payload,
                                    qa_results=qa_results,
                                    include_citations=include_citations,
                                    write_mode=docx_write_mode,
                                )
                                for download in bundle["downloads"]:
                                    _store_doc_download(
                                        download["key"],
                                        label=download["label"],
                                        data=download["data"],
                                        file_name=download["file_name"],
                                        mime=download.get("mime"),
                                        order=download.get("order", 0),
                                    )
                                _render_doc_downloads(downloads_container)
                        completed = total_qs == 0 or len(qa_results) == total_qs
                        st.session_state["doc_questions_answered"] = completed
                        st.session_state["doc_answers_payload"] = answers_payload
                        run_context = {
                            "mode": "docx_slots",
                            "uploaded_name": file_info.get("name"),
                            "fund": fund,
                            "search_mode": search_mode,
                            "include_citations": include_citations,
                            "docx_write_mode": docx_write_mode,
                            "extra_documents": [f.name for f in extra_uploads] if extra_uploads else [],
                            "qa_pairs": bundle.get("qa_pairs", []),
                            "slots": questions_state.get("slots_payload") or {},
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    else:
                        if not questions_state or questions_state.get("mode") != "document_summary":
                            log_step("Extracting questions", "Reading document...")
                            treat_as_text = suffix == ".docx"
                            questions = extractor.extract(input_path, treat_docx_as_text=treat_as_text)
                            payload = [dict(entry) for entry in questions]
                            st.session_state["doc_extracted_questions"] = {
                                "mode": "document_summary",
                                "questions": payload,
                                "treat_docx_as_text": treat_as_text,
                            }
                            st.session_state["doc_answers_payload"] = [None] * len(payload)
                        else:
                            log_step("Reusing extracted questions", "Using cached document questions.")
                        questions_state = st.session_state.get("doc_extracted_questions") or {}
                        questions_list = questions_state.get("questions") or []
                        answers_payload = st.session_state.get("doc_answers_payload")
                        if answers_payload is None or len(answers_payload) != len(questions_list):
                            answers_payload = [None] * len(questions_list)
                            st.session_state["doc_answers_payload"] = answers_payload
                        total_qs = len(questions_list)
                        qa_results = []
                        bundle = {"downloads": [], "qa_pairs": []}
                        if total_qs == 0:
                            st.info("No questions could be extracted from the document.")
                        else:
                            log_step("Generating answers", "Creating responses...")
                            progress_container = st.container()
                            progress_bar = progress_container.progress(0.0)
                            qa_box = st.container() if show_live else None
                            placeholders = [None] * total_qs
                            if show_live and qa_box is not None:
                                for idx, entry in enumerate(questions_list):
                                    placeholders[idx] = _create_live_placeholder(
                                        qa_box,
                                        idx,
                                        entry.get("question", ""),
                                    )
                            answered_count = sum(
                                1
                                for item in answers_payload or []
                                if item and item.get("status") == "answered"
                            )
                            if total_qs:
                                progress_bar.progress(answered_count / total_qs, text=f"{answered_count}/{total_qs}")

                            pending_indices: List[int] = []
                            for idx, entry in enumerate(questions_list):
                                existing = answers_payload[idx] if answers_payload and idx < len(answers_payload) else None
                                q_text = entry.get("question", "")
                                if existing and existing.get("status") == "answered":
                                    if show_live and placeholders[idx] is not None:
                                        payload = existing.get("result_payload") or existing
                                        comments_payload = existing.get("raw_comments") or []
                                        _render_live_answer(
                                            placeholders[idx],
                                            payload,
                                            comments_payload,
                                            include_citations,
                                            card_index=idx,
                                            question_text=q_text,
                                        )
                                    continue
                                pending_indices.append(idx)

                            if pending_indices:
                                worker_limit = _resolve_concurrency(None) or len(pending_indices)
                                worker_limit = max(1, min(worker_limit, len(pending_indices)))
                                with ThreadPoolExecutor(max_workers=worker_limit) as pool:
                                    future_map = {
                                        pool.submit(
                                            responder.answer,
                                            questions_list[idx].get("question", ""),
                                            include_citations=include_citations,
                                        ): idx
                                        for idx in pending_indices
                                    }
                                    for fut in as_completed(future_map):
                                        idx = future_map[fut]
                                        q_text = questions_list[idx].get("question", "")
                                        result = fut.result()
                                        qa_entry = dict(questions_list[idx])
                                        qa_entry["answer"] = result["text"]
                                        qa_entry["citations"] = result["citations"]
                                        qa_entry["raw_comments"] = result.get("raw_comments", [])
                                        qa_entry["status"] = "answered"
                                        qa_entry["result_payload"] = result
                                        answers_payload[idx] = qa_entry
                                        st.session_state["doc_answers_payload"] = answers_payload
                                        answered_count += 1
                                        progress_bar.progress(
                                            answered_count / total_qs, text=f"{answered_count}/{total_qs}"
                                        )
                                        if show_live and placeholders[idx] is not None:
                                            _render_live_answer(
                                                placeholders[idx],
                                                result,
                                                qa_entry.get("raw_comments") or [],
                                                include_citations,
                                                card_index=idx,
                                                question_text=q_text,
                                            )
                            qa_results = []
                            for item in answers_payload or []:
                                if not item or item.get("status") != "answered":
                                    continue
                                cleaned = dict(item)
                                cleaned.pop("status", None)
                                cleaned.pop("result_payload", None)
                                qa_results.append(cleaned)
                            if qa_results:
                                bundle = filler.build_summary_bundle(
                                    questions=[entry.get("question", "") for entry in questions_list],
                                    qa_results=qa_results,
                                    include_citations=include_citations,
                                )
                                for download in bundle["downloads"]:
                                    _store_doc_download(
                                        download["key"],
                                        label=download["label"],
                                        data=download["data"],
                                        file_name=download["file_name"],
                                        mime=download.get("mime"),
                                        order=download.get("order", 0),
                                    )
                                _render_doc_downloads(downloads_container)
                        completed = total_qs == 0 or len(qa_results) == total_qs
                        st.session_state["doc_questions_answered"] = completed
                        st.session_state["doc_answers_payload"] = answers_payload
                        run_context = {
                            "mode": "document_summary",
                            "uploaded_name": file_info.get("name"),
                            "fund": fund,
                            "search_mode": search_mode,
                            "include_citations": include_citations,
                            "length": length_opt,
                            "approx_words": approx_words,
                            "extra_documents": [f.name for f in extra_uploads] if extra_uploads else [],
                            "qa_pairs": bundle.get("qa_pairs", []),
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    step_bar.progress(1.0, text="Done")
                    st.session_state.doc_processing_state = "finished"
                    st.session_state.doc_processing_finished_at = datetime.utcnow().isoformat()
                    if run_context is None:
                        run_context = {
                            "mode": (st.session_state.get("doc_extracted_questions") or {}).get("mode") or "document_summary",
                            "uploaded_name": file_info.get("name"),
                            "fund": fund,
                            "search_mode": search_mode,
                            "include_citations": include_citations,
                            "length": length_opt,
                            "approx_words": approx_words,
                            "extra_documents": [f.name for f in extra_uploads] if extra_uploads else [],
                            "qa_pairs": [],
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    st.session_state.doc_processing_result = run_context
                    if run_context:
                        st.session_state.latest_doc_run = run_context
                        try:
                            _persist_key = st.session_state.get("current_user_id", st.session_state.get("session_id", ""))
                            save_latest_doc_run(_persist_key, run_context)
                        except Exception:
                            pass
                        render_document_feedback_section(run_context)
                        with st.container():
                            col1, col2 = st.columns([1, 6])
                            with col1:
                                if st.button("Clear saved run", key="clear_saved_run_post", help="Remove the last run from memory and disk."):
                                    st.session_state.latest_doc_run = None
                                    st.session_state.doc_feedback_submitted = False
                                    _reset_doc_workflow(clear_file=False)
                                    _reset_doc_downloads()
                                    try:
                                        _persist_key = st.session_state.get("current_user_id", st.session_state.get("session_id", ""))
                                        clear_latest_doc_run(_persist_key)
                                    except Exception:
                                        pass
                                    st.success("Saved run cleared.")
                                    try:
                                        st.rerun()
                                    except Exception:
                                        pass
                except Exception as exc:
                    st.session_state.doc_processing_state = "error"
                    st.session_state.doc_processing_error = str(exc)
                    _reset_doc_downloads()
                    st.error("Something went wrong while processing the document. Please try again.")
                    st.exception(exc)
                    st.stop()
        elif st.session_state.get("latest_doc_run") and not run_clicked:
            latest = st.session_state.get("latest_doc_run")
            render_document_feedback_section(latest)
            render_saved_qa_pairs(latest)
            with st.container():
                col1, col2 = st.columns([1, 6])
                with col1:
                    if st.button("Clear saved run", key="clear_saved_run", help="Remove the last run from memory and disk."):
                        st.session_state.latest_doc_run = None
                        st.session_state.doc_feedback_submitted = False
                        _reset_doc_workflow(clear_file=False)
                        _reset_doc_downloads()
                        try:
                            _persist_key = st.session_state.get("current_user_id", st.session_state.get("session_id", ""))
                            clear_latest_doc_run(_persist_key)
                        except Exception:
                            pass
                        st.success("Saved run cleared.")
                        try:
                            st.rerun()
                        except Exception:
                            pass

        if st.session_state.get("doc_processing_state") == "finished" and not run_clicked:
            _render_doc_downloads(downloads_container)
    
    
if __name__ == "__main__":
    main()
