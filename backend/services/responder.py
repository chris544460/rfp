from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..qa_core import answer_question, collect_relevant_snippets

ProgressFn = Optional[Callable[[str], None]]
BatchProgressFn = Optional[Callable[[int, str], None]]


class Responder:
    """High-level wrapper around the RFP answer generation pipeline."""

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
    ) -> None:
        self.llm = llm_client
        self.search_mode = search_mode
        self.fund = fund or ""
        self.k = k
        self.length = length
        self.approx_words = approx_words
        self.min_confidence = min_confidence
        self.include_citations = include_citations
        self.extra_docs = extra_docs or []

    def with_updates(self, **overrides: Any) -> "Responder":
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
        )

    def answer(
        self,
        question: str,
        *,
        include_citations: Optional[bool] = None,
        progress: ProgressFn = None,
    ) -> Dict[str, Any]:
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
        normalized: List[Tuple[int, str, Dict[str, Any]]] = []
        for idx, item in enumerate(questions):
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
        )

    def _package_answer(
        self,
        question_text: str,
        meta: Dict[str, Any],
        ans: str,
        comments: List[Tuple[str, str, str, float, str]],
        include_citations: Optional[bool],
    ) -> Dict[str, Any]:
        formatted = self._format_answer(ans, comments, include_citations)
        payload = dict(meta)
        payload["question"] = payload.get("question") or question_text
        payload["answer"] = formatted["text"]
        payload["citations"] = formatted["citations"]
        include = self.include_citations if include_citations is None else include_citations
        payload["raw_comments"] = comments if include else []
        return payload

    def _format_answer(
        self,
        ans: str,
        comments: List[Tuple[str, str, str, float, str]],
        include_citations: Optional[bool],
    ) -> Dict[str, Any]:
        include = self.include_citations if include_citations is None else include_citations
        text = ans or ""
        citations: Dict[str, Dict[str, Any]] = {}
        if not include:
            text = re.sub(r"\[\d+\]", "", text)
            comments = []
        else:
            for lbl, src, snippet, score, date_str in comments:
                key = str(lbl or len(citations) + 1)
                citations[key] = {
                    "source_file": Path(src).name if src else "Unknown",  # type: ignore[name-defined]
                    "source_path": src,
                    "text": snippet,
                    "score": score,
                    "date": date_str,
                }
        return {"text": text, "citations": citations}
