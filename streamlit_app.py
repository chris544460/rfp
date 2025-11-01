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
import threading
import time
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
from components import (
    DOC_HIGHLIGHT_OPTIONS,
    DOC_IMPROVEMENT_OPTIONS,
    FeedbackUI,
    create_live_placeholder,
    render_live_answer,
)
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
    st.session_state.setdefault("doc_job", None)
    st.session_state.setdefault("suspend_autorefresh", False)
    st.session_state.setdefault("feedback_dialog_target", None)

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


feedback_ui = FeedbackUI(
    log_feedback=log_feedback,
    get_current_user=get_current_user,
    serialize_list=serialize_list,
    format_context=format_context,
)
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


def _trigger_rerun() -> None:
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


def _schedule_document_job(config: Dict[str, Any]) -> None:
    """Prepare question extraction and schedule answer generation futures."""

    input_path = config["input_path"]
    suffix = config["suffix"]
    include_citations = config["include_citations"]
    extra_doc_paths = config.get("extra_doc_paths", [])
    extra_doc_names = config.get("extra_doc_names", [])
    framework = config["framework"]
    llm_model = config["llm_model"]

    llm = CompletionsClient(model=llm_model) if framework == "aladdin" else OpenAIClient(model=llm_model)
    responder = Responder(
        llm_client=llm,
        search_mode=config["search_mode"],
        fund=config["fund"],
        k=config["k_max_hits"],
        length=config["length_opt"],
        approx_words=config["approx_words"],
        min_confidence=config["min_confidence"],
        include_citations=include_citations,
        extra_docs=list(extra_doc_paths),
    )
    extractor = QuestionExtractor(llm)

    job: Dict[str, Any] = {
        "status": "running",
        "mode": None,
        "config": config,
        "executor": None,
        "futures": [],
        "future_info": {},
        "answers": [],
        "questions": [],
        "questions_text": [],
        "schema": [],
        "slots_payload": {},
        "skipped_slots": [],
        "heuristic_skips": [],
        "downloads": [],
        "run_context": None,
        "extra_doc_names": extra_doc_names,
        "started_at": datetime.utcnow().isoformat(),
        "completed": 0,
        "downloads_registered": False,
        "completion_notified": False,
    }

    if suffix in {".xlsx", ".xls"}:
        questions = extractor.extract(input_path)
        schema = extractor.last_details.get("schema") or []
        questions_text = [(entry.get("question") or "").strip() for entry in questions]
        total = len(questions_text)
        job.update(
            {
                "mode": "excel",
                "questions": questions,
                "questions_text": questions_text,
                "schema": schema,
                "answers": [None] * total,
            }
        )
        if total > 0:
            worker_limit = _resolve_concurrency(None) or total
            worker_limit = max(1, min(worker_limit, total))
            executor = ThreadPoolExecutor(max_workers=worker_limit)
            job["executor"] = executor
            for idx, question_text in enumerate(questions_text):
                future = executor.submit(_run_excel_task, responder, question_text)
                job["futures"].append(future)
                job["future_info"][future] = {"index": idx, "question_text": question_text}
    elif suffix == ".docx" and not config["docx_as_text"]:
        questions = extractor.extract(input_path)
        details = extractor.last_details
        slots_payload = details.get("slots_payload") or {}
        slot_list = [entry.get("slot") for entry in questions]
        slot_list = [slot for slot in slot_list if slot is not None]
        questions_text = [(slot.get("question_text") or "").strip() for slot in slot_list]
        total = len(slot_list)
        job.update(
            {
                "mode": "docx_slots",
                "questions": slot_list,
                "questions_text": questions_text,
                "slots_payload": slots_payload,
                "skipped_slots": details.get("skipped_slots") or [],
                "heuristic_skips": details.get("heuristic_skips") or [],
                "answers": [None] * total,
            }
        )
        if total > 0:
            worker_limit = _resolve_concurrency(None) or total
            worker_limit = max(1, min(worker_limit, total))
            executor = ThreadPoolExecutor(max_workers=worker_limit)
            job["executor"] = executor
            for idx, slot in enumerate(slot_list):
                future = executor.submit(_run_docx_task, responder, slot)
                job["futures"].append(future)
                job["future_info"][future] = {"index": idx, "slot_id": slot.get("id")}
    else:
        treat_docx_as_text = suffix == ".docx" and config["docx_as_text"]
        questions = extractor.extract(input_path, treat_docx_as_text=treat_docx_as_text)
        questions_text = [(entry.get("question") or "").strip() for entry in questions]
        total = len(questions_text)
        job.update(
            {
                "mode": "document_summary",
                "questions": questions,
                "questions_text": questions_text,
                "answers": [None] * total,
                "treat_docx_as_text": treat_docx_as_text,
            }
        )
        if total > 0:
            worker_limit = _resolve_concurrency(None) or total
            worker_limit = max(1, min(worker_limit, total))
            executor = ThreadPoolExecutor(max_workers=worker_limit)
            job["executor"] = executor
            for idx, question_text in enumerate(questions_text):
                future = executor.submit(_run_summary_task, responder, question_text)
                job["futures"].append(future)
                job["future_info"][future] = {"index": idx, "question_text": question_text}

    job["include_citations"] = include_citations
    job["responder_model"] = llm_model
    st.session_state["doc_job"] = job
    st.session_state["doc_processing_state"] = "running"
    st.session_state["doc_processing_started_at"] = datetime.utcnow().isoformat()
    st.session_state["doc_processing_result"] = None
    st.session_state["doc_processing_error"] = None
    st.session_state["doc_feedback_submitted"] = False
    st.session_state["doc_card_feedback_submitted"] = {}


