#!/usr/bin/env python3

"""Streamlit application entrypoint extracted from the original
notebook so it can be maintained as a normal module."""

from __future__ import annotations

import contextlib
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import streamlit as st

from design import APP_NAME, StyleCSS, StyleColors, display_aladdin_logos_and_app_title
from answer_composer import CompletionsClient, get_openai_completion
import my_module
from my_module import _classify_intent, _detect_followup, gen_answer
from feedback_storage import build_feedback_store, FeedbackStorageError
from persistent_state import load_latest_doc_run, save_latest_doc_run, clear_latest_doc_run
from components import (
    DOC_HIGHLIGHT_OPTIONS,
    DOC_IMPROVEMENT_OPTIONS,
    FeedbackUI,
    create_live_placeholder,
    render_live_answer,
)
from services import QuestionExtractor, Responder
from workflows import DocumentJobController


# ---------------------------------------------------------------------------
# Initial page configuration and dependency bootstrap
# ---------------------------------------------------------------------------


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


def install_packages(package: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])


configure_page()


@st.cache_resource
def cached_install(package: str) -> None:
    install_packages(package)


SETUP_VERSION = "2025-09-azure-feedback"

REQUIRED_PACKAGES = [
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
    total = len(REQUIRED_PACKAGES)
    for idx, package in enumerate(REQUIRED_PACKAGES, start=1):
        try:
            cached_install(package)
        except subprocess.CalledProcessError:
            progress_placeholder.empty()
            st.error(
                "Something went wrong while setting things up. Please try again or contact support."
            )
            return
        percent = int(idx / total * 100)
        message = f"Setting up step {idx} of {total}..."
        progress_placeholder.info(f"{message} ({percent}% complete)")

    progress_placeholder.success("Setup complete.")
    st.session_state["setup_version"] = SETUP_VERSION
    st.toast(
        "You're all set! Choose 'Upload document' to load an RFP or 'Ask a question' to chat. "
        "Provide any required API keys in the sidebar."
    )


ensure_packages()


# ---------------------------------------------------------------------------
# Session-state helpers and feedback plumbing
# ---------------------------------------------------------------------------


def initialize_session_state() -> None:
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
        pass


def get_current_user() -> str:
    return st.session_state.get("current_user_id", "demo_user")


def serialize_list(items: Optional[List[str]]) -> str:
    if not items:
        return ""
    return " | ".join(item.strip() for item in items if item)


def log_feedback(record: Dict[str, Any]) -> None:
    try:
        feedback_store.append(record)
    except FeedbackStorageError as exc:
        st.error(f"Unable to save feedback: {exc}")


def format_context(context: Dict[str, Any]) -> str:
    try:
        return json.dumps(context, ensure_ascii=False)
    except Exception:
        return ""


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

try:
    feedback_store = build_feedback_store(FEEDBACK_FIELDS, LOCAL_FEEDBACK_FILE)
except FeedbackStorageError as exc:
    st.error(f"Feedback storage is unavailable: {exc}")
    st.stop()


# ---------------------------------------------------------------------------
# Document workflow helpers
# ---------------------------------------------------------------------------


def _reset_doc_downloads() -> None:
    st.session_state["doc_downloads"] = {}


def _trigger_rerun() -> None:
    rerun_fn = getattr(st, "experimental_rerun", None)
    if callable(rerun_fn):
        rerun_fn()
        return
    rerun_fn = getattr(st, "rerun", None)
    if callable(rerun_fn):
        rerun_fn()
        return
    raise RuntimeError("Streamlit rerun API unavailable; update Streamlit to a newer version.")


def _reset_doc_workflow(*, clear_file: bool = False) -> None:
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


# ---------------------------------------------------------------------------
# Model metadata and utilities
# ---------------------------------------------------------------------------

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
    path = Path("structured_extraction/parsed_json_outputs/embedding_data.json")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    tags = {t for item in data for t in item.get("metadata", {}).get("tags", [])}
    return sorted(tags)


class OpenAIClient:
    def __init__(self, model: str) -> None:
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


@dataclass
class AppConfig:
    framework: str
    llm_model: str
    search_mode: str
    fund: str
    k_max_hits: int
    min_confidence: float
    length_opt: str
    approx_words: Optional[int]
    include_citations: bool
    show_live: bool
    docx_as_text: bool
    docx_write_mode: str
    extra_uploads: List[Any] = field(default_factory=list)


class StreamlitApp:
    def __init__(self) -> None:
        self.feedback = FeedbackUI(
            log_feedback=log_feedback,
            get_current_user=get_current_user,
            serialize_list=serialize_list,
            format_context=format_context,
        )
        self.document_controller = DocumentJobController(self.feedback)

    def run(self) -> None:
        st.title("RFP Responder")
        initialize_session_state()
        self._render_styles()

        view_mode = "User"
        input_mode = st.radio(
            "How would you like to proceed?",
            ["Upload document", "Ask a question"],
            index=1,
            horizontal=True,
        )

        framework, llm_model = self._select_framework(view_mode)
        self._ensure_api_credentials(framework, view_mode)

        uploaded = self._handle_primary_upload(input_mode)
        config = self._collect_configuration(view_mode, framework, llm_model)

        if input_mode == "Ask a question":
            self._render_chat_mode(view_mode, config)
        else:
            self._render_document_mode(view_mode, config, uploaded)

    def _render_styles(self) -> None:
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

    def _select_framework(self, view_mode: str) -> Tuple[str, str]:
        doc_default_model = (
            DOC_DEFAULT_MODEL if DOC_DEFAULT_MODEL in MODEL_OPTIONS else MODEL_OPTIONS[DEFAULT_INDEX]
        )
        llm_model = doc_default_model
        framework_env = os.getenv("ANSWER_FRAMEWORK")
        if framework_env:
            if view_mode == "Developer":
                st.info(f"Using framework from ANSWER_FRAMEWORK: {framework_env}")
            framework = framework_env
        else:
            framework = st.selectbox(
                "Framework",
                ["aladdin", "openai"],
                index=0,
                help="Choose backend for language model.",
            )
        return framework, llm_model

    def _ensure_api_credentials(self, framework: str, view_mode: str) -> None:
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
                    val = st.text_input(
                        label,
                        type="password" if "passwd" in key or "api_key" in key else "default",
                    )
                    if val:
                        os.environ[key] = val
        else:
            if os.getenv("OPENAI_API_KEY"):
                if view_mode == "Developer":
                    st.info("OPENAI_API_KEY loaded from environment")
            else:
                api_key = st.text_input(
                    "OpenAI API key",
                    type="password",
                    help="API key for OpenAI.",
                )
                if api_key:
                    os.environ["OPENAI_API_KEY"] = api_key

    def _collect_configuration(self, view_mode: str, framework: str, llm_model: str) -> AppConfig:
        if view_mode == "Developer":
            st.info("Search mode fixed to 'both'")
            search_mode = "both"
            fund = st.selectbox(
                "Fund",
                [""] + load_fund_tags(),
                index=0,
                help="Filter answers for a specific fund or strategy.",
            )
            llm_model = st.selectbox(
                "LLM model",
                MODEL_OPTIONS,
                index=DEFAULT_INDEX,
                format_func=lambda m: f"{m} - {MODEL_DESCRIPTIONS[m]}",
                help="Model name for generating answers.",
            )
            k_max_hits = st.number_input(
                "Hits per question", value=10, help="Maximum documents retrieved per question."
            )
            min_confidence = st.number_input(
                "Min confidence", value=0.0, help="Minimum score for retrieved documents."
            )
            docx_as_text = st.checkbox("Treat DOCX as text", value=False)
            docx_write_mode = st.selectbox("DOCX write mode", ["fill", "replace", "append"], index=0)
            extra_uploads = st.file_uploader(
                "Additional documents",
                type=["pdf", "docx", "xls", "xlsx"],
                accept_multiple_files=True,
                help="Additional PDF or Word documents to include in search.",
            )
            extra_uploads = self._filter_extra_uploads(extra_uploads)
        else:
            search_mode = "both"
            fund = st.selectbox(
                "Fund",
                [""] + load_fund_tags(),
                index=0,
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
            extra_uploads = self._filter_extra_uploads(extra_uploads)

        with st.expander("More options"):
            if view_mode == "User":
                llm_model = st.selectbox(
                    "Model",
                    MODEL_OPTIONS,
                    index=MODEL_OPTIONS.index(llm_model),
                    format_func=lambda m: f"{MODEL_SHORT_NAMES[m]} - {MODEL_DESCRIPTIONS[m]}",
                    help="Choose which model generates answers.",
                )
            length_opt = st.selectbox(
                "Answer length", ["auto", "short", "medium", "long"], index=3
            )
            approx_words_text = st.text_input(
                "Approx words",
                value="",
                help="Approximate words per answer (optional).",
            )
            include_env = os.getenv("RFP_INCLUDE_COMMENTS")
            if include_env is not None:
                include_citations = include_env != "0"
                st.info(f"Using include citations from RFP_INCLUDE_COMMENTS: {include_citations}")
            else:
                include_citations = st.checkbox("Include citations", value=True)
            show_live = st.checkbox(
                "Show questions and answers during processing", value=True
            )

        approx_words = None
        if approx_words_text:
            try:
                approx_words = int(approx_words_text)
            except ValueError:
                st.warning("Approx words must be an integer. Ignoring value.")

        return AppConfig(
            framework=framework,
            llm_model=llm_model,
            search_mode=search_mode,
            fund=fund,
            k_max_hits=int(k_max_hits),
            min_confidence=float(min_confidence),
            length_opt=length_opt,
            approx_words=approx_words,
            include_citations=include_citations,
            show_live=show_live,
            docx_as_text=bool(docx_as_text),
            docx_write_mode=docx_write_mode,
            extra_uploads=extra_uploads,
        )

    def _filter_extra_uploads(self, files) -> List[Any]:
        if not files:
            return []
        allowed: List[Any] = []
        invalid: List[str] = []
        for file in files:
            if Path(file.name).suffix.lower() in {".pdf", ".docx", ".xls", ".xlsx"}:
                allowed.append(file)
            else:
                invalid.append(file.name)
        if invalid:
            st.warning("Unsupported file types were ignored: " + ", ".join(invalid))
        return allowed

    def _handle_primary_upload(self, input_mode: str):
        if input_mode != "Upload document":
            return None

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
            st.caption(
                f"Current document: **{file_info.get('name', 'unknown')}** ({size_display})"
            )
            if st.button(
                "Clear current document",
                key="clear_current_document",
                help="Forget the uploaded document and reset progress.",
            ):
                path_to_remove = file_info.get("path")
                if path_to_remove:
                    try:
                        Path(path_to_remove).unlink(missing_ok=True)
                    except Exception:
                        pass
                _reset_doc_workflow(clear_file=True)
                st.success("Document cleared. Upload a new file to start again.")
                try:
                    _trigger_rerun()
                except Exception:
                    st.stop()
        return uploaded

    def _prepare_extra_documents(self, uploads: List[Any]) -> Tuple[List[str], List[str]]:
        paths: List[str] = []
        names: List[str] = []
        for extra in uploads:
            saved_path = save_uploaded_file(extra)
            paths.append(saved_path)
            names.append(extra.name)
        return paths, names

    def _render_chat_mode(self, view_mode: str, config: AppConfig) -> None:
        extra_docs = [save_uploaded_file(f) for f in config.extra_uploads] if config.extra_uploads else []
        llm = (
            CompletionsClient(model=config.llm_model)
            if config.framework == "aladdin"
            else OpenAIClient(model=config.llm_model)
        )
        responder = Responder(
            llm_client=llm,
            search_mode=config.search_mode,
            fund=config.fund,
            k=int(config.k_max_hits),
            length=config.length_opt,
            approx_words=config.approx_words,
            min_confidence=config.min_confidence,
            include_citations=config.include_citations,
            extra_docs=extra_docs,
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
        with sidebar:
            st.markdown("### Tools")
            if st.button(
                "Clear chat history",
                key="clear_chat_history",
                help="Remove all chat messages and context.",
            ):
                st.session_state.chat_messages = []
                st.session_state.question_history = []
                try:
                    my_module.QUESTION_HISTORY.clear()
                except Exception:
                    pass
                st.success("Chat history cleared.")
                try:
                    _trigger_rerun()
                except Exception:
                    pass
        sidebar.markdown("### References")

        answer_idx = 0
        last_user_message = None
        for idx, msg in enumerate(st.session_state.chat_messages):
            with st.chat_message(msg.get("role")):
                if msg.get("role") == "user":
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
                                    <div class="hit-meta">{' · '.join(meta_parts)}</div>
                                    {reason_block}
                                    <div class="hit-snippet">{snippet}</div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                            snippet_plain = (hit.get("snippet") or "").strip()
                            if snippet_plain:
                                summary_lines.append(f"{i}. {snippet_plain}")
                        if summary_lines:
                            answer_summary = f"{answer_summary}\n" + "\n".join(summary_lines)
                    self.feedback.render_chat_feedback_form(
                        message_index=idx,
                        question=last_user_message,
                        answer=answer_summary.strip(),
                        message_payload=msg,
                    )
                    continue
                st.markdown(msg.get("content", ""))
                if msg.get("model"):
                    label_model = (
                        MODEL_SHORT_NAMES.get(msg["model"], msg["model"])
                        if view_mode == "User"
                        else msg["model"]
                    )
                    st.caption(f"Model: {label_model}")
                if view_mode == "Developer" and msg.get("debug"):
                    st.expander("Debug info").markdown(f"```\n{msg['debug']}\n```")
                if msg.get("role") == "assistant" and "hits" not in msg:
                    answer_idx += 1
                    sidebar.markdown(f"**Answer {answer_idx}**")
                    citations_map = msg.get("citations") or {}
                    if citations_map:
                        for lbl, cite in citations_map.items():
                            source_name = (cite.get('source_file') or 'Unknown').strip() or 'Unknown'
                            with sidebar.expander(f"[{lbl}] {source_name}"):
                                snippet_text = (cite.get('text') or '').strip()
                                if snippet_text:
                                    st.markdown(snippet_text)
                                else:
                                    st.caption("Snippet not available.")
                    else:
                        sidebar.caption("No source details returned.")
                    self.feedback.render_chat_feedback_form(
                        message_index=idx,
                        question=last_user_message,
                        answer=msg.get("content", ""),
                        message_payload=msg,
                    )

        history = list(st.session_state.get("question_history", []))
        if prompt := st.chat_input("Ask a question"):
            st.chat_message("user").markdown(prompt)
            st.session_state.chat_messages.append({"role": "user", "content": prompt})

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
                            snippet_plain = (hit.get("snippet") or "").strip()
                            if snippet_plain:
                                summary_lines.append(f"{display_idx}. {snippet_plain}")
                            snippet_html = html.escape(hit.get("snippet", ""))
                            source_html = html.escape(hit.get("source") or "Unknown")
                            meta_parts = [f"<strong>{display_idx}. {source_html}</strong>"]
                            score_val = hit.get("score")
                            if isinstance(score_val, (int, float)):
                                meta_parts.append(f"Score {score_val:.3f}")
                            elif score_val:
                                meta_parts.append(f"Score {html.escape(str(score_val))}")
                            date_str = hit.get("date")
                            if date_str:
                                meta_parts.append(html.escape(str(date_str)))
                            original_idx = hit.get("original_index")
                            if original_idx and original_idx != display_idx:
                                meta_parts.append(f"Orig #{original_idx}")
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
                                    <div class="hit-meta">{' · '.join(meta_parts)}</div>
                                    {reason_block}
                                    <div class="hit-snippet">{snippet_html}</div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                        msg = {
                            "role": "assistant",
                            "content": "**Closest pre-approved answers**",
                            "hits": hits_to_show,
                        }
                        answer_summary = msg.get("content", "")
                        if summary_lines:
                            answer_summary = f"{answer_summary}\n" + "\n".join(summary_lines)
                        self.feedback.render_chat_feedback_form(
                            message_index=len(st.session_state.chat_messages),
                            question=prompt,
                            answer=answer_summary.strip(),
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
                        self.feedback.render_chat_feedback_form(
                            message_index=len(st.session_state.chat_messages),
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
                    response_model = config.llm_model
                    restore_client = None
                    call_fn = gen_answer if intent == "follow_up" else responder.answer
                    try:
                        if intent == "follow_up" and view_mode != "Developer":
                            response_model = FOLLOWUP_DEFAULT_MODEL
                            followup_llm = (
                                llm
                                if response_model == config.llm_model
                                else (
                                    CompletionsClient(model=response_model)
                                    if config.framework == "aladdin"
                                    else OpenAIClient(model=response_model)
                                )
                            )
                            restore_client = my_module._llm_client
                            my_module._llm_client = followup_llm
                        if buf:
                            with contextlib.redirect_stdout(buf):
                                result = call_fn(
                                    prompt,
                                    follow=follow,
                                    history=history,
                                    responder=responder,
                                    include_citations=config.include_citations,
                                    update_status=update_status,
                                )
                        else:
                            result = call_fn(
                                prompt,
                                follow=follow,
                                history=history,
                                responder=responder,
                                include_citations=config.include_citations,
                                update_status=update_status,
                            )
                    finally:
                        if restore_client is not None:
                            my_module._llm_client = restore_client
                    _set_answer_status("Stage 4/4 - Answer ready.", final=True)
                    if isinstance(result, tuple):
                        ans, comments = result
                        payload = {
                            "text": ans.get("text") if isinstance(ans, dict) else ans,
                            "citations": ans.get("citations") if isinstance(ans, dict) else {},
                            "raw_comments": comments,
                            "model": response_model,
                        }
                    else:
                        payload = result

                    debug_text = buf.getvalue() if buf else ""
                    if isinstance(payload, dict):
                        text = payload.get("text", "")
                        citations = payload.get("citations") or {}
                    else:
                        text = str(payload)
                        citations = {}
                    message_placeholder.markdown(text)
                    label = (
                        MODEL_SHORT_NAMES.get(response_model, response_model)
                        if view_mode == "User"
                        else response_model
                    )
                    st.caption(f"Model: {label}")
                    if view_mode == "Developer":
                        st.expander("Debug info").markdown(f"```\n{debug_text}\n```")
                    if intent != "follow_up":
                        my_module.QUESTION_HISTORY.append(prompt)
                        my_module.QA_HISTORY.append({"question": prompt, "answer": text, "citations": []})
                    self.feedback.render_chat_feedback_form(
                        message_index=len(st.session_state.chat_messages),
                        question=prompt,
                        answer=text,
                        message_payload=payload,
                    )
                    msg = {
                        "role": "assistant",
                        "content": text,
                        "citations": citations,
                        "model": response_model,
                    }
                st.session_state.chat_messages.append(msg)
                history.append(prompt)
                st.session_state.question_history = history

    def _render_document_mode(self, view_mode: str, config: AppConfig, uploaded) -> None:
        run_clicked = st.button("Run")
        file_info = st.session_state.get("doc_file_info") or {}
        document_ready = bool(st.session_state.get("doc_file_ready") and file_info)
        job = st.session_state.get("doc_job")

        if run_clicked:
            if job and job.get("status") == "running":
                st.warning("A document run is already in progress. Please wait for it to finish.")
            elif not document_ready:
                st.warning("Please upload a document before running.")
            elif not config.fund:
                st.warning("Please select a fund or strategy before running.")
            elif not file_info.get("path"):
                st.warning("The uploaded document could not be cached. Please upload it again.")
            else:
                extra_doc_paths, extra_doc_names = self._prepare_extra_documents(config.extra_uploads)
                run_config = {
                    "input_path": file_info.get("path"),
                    "file_name": file_info.get("name"),
                    "suffix": file_info.get("suffix", "").lower(),
                    "include_citations": config.include_citations,
                    "show_live": config.show_live,
                    "search_mode": config.search_mode,
                    "fund": config.fund,
                    "k_max_hits": int(config.k_max_hits),
                    "length_opt": config.length_opt,
                    "approx_words": config.approx_words,
                    "min_confidence": float(config.min_confidence),
                    "framework": config.framework,
                    "docx_as_text": config.docx_as_text,
                    "docx_write_mode": config.docx_write_mode,
                    "extra_doc_paths": extra_doc_paths,
                    "extra_doc_names": extra_doc_names,
                }
                llm = (
                    CompletionsClient(model=config.llm_model)
                    if config.framework == "aladdin"
                    else OpenAIClient(model=config.llm_model)
                )
                responder = Responder(
                    llm_client=llm,
                    search_mode=config.search_mode,
                    fund=config.fund,
                    k=int(config.k_max_hits),
                    length=config.length_opt,
                    approx_words=config.approx_words,
                    min_confidence=config.min_confidence,
                    include_citations=config.include_citations,
                    extra_docs=extra_doc_paths,
                )
                extractor = QuestionExtractor(llm)
                job = self.document_controller.schedule(
                    config=run_config,
                    responder=responder,
                    extractor=extractor,
                )
                st.session_state["doc_job"] = job
                st.session_state["doc_processing_state"] = "running"
                st.session_state["doc_processing_started_at"] = datetime.utcnow().isoformat()
                st.session_state["doc_processing_result"] = None
                st.session_state["doc_processing_error"] = None
                st.session_state["doc_feedback_submitted"] = False
                st.session_state["doc_card_feedback_submitted"] = {}
                _reset_doc_downloads()
                _trigger_rerun()

        job = st.session_state.get("doc_job")
        if job and job.get("status") in {"running", "ready_for_finalize"}:
            self.document_controller.update(job)
            if job.get("status") == "ready_for_finalize":
                self.document_controller.finalize(job)
                job = st.session_state.get("doc_job")
            self.document_controller.render(
                job,
                include_citations=config.include_citations,
                show_live=config.show_live,
            )
            if job.get("status") == "finished":
                self.document_controller.register_downloads(
                    job,
                    reset_downloads=_reset_doc_downloads,
                    store_download=_store_doc_download,
                )
                if not job.get("completion_notified"):
                    st.success(
                        "Document processing completed. You can download the results below or start another run."
                    )
                    st.session_state["doc_processing_state"] = "finished"
                    st.session_state["doc_processing_result"] = job.get("run_context")
                    st.session_state["doc_processing_error"] = None
                    st.session_state["doc_processing_finished_at"] = datetime.utcnow().isoformat()
                    st.session_state["latest_doc_run"] = job.get("run_context")
                    try:
                        persist_key = st.session_state.get(
                            "current_user_id", st.session_state.get("session_id", "")
                        )
                        if job.get("run_context"):
                            save_latest_doc_run(persist_key, job["run_context"])
                    except Exception:
                        pass
                    job["completion_notified"] = True
                    if job.get("run_context"):
                        self._render_document_feedback_section(job["run_context"])
                        self._render_saved_qa_pairs(job["run_context"])
            else:
                if not st.session_state.get("suspend_autorefresh", False):
                    time.sleep(0.25)
                    _trigger_rerun()
        elif job and job.get("status") == "finished":
            self.document_controller.register_downloads(
                job,
                reset_downloads=_reset_doc_downloads,
                store_download=_store_doc_download,
            )
            self.document_controller.render(
                job,
                include_citations=config.include_citations,
                show_live=config.show_live,
            )
            st.session_state["doc_processing_state"] = "finished"
            st.session_state["doc_processing_result"] = job.get("run_context")
            if job.get("run_context"):
                self._render_document_feedback_section(job["run_context"])
                self._render_saved_qa_pairs(job["run_context"])
        else:
            latest = st.session_state.get("latest_doc_run")
            if latest:
                self._render_document_feedback_section(latest)
                self._render_saved_qa_pairs(latest)
                with st.container():
                    col1, col2 = st.columns([1, 6])
                    with col1:
                        if st.button(
                            "Clear saved run",
                            key="clear_saved_run",
                            help="Remove the last run from memory and disk.",
                        ):
                            st.session_state.latest_doc_run = None
                            st.session_state.doc_feedback_submitted = False
                            st.session_state["doc_job"] = None
                            _reset_doc_workflow(clear_file=False)
                            _reset_doc_downloads()
                            try:
                                persist_key = st.session_state.get(
                                    "current_user_id", st.session_state.get("session_id", "")
                                )
                                clear_latest_doc_run(persist_key)
                            except Exception:
                                pass
                            st.success("Saved run cleared.")
                            _trigger_rerun()
            else:
                st.info("Upload a document and click Run to begin.")

        _render_doc_downloads()

    def _render_document_feedback_section(self, run_context: Optional[dict]) -> None:
        if not run_context:
            return
        submitted = st.session_state.get("doc_feedback_submitted", False)
        with st.expander("Share feedback on this document run", expanded=not submitted):
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
                comment = st.text_area(
                    "Additional comments",
                    placeholder="Optional details…",
                )
                submitted_form = st.form_submit_button(
                    "Submit feedback", use_container_width=True
                )
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

    def _render_saved_qa_pairs(self, run_context: Optional[dict]) -> None:
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
            placeholder = create_live_placeholder(qa_box, idx, q_text)
            ans_payload = pair.get("answer")
            comments = pair.get("comments") or []
            if not comments and isinstance(ans_payload, dict):
                raw_comments = ans_payload.get("citations") or ans_payload.get("comments") or []
                comments = raw_comments
            render_live_answer(
                placeholder,
                ans_payload,
                comments,
                include_citations and (isinstance(ans_payload, dict) or bool(comments)),
                feedback=self.feedback,
                card_index=idx,
                question_text=q_text,
                run_context=run_context,
                use_dialog=True,
            )


def main() -> None:
    app = StreamlitApp()
    app.run()


if __name__ == "__main__":
    main()
