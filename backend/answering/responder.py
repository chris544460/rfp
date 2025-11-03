from __future__ import annotations

"""
High-level orchestration for the RFP answering pipeline.

Responder glues together vector retrieval (`backend.retrieval`) and the QA
engine so Streamlit views, API callers, and background jobs can share the
same batching and formatting logic.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from backend.retrieval.stacks import RetrievalStack, get_stack
from backend.prompts import get_developer_prompt

from .qa_engine import answer_question, collect_relevant_snippets

# Progress callbacks let Streamlit surface incremental status updates.
ProgressFn = Optional[Callable[[str], None]]
# Batch progress callbacks take the index of the question plus the message.
BatchProgressFn = Optional[Callable[[int, str], None]]


class Responder:
    """Prepare questions, invoke the QA engine, and format answers for downstream consumers."""

    def __init__(
        self,
        *,
        llm_client,
        search_mode: str = "both",
        fund: Optional[str] = None,
        k: int = 20,
        length: Optional[str] = None,
        approx_words: Optional[int] = None,
        min_confidence: float = 0.0,
        include_citations: bool = True,
        extra_docs: Optional[List[str]] = None,
        retrieval_stack: Optional[Union[str, RetrievalStack]] = None,
        retrieval_backend: Optional[Union[str, RetrievalStack]] = None,
        developer_prompt_team: Optional[str] = None,
    ) -> None:
        if retrieval_stack is None and retrieval_backend is not None:
            retrieval_stack = retrieval_backend
        self.llm = llm_client
        self.search_mode = search_mode
        self.fund = fund or ""
        self.k = k
        self.length = length
        self.approx_words = approx_words
        self.min_confidence = min_confidence
        self.include_citations = include_citations
        self.extra_docs = extra_docs or []
        self._retrieval_stack_param: Optional[Union[str, RetrievalStack]] = retrieval_stack
        if isinstance(retrieval_stack, str):
            self.retrieval_stack: Optional[RetrievalStack] = get_stack(retrieval_stack)
        else:
            self.retrieval_stack = retrieval_stack
        self.developer_prompt_team = developer_prompt_team or os.getenv("RFP_DEVELOPER_PROMPT_TEAM")
        self.developer_prompt = get_developer_prompt(self.developer_prompt_team)

    def with_updates(self, **overrides: Any) -> "Responder":
        """Return a new `Responder` using the current defaults plus any overrides."""
        # Build a fresh Responder so Streamlit can tweak per-request knobs
        # without mutating the shared instance held in session state.
        legacy_stack = overrides.get("retrieval_stack")
        if legacy_stack is None and "retrieval_backend" in overrides:
            legacy_stack = overrides["retrieval_backend"]

        params = {
            "llm_client": overrides.get("llm_client", self.llm),
            "search_mode": overrides.get("search_mode", self.search_mode),
            "fund": overrides.get("fund", self.fund),
            "k": overrides.get("k", self.k),
            "length": overrides.get("length", self.length),
            "approx_words": overrides.get("approx_words", self.approx_words),
            "min_confidence": overrides.get("min_confidence", self.min_confidence),
            "include_citations": overrides.get("include_citations", self.include_citations),
            "extra_docs": overrides.get("extra_docs", list(self.extra_docs)),
            "retrieval_stack": overrides.get(
                "retrieval_stack",
                legacy_stack
                if legacy_stack is not None
                else self._retrieval_stack_param
                if self._retrieval_stack_param is not None
                else self.retrieval_stack,
            ),
            "developer_prompt_team": overrides.get(
                "developer_prompt_team", self.developer_prompt_team
            ),
        }
        return Responder(**params)

    def get_context(
        self,
        question: str,
        *,
        progress: ProgressFn = None,
        diagnostics: Optional[List[Dict[str, Any]]] = None,
        include_vectors: bool = False,
    ) -> List[Tuple[str, str, str, float, str]]:
        """Return retrieval snippets so callers can preview or debug context selection."""
        return collect_relevant_snippets(
            question,
            self.search_mode,
            self.fund or None,
            self.k,
            self.min_confidence,
            self.llm,
            extra_docs=list(self.extra_docs),
            progress=progress,
            diagnostics=diagnostics,
            include_vectors=include_vectors,
            retrieval_stack=self.retrieval_stack,
        )

    def answer(
        self,
        question: str,
        *,
        include_citations: Optional[bool] = None,
        progress: ProgressFn = None,
    ) -> Dict[str, Any]:
        """Answer a single question and wrap the result with citation metadata."""
        ans, comments = self._answer_raw(question, progress=progress)
        include = self.include_citations if include_citations is None else include_citations
        formatted = dict(self._format_answer(ans, comments, include_citations))
        formatted["raw_comments"] = comments if include else []
        return formatted

    def answer_batch(
        self,
        questions: Sequence[Any],
        *,
        include_citations: Optional[bool] = None,
        max_workers: int = 1,
        progress: BatchProgressFn = None,
    ) -> List[Dict[str, Any]]:
        """Answer many questions, optionally parallelising work with a thread pool."""
        normalized: List[Tuple[int, str, Dict[str, Any]]] = []
        for idx, item in enumerate(questions):
            # The Streamlit UI can pass bare strings or rich dict metadata; we
            # normalize here so batching logic can treat everything uniformly.
            if isinstance(item, str):
                normalized.append((idx, item, {}))
            elif isinstance(item, dict):
                text = str(item.get("question") or "").strip()
                normalized.append((idx, text, item))
            else:
                normalized.append((idx, str(item), {}))

        answers: List[Optional[Dict[str, Any]]] = [None] * len(normalized)

        def _progress_wrapper(idx: int) -> ProgressFn:
            if progress is None:
                return None

            def _inner(message: str) -> None:
                progress(idx, message)

            return _inner

        def _run_single(idx: int, q_text: str) -> Tuple[str, List[Tuple[str, str, str, float, str]]]:
            return self._answer_raw(q_text, progress=_progress_wrapper(idx))

        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                # The underlying QA engine can issue blocking LLM calls, so threads help hide latency.
                # Fan out questions to the worker pool but retain the original
                # ordering so downstream consumers can match answers by index.
                future_map = {
                    pool.submit(_run_single, idx, q_text): (idx, q_text, meta)
                    for idx, q_text, meta in normalized
                }
                for fut in as_completed(future_map):
                    idx, q_text, meta = future_map[fut]
                    ans, comments = fut.result()
                    answers[idx] = self._package_answer(q_text, meta, ans, comments, include_citations)
        else:
            for idx, q_text, meta in normalized:
                ans, comments = _run_single(idx, q_text)
                answers[idx] = self._package_answer(q_text, meta, ans, comments, include_citations)

        return [ans for ans in answers if ans is not None]

    def _answer_raw(
        self,
        question: str,
        *,
        progress: ProgressFn = None,
    ) -> Tuple[str, List[Tuple[str, str, str, float, str]]]:
        """Call `qa_engine.answer_question` and return both answer text and raw snippets."""
        return answer_question(
            question,
            self.search_mode,
            self.fund or None,
            self.k,
            self.length,
            self.approx_words,
            self.min_confidence,
            self.llm,
            extra_docs=list(self.extra_docs),
            progress=progress,
            retrieval_stack=self.retrieval_stack,
            developer_prompt=self.developer_prompt,
        )

    def _package_answer(
        self,
        question_text: str,
        meta: Dict[str, Any],
        ans: str,
        comments: List[Tuple[str, str, str, float, str]],
        include_citations: Optional[bool],
    ) -> Dict[str, Any]:
        """Merge the raw answer with original metadata for compatibility with exports."""
        formatted = self._format_answer(ans, comments, include_citations)
        payload = dict(meta)
        payload["question"] = payload.get("question") or question_text
        payload["answer"] = formatted["text"]
        payload["citations"] = formatted["citations"]
        # Keep the raw snippet tuples only if the caller asked for citations;
        # otherwise we avoid leaking potentially sensitive context strings.
        include = self.include_citations if include_citations is None else include_citations
        payload["raw_comments"] = comments if include else []
        return payload

    def _format_answer(
        self,
        ans: str,
        comments: List[Tuple[str, str, str, float, str]],
        include_citations: Optional[bool],
    ) -> Dict[str, Any]:
        """Convert raw answer output into repo-wide shape expected by UI/export layers."""
        include = self.include_citations if include_citations is None else include_citations
        text = ans or ""
        citations: Dict[str, Dict[str, Any]] = {}
        if not include:
            # Consumers like Excel/Word exports rely on bracket markers; strip
            # them when callers explicitly disable citations.
            text = re.sub(r"\[\d+\]", "", text)
            comments = []
        else:
            for lbl, src, snippet, score, date_str in comments:
                key = str(lbl or len(citations) + 1)
                citations[key] = {
                    "source_file": Path(src).name if src else "Unknown",  # type: ignore[name-defined]
                    "source_path": src,
                    # These keys are consumed by the document filler and Excel exporters.
                    "text": snippet,
                    "score": score,
                    "date": date_str,
                }
        return {"text": text, "citations": citations}


# The block below can be uncommented when you want to run this module directly.
# It mirrors how the Streamlit UI constructs a responder but avoids importing
# the full frontend stack. Keep the code commented-out so importing this module
# remains side-effect free.
#
# if __name__ == "__main__":
#     import os
#     from backend.llm.completions_client import CompletionsClient
#
#     # Configure the client using the same environment variables as the web app.
#     client = CompletionsClient(model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
#     responder = Responder(llm_client=client, search_mode="both", k=6)
#     sample_question = "Summarize the investment strategy for Fund X."
#     payload = responder.answer(sample_question, include_citations=True)
#     print("Answer:", payload["text"])
#     print("Citations:", payload["citations"])