def _run_excel_task(responder: Responder, question_text: str) -> Dict[str, Any]:
    result = responder.answer(question_text)
    return {
        "question": question_text,
        "answer_payload": result,
        "storage_answer": {
            "text": result["text"],
            "citations": result["citations"],
        },
        "comments": result.get("raw_comments", []),
    }


def _run_docx_task(responder: Responder, slot: Dict[str, Any]) -> Dict[str, Any]:
    question_text = (slot.get("question_text") or "").strip()
    result = responder.answer(question_text)
    if _is_table_slot(slot):
        sanitized = _sanitize_table_answer(result)
        display_payload: Any = sanitized
        storage_answer = {"text": sanitized, "citations": {}}
        comments: List[Any] = []
    else:
        display_payload = result
        storage_answer = {"text": result["text"], "citations": result["citations"]}
        comments = result.get("raw_comments", [])
    return {
        "question": question_text,
        "slot_id": slot.get("id"),
        "answer_payload": display_payload,
        "storage_answer": storage_answer,
        "comments": comments,
    }


def _run_summary_task(responder: Responder, question_text: str) -> Dict[str, Any]:
    result = responder.answer(question_text)
    return {
        "question": question_text,
        "answer_payload": result,
        "storage_answer": {
            "text": result["text"],
            "citations": result["citations"],
        },
        "comments": result.get("raw_comments", []),
    }


def _update_document_job(job: Dict[str, Any]) -> None:
    """Poll futures for completed answers and update job bookkeeping."""

    if job.get("status") != "running":
        return

    future_info: Dict[Any, Dict[str, Any]] = job.get("future_info", {})
    answers: List[Optional[Dict[str, Any]]] = job.get("answers", [])
    changed = False

    for future in list(future_info.keys()):
        info = future_info[future]
        if future.done():
            idx = info["index"]
            if 0 <= idx < len(answers) and answers[idx] is None:
                try:
                    result = future.result()
                except Exception as exc:
                    error_text = f"[error] {exc}"
                    result = {
                        "question": info.get("question_text") or "",
                        "answer_payload": error_text,
                        "storage_answer": {"text": error_text, "citations": {}},
                        "comments": [],
                        "error": True,
                    }
                answers[idx] = result
                changed = True
            del future_info[future]

    if changed:
        job["completed"] = sum(1 for entry in answers if entry is not None)

    if not future_info:
        executor = job.get("executor")
        if executor:
            executor.shutdown(wait=False)
            job["executor"] = None
        if job.get("status") == "running":
            job["status"] = "ready_for_finalize"


