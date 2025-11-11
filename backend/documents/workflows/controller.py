from __future__ import annotations

"""
Streamlit controller orchestrating long-running document answering jobs.

This module bridges the UI layer (progress spinners, downloads) with the
answering pipeline (`Responder`, `DocumentFiller`, structured extraction).
"""

import json
import os
import re
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import streamlit as st

from backend.ui.components import FeedbackUI, create_live_placeholder, render_live_answer
from backend.answering import DocumentFiller, DOCUMENT_FILLER_IMPORT_ERROR


def _resolve_concurrency(value: Optional[int]) -> int:
    """Pick a sensible worker pool size, honouring CLI env overrides when present."""
    env = os.getenv("CLI_STREAMLIT_CONCURRENCY")
    resolved = value
    if resolved is None and env:
        try:
            resolved = int(env)
        except ValueError:
            print(f"[WARN] Invalid CLI_STREAMLIT_CONCURRENCY '{env}'; falling back to default")
    if resolved is None:
        # Pick a sensible default for local laptops: bound by CPU count but
        # capped to avoid overwhelming rate-limited backends.
        cpu_default = max(1, (os.cpu_count() or 4))
        resolved = min(8, max(2, cpu_default))
    return max(1, resolved)


class DocumentJobController:
    """Coordinates background document answering work and UI rendering."""

    def __init__(self, feedback: FeedbackUI) -> None:
        self._feedback = feedback

    # ── Job lifecycle -----------------------------------------------------

    def schedule(
        self,
        *,
        config: Dict[str, Any],
        responder,
        extractor,
    ) -> Dict[str, Any]:
        input_path = config["input_path"]
        suffix = config["suffix"]
        include_citations = config["include_citations"]
        extra_doc_names = config.get("extra_doc_names", [])

        # Central job record consumed by both the UI loop and finalize step.
        # It captures worker futures, intermediate answers, and download info.
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
            "include_citations": include_citations,
        }

        # Route to the correct workflow: Excel slots, docx slot injection, or
        # plain Q/A summary depending on file type and user toggles.
        if suffix in {".xlsx", ".xls"}:
            job.update(self._schedule_excel(config, responder, extractor))
        elif suffix == ".docx" and not config["docx_as_text"]:
            job.update(self._schedule_docx_slots(config, responder, extractor))
        else:
            job.update(self._schedule_summary(config, responder, extractor))

        return job

    def update(self, job: Dict[str, Any]) -> None:
        if job.get("status") != "running":
            return
        future_info: Dict[Any, Dict[str, Any]] = job.get("future_info", {})
        answers: List[Optional[Dict[str, Any]]] = job.get("answers", [])
        changed = False

        for future in list(future_info.keys()):
            info = future_info[future]
            if future.done():
                idx = info["index"]
                # Futures complete out-of-order; only record the first terminal
                # result for each slot/question.
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
                # Consumers treat "ready_for_finalize" as a cue to generate
                # output bundles (downloads, docx injection, etc.).
                job["status"] = "ready_for_finalize"

    def finalize(self, job: Dict[str, Any]) -> None:
        if job.get("status") not in {"ready_for_finalize", "running"}:
            return

        config = job["config"]
        include_citations = job.get("include_citations", True)
        answers: List[Optional[Dict[str, Any]]] = job.get("answers", [])
        questions_text: List[str] = job.get("questions_text", [])

        if DocumentFiller is None:
            missing = DOCUMENT_FILLER_IMPORT_ERROR
            missing_name = getattr(missing, "name", None) if missing else None
            print(
                "[DocumentJobController] DocumentFiller import failed; optional dependencies"
                " (python-docx/openpyxl) likely missing.",
                "missing_module=",
                repr(missing_name) if missing_name else repr(missing),
            )
            print(
                "[DocumentJobController] job summary:",
                f"mode={job.get('mode')}",
                f"suffix={config.get('suffix')}",
                f"framework={config.get('framework')}",
                f"extra_docs={job.get('extra_doc_names')}",
            )
            st.error(
                "Document filling is unavailable because optional dependencies failed to "
                "import."
            )
            st.info(
                "Ensure python-docx, openpyxl, and related document packages are installed."
            )
            st.info(
                "Also confirm optional env vars (e.g. aladdin_studio_api_key, "
                "defaultWebServer) are configured."
            )
            if missing is not None:
                st.caption(f"Import error detail: {missing!r}")
            return

        try:
            filler = DocumentFiller()
        except Exception as exc:  # pragma: no cover - depends on runtime environment
            print(
                "[DocumentJobController] DocumentFiller initialization failed:",
                repr(exc),
            )
            print(
                "[DocumentJobController] job summary:",
                f"mode={job.get('mode')}",
                f"suffix={config.get('suffix')}",
                f"input_path={config.get('input_path')}",
                f"extra_docs={job.get('extra_doc_names')}",
            )
            traceback.print_exc()
            st.error(
                "Document filling is unavailable because DocumentFiller could not initialize."
            )
            st.info(
                "This most often means dependencies or environment variables (e.g. "
                "aladdin_studio_api_key, defaultWebServer, aladdin_user, aladdin_passwd) "
                "are missing."
            )
            st.caption(f"DocumentFiller error: {exc}")
            return
        mode = job.get("mode")

        if mode == "excel":
            schema = job.get("schema") or []
            qa_results = []
            for idx in range(len(answers)):
                entry = answers[idx]
                question_text = questions_text[idx] if idx < len(questions_text) else ""
                question_meta = self._question_entry(job, idx)
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
                        "question_meta": question_meta,
                    }
                )
            # build_excel_bundle handles temp files + download metadata for the UI.
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
            # Capture a log of generated answers in the Responsive upload format so operators
            # can download and sync it manually even if we do not POST to the API yet.
            responsive_export = self._build_responsive_export(job, qa_results, config)
            if responsive_export:
                bundle.setdefault("downloads", []).append(responsive_export["download"])
                run_context["responsive_export"] = responsive_export["metadata"]
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
                        "question_meta": slot if isinstance(slot, dict) else {},
                    }
                )
            # For docx we keep both answered file and metadata about skipped slots.
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
            # Excel schema metadata often includes alternate questions/tags; include those
            # details when building the Responsive-style payload.
            responsive_export = self._build_responsive_export(job, qa_results, config)
            if responsive_export:
                bundle.setdefault("downloads", []).append(responsive_export["download"])
                run_context["responsive_export"] = responsive_export["metadata"]
        else:
            qa_results = []
            total = len(questions_text)
            for idx in range(total):
                entry = answers[idx] if idx < len(answers) else None
                question_text = questions_text[idx] if idx < len(questions_text) else f"Question {idx + 1}"
                question_meta = self._question_entry(job, idx)
                if entry is None:
                    storage = {"text": "No answer generated.", "citations": {}}
                    comments = []
                else:
                    storage = entry["storage_answer"]
                    comments = entry.get("comments", [])
                qa_results.append(
                    {
                        "question": question_text,
                        "answer": storage.get("text", ""),
                        "citations": storage.get("citations", {}),
                        "raw_comments": comments,
                        "question_meta": question_meta,
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
            # Summary mode has less structure, but we still want a portable Q/A log downstream.
            responsive_export = self._build_responsive_export(job, qa_results, config)
            if responsive_export:
                bundle.setdefault("downloads", []).append(responsive_export["download"])
                run_context["responsive_export"] = responsive_export["metadata"]

        job["downloads"] = bundle.get("downloads", [])
        job["run_context"] = run_context
        job["status"] = "finished"
        job["completed"] = len([entry for entry in answers if entry is not None])

    def register_downloads(
        self,
        job: Dict[str, Any],
        *,
        reset_downloads: Callable[[], None],
        store_download: Callable[..., None],
    ) -> None:
        if not job or job.get("downloads_registered"):
            return
        reset_downloads()
        for item in job.get("downloads", []):
            store_download(
                item.get("key", f"download_{id(item)}"),
                label=item.get("label", "Download file"),
                data=item.get("data", b""),
                file_name=item.get("file_name", "output"),
                mime=item.get("mime"),
                order=item.get("order", 0),
            )
        job["downloads_registered"] = True

    def render(self, job: Dict[str, Any], *, include_citations: bool, show_live: bool) -> None:
        if not job:
            return

        answers: List[Optional[Dict[str, Any]]] = job.get("answers", [])
        questions_text: List[str] = job.get("questions_text", [])
        total = len(answers)
        if total == 0:
            st.info("No questions detected for this document.")
            return
        completed = job.get("completed", sum(1 for entry in answers if entry is not None))

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

        # Each QA card gets its own placeholder so we can stream answers as they
        # complete without rerendering the entire list.
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
                feedback=self._feedback,
                card_index=idx,
                question_text=question_text,
                run_context=run_context,
                use_dialog=True,
            )

    def _build_responsive_export(
        self,
        job: Dict[str, Any],
        qa_results: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Generate a Responsive-compatible JSON payload plus download metadata.

        The resulting list mirrors the POST /answer-lib/add contract:
        ``[{question, alternateQuestions, answers[{key,value,isPrimary,languageCode}], tags}]``.
        We only emit entries that have both question and answer text.
        """
        if not qa_results:
            return None

        question_meta = job.get("questions") or []
        default_key = (config.get("responsive_answer_key") or "Answer").strip() or "Answer"
        default_language = (config.get("responsive_language") or "en").strip() or "en"
        default_tags = self._normalize_tag_list(config.get("responsive_tags"))

        payload: List[Dict[str, Any]] = []
        for idx, qa in enumerate(qa_results):
            question_text = (qa.get("question") or "").strip()
            answer_text = (qa.get("answer") or "").strip()
            if not question_text or not answer_text:
                # Responsive rejects empty fields; omit incomplete records.
                continue
            # Prefer the per-question metadata captured earlier (Excel schema entry,
            # DOCX slot descriptor, etc.) so we can reuse alternate phrasing/tag info.
            meta = {}
            if isinstance(qa.get("question_meta"), dict):
                meta = qa["question_meta"]
            elif 0 <= idx < len(question_meta) and isinstance(question_meta[idx], dict):
                meta = question_meta[idx]
            alternate = self._extract_alternate_questions(meta)
            tags = self._resolve_tags(meta, default_tags)
            answer_key = self._resolve_answer_key(qa, meta, default_key)
            language_code = self._resolve_language_code(qa, meta, default_language)

            # Mirror POST /answer-lib/add: {question, alternateQuestions, answers[], tags}.
            payload.append(
                {
                    "question": question_text,
                    "alternateQuestions": alternate,
                    "answers": [
                        {
                            "key": answer_key,
                            "value": answer_text,
                            "isPrimary": True,
                            "languageCode": language_code,
                        }
                    ],
                    "tags": tags,
                }
            )

        if not payload:
            # Nothing to download when no valid Q/A pairs were produced for this run.
            return None

        # Streamlit download buttons expect an in-memory bytes payload; write the JSON to
        # a temp file first so we can read it back and immediately clean up the artifact.
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        with open(tmp.name, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        with open(tmp.name, "rb") as handle:
            data = handle.read()
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

        stem = os.path.splitext(config.get("file_name") or "answer_library")[0] or "answer_library"
        download = {
            "key": "responsive_answer_library",
            "label": "Download Responsive answer library JSON",
            "data": data,
            "file_name": f"{stem}_responsive.json",
            "mime": "application/json",
            "order": 90,
        }
        metadata = {
            "record_count": len(payload),
            "default_tags": default_tags,
            "default_language": default_language,
        }
        return {"download": download, "metadata": metadata}

    @staticmethod
    def _question_entry(job: Dict[str, Any], idx: int) -> Dict[str, Any]:
        """Return the raw question metadata for a given index if available."""
        questions = job.get("questions") or []
        if 0 <= idx < len(questions):
            entry = questions[idx]
            if isinstance(entry, dict):
                return entry
        return {}

    def _extract_alternate_questions(self, meta: Dict[str, Any]) -> List[str]:
        """Normalize alternate question strings from any available metadata."""
        candidates = meta.get("alternateQuestions") or meta.get("alternate_questions")
        schema_entry = meta.get("schema_entry")
        if not candidates and isinstance(schema_entry, dict):
            candidates = schema_entry.get("alternate_questions")
        return [str(item).strip() for item in candidates or [] if str(item).strip()]

    def _resolve_tags(self, meta: Dict[str, Any], default_tags: List[str]) -> List[str]:
        """Prefer explicit tag metadata and fall back to the workflow defaults."""
        schema_entry = meta.get("schema_entry")
        tag_sources = [
            meta.get("tags"),
            meta.get("tag_list"),
            meta.get("tag"),
            schema_entry.get("tags") if isinstance(schema_entry, dict) else None,
        ]
        for source in tag_sources:
            normalized = self._normalize_tag_list(source)
            if normalized:
                return normalized
        return default_tags

    @staticmethod
    def _resolve_answer_key(
        qa: Dict[str, Any],
        meta: Dict[str, Any],
        default_key: str,
    ) -> str:
        """Pick the first non-empty answer key while honouring per-question overrides."""
        candidate = qa.get("answer_key") or meta.get("answer_key")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        return default_key

    @staticmethod
    def _resolve_language_code(
        qa: Dict[str, Any],
        meta: Dict[str, Any],
        default_language: str,
    ) -> str:
        """Return a language code from the QA payload/metadata or fall back to default."""
        for candidate in (
            qa.get("language_code"),
            meta.get("language_code"),
            meta.get("language"),
        ):
            if isinstance(candidate, str):
                cleaned = candidate.strip()
                if cleaned:
                    return cleaned
        return default_language

    @staticmethod
    def _normalize_tag_list(value) -> List[str]:
        """Convert comma/semicolon delimited strings or iterables into a tag list."""
        if not value:
            return []
        if isinstance(value, str):
            items = re.split(r"[;,]", value)
        else:
            items = value
        normalized: List[str] = []
        for item in items:
            text = str(item).strip()
            if text:
                normalized.append(text)
        return normalized

    # ── Internal helpers --------------------------------------------------

    def _schedule_excel(self, config, responder, extractor):
        with open(config["input_path"], "rb") as fh:
            questions = extractor.extract(fh)
        schema = extractor.last_details.get("schema") or []
        questions_text = [(entry.get("question") or "").strip() for entry in questions]
        total = len(questions_text)
        job = {
            "mode": "excel",
            "questions": questions,
            "questions_text": questions_text,
            "schema": schema,
            "answers": [None] * total,
        }
        if total > 0:
            # Allow operators to tune concurrency via env var while keeping a
            # hard upper bound so we respect API rate limits.
            worker_limit = _resolve_concurrency(None) or total
            worker_limit = max(1, min(worker_limit, total))
            executor = ThreadPoolExecutor(max_workers=worker_limit)
            job["executor"] = executor
            futures = []
            future_info = {}
            for idx, question_text in enumerate(questions_text):
                future = executor.submit(_run_excel_task, responder, question_text)
                futures.append(future)
                future_info[future] = {"index": idx, "question_text": question_text}
            job["futures"] = futures
            job["future_info"] = future_info
        return job

    def _schedule_docx_slots(self, config, responder, extractor):
        with open(config["input_path"], "rb") as fh:
            questions = extractor.extract(fh)
        details = extractor.last_details
        slots_payload = details.get("slots_payload") or {}
        slot_list = [entry.get("slot") for entry in questions]
        slot_list = [slot for slot in slot_list if slot is not None]
        questions_text = [(slot.get("question_text") or "").strip() for slot in slot_list]
        total = len(slot_list)
        job = {
            "mode": "docx_slots",
            "questions": slot_list,
            "questions_text": questions_text,
            "slots_payload": slots_payload,
            "skipped_slots": details.get("skipped_slots") or [],
            "heuristic_skips": details.get("heuristic_skips") or [],
            "answers": [None] * total,
        }
        if total > 0:
            # Docx slot answering can be slow; keep concurrency modest to avoid
            # saturating the language model or file IO.
            worker_limit = _resolve_concurrency(None) or total
            worker_limit = max(1, min(worker_limit, total))
            executor = ThreadPoolExecutor(max_workers=worker_limit)
            job["executor"] = executor
            futures = []
            future_info = {}
            for idx, slot in enumerate(slot_list):
                future = executor.submit(_run_docx_task, responder, slot)
                futures.append(future)
                future_info[future] = {"index": idx, "slot_id": slot.get("id")}
            job["futures"] = futures
            job["future_info"] = future_info
        return job

    def _schedule_summary(self, config, responder, extractor):
        treat_docx_as_text = config["suffix"] == ".docx" and config["docx_as_text"]
        with open(config["input_path"], "rb") as fh:
            questions = extractor.extract(fh, treat_docx_as_text=treat_docx_as_text)
        questions_text = [(entry.get("question") or "").strip() for entry in questions]
        total = len(questions_text)
        job = {
            "mode": "document_summary",
            "questions": questions,
            "questions_text": questions_text,
            "answers": [None] * total,
            "treat_docx_as_text": treat_docx_as_text,
        }
        if total > 0:
            # Summary mode is cheaper, so we can lean on the same concurrency
            # guard used elsewhere to prevent runaway thread counts.
            worker_limit = _resolve_concurrency(None) or total
            worker_limit = max(1, min(worker_limit, total))
            executor = ThreadPoolExecutor(max_workers=worker_limit)
            job["executor"] = executor
            futures = []
            future_info = {}
            for idx, question_text in enumerate(questions_text):
                future = executor.submit(_run_summary_task, responder, question_text)
                futures.append(future)
                future_info[future] = {"index": idx, "question_text": question_text}
            job["futures"] = futures
            job["future_info"] = future_info
        return job


# ── Task helpers -----------------------------------------------------------

def _run_excel_task(responder, question_text: str) -> Dict[str, Any]:
    result = responder.answer(question_text)
    # Excel mode keeps the original responder payload for live display but
    # stores a flattened structure for the workbook writer.
    return {
        "question": question_text,
        "answer_payload": result,
        "storage_answer": {
            "text": result["text"],
            "citations": result["citations"],
        },
        "comments": result.get("raw_comments", []),
    }


def _run_docx_task(responder, slot: Dict[str, Any]) -> Dict[str, Any]:
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


def _run_summary_task(responder, question_text: str) -> Dict[str, Any]:
    result = responder.answer(question_text)
    # Summary bundle mirrors the Excel storage shape so downstream exporters
    # can reuse serialization helpers.
    return {
        "question": question_text,
        "answer_payload": result,
        "storage_answer": {
            "text": result["text"],
            "citations": result["citations"],
        },
        "comments": result.get("raw_comments", []),
    }


def _is_table_slot(slot: dict) -> bool:
    locator = slot.get("answer_locator") or {}
    return isinstance(locator, dict) and locator.get("type") == "table_cell"


def _sanitize_table_answer(answer) -> str:
    if isinstance(answer, dict):
        text = str(answer.get("text", ""))
    else:
        text = str(answer or "")
    text = re.sub(r"\[\d+\]", "", text)

    # The docx writer struggles with raw markdown tables. Flatten them into a
    # short sentence so the final document reads naturally.
    def _collapse_table_like(line: str) -> str:
        working = line.replace("\t", " | ").strip()
        if not working:
            return ""
        if set(working) <= {"|", ":", "-", " ", "+", "="}:
            return ""
        if "|" in working:
            segments = [seg.strip(" -") for seg in working.strip("|").split("|")]
            segments = [seg for seg in segments if seg and set(seg) != {"-"}]
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
    if not prose.endswith((".", "!", "?")):
        prose += "."
    return prose


__all__ = ["DocumentJobController"]

# To dry-run the controller outside Streamlit, wire up fake responder/extractor:
# if __name__ == "__main__":
#     from types import SimpleNamespace
#     controller = DocumentJobController(feedback=SimpleNamespace(info=lambda *a, **k: None))
#     # Populate a minimal config dict then call controller.schedule(...)
