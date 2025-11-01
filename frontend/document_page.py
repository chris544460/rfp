from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import streamlit as st

from backend.components import (
    DOC_HIGHLIGHT_OPTIONS,
    DOC_IMPROVEMENT_OPTIONS,
    FeedbackUI,
    create_live_placeholder,
    render_live_answer,
)
from backend.services import QuestionExtractor, Responder
from backend.workflows import DocumentJobController

from backend.answer_composer import CompletionsClient
from frontend.config_panel import AppConfig
from frontend.feedback import FeedbackManager
from frontend.session_state import (
    clear_latest_doc_run,
    remember_uploaded_file,
    render_doc_downloads,
    reset_doc_downloads,
    reset_doc_workflow,
    save_latest_doc_run,
    store_doc_download,
    trigger_rerun,
)
from frontend.utils import OpenAIClient, save_uploaded_file


def render_document_page(
    view_mode: str,
    config: AppConfig,
    feedback: FeedbackUI,
    feedback_manager: FeedbackManager,
    document_controller: DocumentJobController,
) -> None:
    """Render the upload workflow, including job orchestration and saved runs."""

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
            remember_uploaded_file(uploaded, upload_token)
        try:
            uploaded.seek(0)
        except Exception:
            pass

    file_info = st.session_state.get("doc_file_info") or {}
    document_ready = bool(st.session_state.get("doc_file_ready") and file_info)
    if document_ready:
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
            reset_doc_workflow(clear_file=True)
            st.success("Document cleared. Upload a new file to start again.")
            try:
                trigger_rerun()
            except Exception:
                st.stop()

    run_clicked = st.button("Run")
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
            extra_doc_paths, extra_doc_names = _prepare_extra_documents(config.extra_uploads)
            if config.framework == "aladdin":
                llm = CompletionsClient(model=config.llm_model)
            else:
                llm = OpenAIClient(model=config.llm_model)
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
            job = document_controller.schedule(
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
            reset_doc_downloads()
            trigger_rerun()

    job = st.session_state.get("doc_job")
    if job and job.get("status") in {"running", "ready_for_finalize"}:
        document_controller.update(job)
        if job.get("status") == "ready_for_finalize":
            document_controller.finalize(job)
            job = st.session_state.get("doc_job")
        document_controller.render(
            job,
            include_citations=config.include_citations,
            show_live=config.show_live,
        )
        if job.get("status") == "finished":
            document_controller.register_downloads(
                job,
                reset_downloads=reset_doc_downloads,
                store_download=store_doc_download,
            )
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
                    _render_document_feedback_section(job["run_context"], feedback_manager)
                    _render_saved_qa_pairs(job["run_context"], feedback)
        else:
            if not st.session_state.get("suspend_autorefresh", False):
                time.sleep(0.25)
                trigger_rerun()
    elif job and job.get("status") == "finished":
        document_controller.register_downloads(
            job,
            reset_downloads=reset_doc_downloads,
            store_download=store_doc_download,
        )
        document_controller.render(
            job,
            include_citations=config.include_citations,
            show_live=config.show_live,
        )
        st.session_state["doc_processing_state"] = "finished"
        st.session_state["doc_processing_result"] = job.get("run_context")
        if job.get("run_context"):
            _render_document_feedback_section(job["run_context"], feedback_manager)
            _render_saved_qa_pairs(job["run_context"], feedback)
    else:
        latest = st.session_state.get("latest_doc_run")
        if latest:
            _render_document_feedback_section(latest, feedback_manager)
            _render_saved_qa_pairs(latest, feedback)
            with st.container():
                col1, _ = st.columns([1, 6])
                with col1:
                    if st.button(
                        "Clear saved run",
                        key="clear_saved_run",
                        help="Remove the last run from memory and disk.",
                    ):
                        st.session_state.latest_doc_run = None
                        st.session_state.doc_feedback_submitted = False
                        st.session_state["doc_job"] = None
                        reset_doc_workflow(clear_file=False)
                        reset_doc_downloads()
                        try:
                            persist_key = st.session_state.get("current_user_id", st.session_state.get("session_id", ""))
                            clear_latest_doc_run(persist_key)
                        except Exception:
                            pass
                        st.success("Saved run cleared.")
                        trigger_rerun()
        else:
            st.info("Upload a document and click Run to begin.")

    render_doc_downloads()


def _prepare_extra_documents(uploads: List[Any]) -> Tuple[List[str], List[str]]:
    paths: List[str] = []
    names: List[str] = []
    for extra in uploads:
        saved_path = save_uploaded_file(extra)
        paths.append(saved_path)
        names.append(extra.name)
    return paths, names


def _render_document_feedback_section(run_context: Optional[dict], manager: FeedbackManager) -> None:
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
            submitted_form = st.form_submit_button("Submit feedback", use_container_width=True)
            if submitted_form:
                record = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "session_id": st.session_state.get("session_id", str(uuid4())),
                    "user_id": manager.get_current_user(),
                    "feedback_source": "document",
                    "feedback_subject": run_context.get("uploaded_name", "document_run"),
                    "rating": "positive" if rating_choice == "Helpful" else "needs_improvement",
                    "highlights": manager.serialize_list(highlights),
                    "improvements": manager.serialize_list(improvements),
                    "comment": comment.strip(),
                    "question": "",
                    "answer": "",
                    "context_json": manager.format_context(run_context),
                }
                manager.log(record)
                st.session_state.doc_feedback_submitted = True
                st.success("Feedback saved — thank you!")


def _render_saved_qa_pairs(run_context: Optional[dict], feedback: FeedbackUI) -> None:
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
            feedback=feedback,
            card_index=idx,
            question_text=q_text,
            run_context=run_context,
            use_dialog=True,
        )


__all__ = ["render_document_page"]
