"""Lightweight answering helper with the same signature used by legacy scripts."""

from __future__ import annotations

import os
from typing import Iterable, List, Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.retrieval.stacks.azure.stack import AzureSearchStack

from backend.answering.qa_engine import answer_question
try:  # pragma: no cover - optional dependency
    from backend.llm.completions_client import CompletionsClient
except Exception:  # pragma: no cover - fallback to stub
    CompletionsClient = None  # type: ignore

from scripts.responder_smoke import DummyLLM


def _context_rows(hits: Iterable[Dict[str, Any]]) -> List[tuple]:
    rows: List[tuple] = []
    for idx, hit in enumerate(hits, start=1):
        meta = hit.get("meta") or {}
        rows.append(
            (
                str(idx),
                meta.get("source", "unknown"),
                hit.get("text", ""),
                float(hit.get("cosine", 0.0)),
                meta.get("date", ""),
            )
        )
    return rows


def _resolve_llm() -> Any:
    if CompletionsClient is None:
        return DummyLLM()
    if os.getenv("aladdin_studio_api_key") and os.getenv("aladdin_user"):
        model = os.getenv("RFP_LLM_MODEL", "o3-2025-04-16_research")
        return CompletionsClient(model=model)
    return DummyLLM()


def generate_answers(
    *,
    questions: List[str],
    all_context: List[List[Dict[str, Any]]],
    fund_filter: Optional[Iterable[str]] = None,
    k: int = 6,
    min_confidence: float = 0.0,
) -> List[Dict[str, Any]]:
    """Return answer payloads matching the legacy lite-answering module."""
    llm = _resolve_llm()
    fund = next(iter(fund_filter), None) if fund_filter else None
    outputs: List[Dict[str, Any]] = []

    for question, context_hits in zip(questions, all_context):
        rows = _context_rows(context_hits)
        answer_text, comments = answer_question(
            q=question,
            mode="both",
            fund=fund,
            k=k,
            length=None,
            approx_words=None,
            min_confidence=min_confidence,
            llm=llm,
            retrieval_stack=None,
            context_rows=rows if rows else None,
        )
        citations = {
            str(label): {
                "text": snippet,
                "source_file": source,
                "score": score,
                "date": date_str,
            }
            for label, source, snippet, score, date_str in comments
        }
        outputs.append({"text": answer_text, "citations": citations})

    return outputs