def _finalize_document_job(job: Dict[str, Any]) -> None:
    """Generate output artifacts once all answers are ready."""

    if job.get("status") not in {"ready_for_finalize", "running"}:
        return

    include_citations = job.get("include_citations", True)
    config = job["config"]
    answers: List[Optional[Dict[str, Any]]] = job.get("answers", [])
    questions_text: List[str] = job.get("questions_text", [])
    filler = DocumentFiller()
    mode = job.get("mode")

    if mode == "excel":
        schema = job.get("schema") or []
        qa_results = []
        for idx in range(len(answers)):
            entry = answers[idx]
            question_text = questions_text[idx] if idx < len(questions_text) else ""
            if entry is None:
                storage = {"text": "No answer generated.", "citations": {}}
                comments: List[Any] = []
            else:
                storage = entry["storage_answer"]
                comments = entry.get("comments", [])
            qa_results.append(
                {
                    "question": question_text,
                    "answer": storage.get("text", ""),
                    "citations": storage.get("citations", {}),
                    "raw_comments": comments,
                }
            )
        bundle = filler.build_excel_bundle(
            source_path=config["input_path"],
            schema=schema,
            qa_results=qa_results,
            include_citations=include_citations,
            mode="fill",
        )
        run_context = {
            "mode": "excel",
            "uploaded_name": config["file_name"],
            "fund": config["fund"],
            "search_mode": config["search_mode"],
            "include_citations": include_citations,
            "length": config["length_opt"],
            "approx_words": config["approx_words"],
            "extra_documents": job.get("extra_doc_names", []),
            "qa_pairs": bundle.get("qa_pairs", []),
            "schema": schema,
            "timestamp": datetime.utcnow().isoformat(),
        }
    elif mode == "docx_slots":
        slots_payload = job.get("slots_payload") or {}
        slots = job.get("questions") or []
        qa_results = []
        for idx in range(len(answers)):
            entry = answers[idx]
            slot = slots[idx] if idx < len(slots) else {}
            question_text = questions_text[idx] if idx < len(questions_text) else (slot.get("question_text") or "")
            slot_id = slot.get("id")
            if entry is None:
                storage = {"text": "No answer generated.", "citations": {}}
                comments = []
            else:
                storage = entry["storage_answer"]
                comments = entry.get("comments", [])
                if slot_id is None:
                    slot_id = entry.get("slot_id")
            qa_results.append(
                {
                    "question": question_text,
                    "answer": storage.get("text", ""),
                    "citations": storage.get("citations", {}),
                    "raw_comments": comments,
                    "slot_id": slot_id,
                }
            )
        bundle = filler.build_docx_slot_bundle(
            source_path=config["input_path"],
            slots_payload=slots_payload,
            qa_results=qa_results,
            include_citations=include_citations,
            write_mode=config["docx_write_mode"],
        )
        run_context = {
            "mode": "docx_slots",
            "uploaded_name": config["file_name"],
            "fund": config["fund"],
            "search_mode": config["search_mode"],
            "include_citations": include_citations,
            "docx_write_mode": config["docx_write_mode"],
            "extra_documents": job.get("extra_doc_names", []),
            "qa_pairs": bundle.get("qa_pairs", []),
            "slots": slots_payload,
            "skipped_slots": job.get("skipped_slots", []),
            "heuristic_skips": job.get("heuristic_skips", []),
            "timestamp": datetime.utcnow().isoformat(),
        }
    else:
        qa_results = []
        total = len(questions_text)
        for idx in range(total):
            entry = answers[idx] if idx < len(answers) else None
            if entry is None:
                storage = {"text": "No answer generated.", "citations": {}}
                comments = []
            else:
                storage = entry["storage_answer"]
                comments = entry.get("comments", [])
            qa_results.append(
                {
                    "answer": storage.get("text", ""),
                    "citations": storage.get("citations", {}),
                    "raw_comments": comments,
                }
            )
        bundle = filler.build_summary_bundle(
            questions=questions_text,
            qa_results=qa_results,
            include_citations=include_citations,
        )
        run_context = {
            "mode": "document_summary",
            "uploaded_name": config["file_name"],
            "fund": config["fund"],
            "search_mode": config["search_mode"],
            "include_citations": include_citations,
            "length": config["length_opt"],
            "approx_words": config["approx_words"],
            "extra_documents": job.get("extra_doc_names", []),
            "qa_pairs": bundle.get("qa_pairs", []),
            "timestamp": datetime.utcnow().isoformat(),
        }

    job["downloads"] = bundle.get("downloads", [])
    job["run_context"] = run_context
    job["status"] = "finished"
    job["completed"] = len([entry for entry in answers if entry is not None])


