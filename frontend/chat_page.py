from __future__ import annotations

import contextlib
import html
import io
import os
from typing import Any, Dict, List

import streamlit as st

from backend import my_module
from backend.components import FeedbackUI
from backend.services import Responder

from backend.answer_composer import CompletionsClient
from frontend.config_panel import AppConfig, FOLLOWUP_DEFAULT_MODEL, MODEL_SHORT_NAMES
from frontend.session_state import trigger_rerun
from frontend.utils import OpenAIClient, save_uploaded_file, select_top_preapproved_answers


def render_chat_page(view_mode: str, config: AppConfig, feedback: FeedbackUI) -> None:
    """Render the conversational experience."""

    extra_docs = [save_uploaded_file(f) for f in config.extra_uploads] if config.extra_uploads else []
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
                trigger_rerun()
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
                    summary_lines: List[str] = []
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
                feedback.render_chat_feedback_form(
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
                        source_name = (cite.get("source_file") or "Unknown").strip() or "Unknown"
                        with sidebar.expander(f"[{lbl}] {source_name}"):
                            snippet_text = (cite.get("text") or "").strip()
                            if snippet_text:
                                st.markdown(snippet_text)
                            else:
                                st.caption("Snippet not available.")
                else:
                    sidebar.caption("No source details returned.")
                feedback.render_chat_feedback_form(
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
                hits_payload: List[Dict[str, Any]] = []
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
                    summary_lines: List[str] = []
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
                    msg_payload = {
                        "role": "assistant",
                        "content": "**Closest pre-approved answers**",
                        "hits": hits_to_show,
                    }
                    answer_summary = msg_payload.get("content", "")
                    if summary_lines:
                        answer_summary = f"{answer_summary}\n" + "\n".join(summary_lines)
                    feedback.render_chat_feedback_form(
                        message_index=len(st.session_state.chat_messages),
                        question=prompt,
                        answer=answer_summary.strip(),
                        message_payload=msg_payload,
                    )
                else:
                    empty_message = "No relevant answers found in the approved library."
                    container.info(empty_message)
                    msg_payload = {
                        "role": "assistant",
                        "content": "Closest pre-approved answers",
                        "hits": [],
                        "empty_message": empty_message,
                    }
                    feedback.render_chat_feedback_form(
                        message_index=len(st.session_state.chat_messages),
                        question=prompt,
                        answer=empty_message,
                        message_payload=msg_payload,
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
                intent = my_module._classify_intent(prompt, history)
                follow = my_module._detect_followup(prompt, history) if intent == "follow_up" else []
                buf = io.StringIO() if view_mode == "Developer" else None
                response_model = config.llm_model
                restore_client = None
                call_fn = my_module.gen_answer if intent == "follow_up" else responder.answer
                try:
                    if intent == "follow_up" and view_mode != "Developer":
                        response_model = FOLLOWUP_DEFAULT_MODEL
                        if config.framework == "aladdin":
                            followup_llm = (
                                llm if response_model == config.llm_model else CompletionsClient(model=response_model)
                            )
                        else:
                            followup_llm = (
                                llm if response_model == config.llm_model else OpenAIClient(model=response_model)
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
                feedback.render_chat_feedback_form(
                    message_index=len(st.session_state.chat_messages),
                    question=prompt,
                    answer=text,
                    message_payload=payload,
                )
                msg_payload = {
                    "role": "assistant",
                    "content": text,
                    "citations": citations,
                    "model": response_model,
                }
        st.session_state.chat_messages.append(msg_payload)
        history.append(prompt)
        st.session_state.question_history = history


__all__ = ["render_chat_page"]