def _register_job_downloads(job: Dict[str, Any]) -> None:
    """Persist job downloads into session download bucket once."""

    if not job or not job.get("downloads"):
        if job is not None:
            job["downloads_registered"] = True
        return
    if job.get("downloads_registered"):
        return
    _reset_doc_downloads()
    for item in job["downloads"]:
        _store_doc_download(
            item.get("key", f"download_{uuid4().hex[:8]}"),
            label=item.get("label", "Download file"),
            data=item.get("data", b""),
            file_name=item.get("file_name", "output"),
            mime=item.get("mime"),
            order=item.get("order", 0),
        )
    job["downloads_registered"] = True


def _render_document_job(job: Dict[str, Any], *, include_citations: bool, show_live: bool) -> None:
    """Display current progress and answered cards for an in-flight or completed job."""

    if not job:
        return

    answers: List[Optional[Dict[str, Any]]] = job.get("answers", [])
    questions_text: List[str] = job.get("questions_text", [])
    total = len(answers)
    if total == 0:
        st.info("No questions detected for this document.")
        return
    completed = job.get("completed", sum(1 for entry in answers if entry is not None))

    if total:
        progress_value = completed / total
        st.progress(progress_value, text=f"{completed}/{total}")

    if job.get("mode") == "docx_slots":
        skipped = job.get("skipped_slots") or []
        heuristic = job.get("heuristic_skips") or []
        if skipped or heuristic:
            st.warning(f"Skipped {len(skipped) + len(heuristic)} question(s) that cannot be answered automatically.")
            with st.expander("View skipped questions", expanded=False):
                for entry in skipped:
                    reason = entry.get("reason") or "unspecified"
                    q = (entry.get("question_text") or "").strip() or "[blank question text]"
                    st.markdown(f"- **{q}** — {reason}")
                for entry in heuristic:
                    reason = entry.get("reason", "unspecified")
                    q = (entry.get("question_text") or "").strip() or "[blank question text]"
                    st.markdown(f"- **{q}** — {reason}")

    qa_box = st.container()
    for idx in range(total):
        question_text = questions_text[idx] if idx < len(questions_text) else f"Question {idx + 1}"
        placeholder = create_live_placeholder(qa_box, idx, question_text)
        entry = answers[idx]
        if entry is None:
            continue
        payload = entry.get("answer_payload")
        comments = entry.get("comments", [])
        run_context = job.get("run_context") or {
            "uploaded_name": job["config"]["file_name"],
            "fund": job["config"]["fund"],
            "search_mode": job["config"]["search_mode"],
            "include_citations": include_citations,
        }
        render_live_answer(
            placeholder,
            payload,
            comments,
            include_citations,
            feedback=feedback_ui,
            card_index=idx,
            question_text=question_text,
            run_context=run_context,
            use_dialog=True,
        )



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
        placeholder = create_live_placeholder(qa_box, idx, q_text)
        ans_payload = pair.get("answer")
        comments = pair.get("comments") or []
        if not comments and isinstance(ans_payload, dict):
            raw_comments = ans_payload.get('citations') or ans_payload.get('comments') or []
            comments = raw_comments
        render_live_answer(
            placeholder,
            ans_payload,
            comments,
            include_citations and (isinstance(ans_payload, dict) or bool(comments)),
            feedback=feedback_ui,
            card_index=idx,
            question_text=q_text,
            run_context=run_context,
            use_dialog=True,
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
                _trigger_rerun()
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
                    _trigger_rerun()
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
                    feedback_ui.render_chat_feedback_form(
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
                    feedback_ui.render_chat_feedback_form(
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
                        feedback_ui.render_chat_feedback_form(
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
                        feedback_ui.render_chat_feedback_form(
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
                    feedback_ui.render_chat_feedback_form(
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
        file_info = st.session_state.get("doc_file_info") or {}
        document_ready = bool(st.session_state.get("doc_file_ready") and file_info)
        job = st.session_state.get("doc_job")

        if run_clicked:
            if job and job.get("status") == "running":
                st.warning("A document run is already in progress. Please wait for it to finish.")
            elif not document_ready:
                st.warning("Please upload a document before running.")
            elif not fund:
                st.warning("Please select a fund or strategy before running.")
            elif not file_info.get("path"):
                st.warning("The uploaded document could not be cached. Please upload it again.")
            else:
                extra_doc_paths: List[str] = []
                extra_doc_names: List[str] = []
                if extra_uploads:
                    for extra in extra_uploads:
                        saved_path = save_uploaded_file(extra)
                        extra_doc_paths.append(saved_path)
                        extra_doc_names.append(extra.name)
                config = {
                    "input_path": file_info.get("path"),
                    "file_name": file_info.get("name"),
                    "suffix": file_info.get("suffix", "").lower(),
                    "include_citations": include_citations,
                    "show_live": show_live,
                    "search_mode": search_mode,
                    "fund": fund,
                    "k_max_hits": int(k_max_hits),
                    "length_opt": length_opt,
                    "approx_words": int(approx_words) if approx_words else None,
                    "min_confidence": float(min_confidence),
                    "llm_model": llm_model,
                    "framework": framework,
                    "docx_as_text": docx_as_text,
                    "docx_write_mode": docx_write_mode,
                    "extra_doc_paths": extra_doc_paths,
                    "extra_doc_names": extra_doc_names,
                }
                _schedule_document_job(config)
                _trigger_rerun()

        job = st.session_state.get("doc_job")
        if job and job.get("status") in {"running", "ready_for_finalize"}:
            _update_document_job(job)
            if job.get("status") == "ready_for_finalize":
                _finalize_document_job(job)
                job = st.session_state.get("doc_job")
            _render_document_job(job, include_citations=include_citations, show_live=show_live)
            if job.get("status") == "finished":
                _register_job_downloads(job)
                if not job.get("completion_notified"):
                    st.success("Document processing completed. You can download the results below or start another run.")
                    st.session_state["doc_processing_state"] = "finished"
                    st.session_state["doc_processing_result"] = job.get("run_context")
                    st.session_state["doc_processing_error"] = None
                    st.session_state["doc_processing_finished_at"] = datetime.utcnow().isoformat()
                    st.session_state["latest_doc_run"] = job.get("run_context")
                    try:
                        persist_key = st.session_state.get("current_user_id", st.session_state.get("session_id", ""))
                        if job.get("run_context"):
                            save_latest_doc_run(persist_key, job["run_context"])
                    except Exception:
                        pass
                    job["completion_notified"] = True
                    if job.get("run_context"):
                        render_document_feedback_section(job["run_context"])
                        render_saved_qa_pairs(job["run_context"])
            else:
                if not st.session_state.get("suspend_autorefresh", False):
                    time.sleep(0.25)
                    _trigger_rerun()
        elif job and job.get("status") == "finished":
            _register_job_downloads(job)
            _render_document_job(job, include_citations=include_citations, show_live=show_live)
            st.session_state["doc_processing_state"] = "finished"
            st.session_state["doc_processing_result"] = job.get("run_context")
            if job.get("run_context"):
                render_document_feedback_section(job["run_context"])
                render_saved_qa_pairs(job["run_context"])
        else:
            latest = st.session_state.get("latest_doc_run")
            if latest:
                render_document_feedback_section(latest)
                render_saved_qa_pairs(latest)
                with st.container():
                    col1, col2 = st.columns([1, 6])
                    with col1:
                        if st.button("Clear saved run", key="clear_saved_run", help="Remove the last run from memory and disk."):
                            st.session_state.latest_doc_run = None
                            st.session_state.doc_feedback_submitted = False
                            st.session_state["doc_job"] = None
                            _reset_doc_workflow(clear_file=False)
                            _reset_doc_downloads()
                            try:
                                persist_key = st.session_state.get("current_user_id", st.session_state.get("session_id", ""))
                                clear_latest_doc_run(persist_key)
                            except Exception:
                                pass
                            st.success("Saved run cleared.")
                            _trigger_rerun()
            else:
                st.info("Upload a document and click Run to begin.")

        _render_doc_downloads()

if __name__ == "__main__":
    main()
