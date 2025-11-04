#!/usr/bin/env python3
"""Convenience CLI to exercise the Responder end-to-end pipeline."""

from __future__ import annotations

import argparse
import os
import sys
from textwrap import shorten
from typing import Iterable, List, Optional, Sequence, Tuple

from backend.utils.dotenv import load_dotenv
from scripts.retrieval_smoke_utils import load_azure_stack, load_faiss_stack

# Default to quieter logs unless callers set RFP_QA_DEBUG explicitly.
os.environ.setdefault("RFP_QA_DEBUG", os.getenv("RFP_QA_DEBUG", "0"))

DEFAULT_RFP_MODEL = "o3-2025-04-16_research"
os.environ.setdefault("RFP_LLM_MODEL", os.getenv("RFP_LLM_MODEL", DEFAULT_RFP_MODEL))


class DummyLLM:
    """Minimal stub that pretends to be a completions client."""

    def __init__(self, template: str | None = None) -> None:
        if template:
            self._template = template
        else:
            self._template = "Stub answer for '{question}' referencing the first snippet. [1]"

    def get_completion(
        self,
        prompt: str = "",
        *,
        messages: Optional[Sequence[dict]] = None,
    ) -> Tuple[str, dict]:
        question = _extract_question(messages, prompt)
        answer = self._template.format(question=question or "smoke test question")
        return answer, {"stub": True}


def _extract_question(messages: Optional[Sequence[dict]], fallback_prompt: str) -> str:
    """Pull the question text from the final prompt block when available."""
    payload = ""
    if messages:
        payload = messages[-1].get("prompt", "")
    payload = payload or fallback_prompt
    marker = "Question:"
    if marker in payload:
        return payload.split(marker, 1)[1].strip().splitlines()[0].strip()
    return payload.strip().splitlines()[0].strip()


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Responder orchestration using either a dummy or live LLM.",
    )
    parser.add_argument("question", help="Question to answer.")
    parser.add_argument(
        "--stack",
        help="Retrieval stack name to use (e.g., 'faiss' or 'azure'). Defaults to the registered default stack.",
    )
    parser.add_argument(
        "--search-mode",
        default="both",
        choices=("both", "answer", "question", "blend"),
        help="Responder search mode (default: both).",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=6,
        help="Maximum number of snippets to retrieve (default: 6).",
    )
    parser.add_argument("--fund", help="Optional fund tag to filter snippets.")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="Minimum cosine similarity to keep snippets (default: 0.0).",
    )
    parser.add_argument(
        "--length",
        choices=("short", "medium", "long", "auto"),
        help="Preset answer length hint.",
    )
    parser.add_argument(
        "--approx-words",
        type=int,
        help="Explicit word count target (overrides --length when provided).",
    )
    parser.add_argument(
        "--dummy-template",
        help="Override dummy LLM answer template (supports '{question}' placeholder).",
    )
    parser.add_argument(
        "--live-llm",
        dest="live_llm",
        action="store_true",
        help="Use backend.llm.CompletionsClient instead of the stub (requires env credentials).",
    )
    parser.add_argument(
        "--no-live-llm",
        dest="live_llm",
        action="store_false",
        help="Force usage of the dummy LLM stub even when credentials are available.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model ID to pass to CompletionsClient when --live-llm is set (defaults to RFP_LLM_MODEL or o3-2025-04-16_research).",
    )
    parser.add_argument(
        "--include-vectors",
        action="store_true",
        help="Request embeddings within the retrieval context (printed only when --show-context).",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Print the retrieved snippets before generating the answer.",
    )
    parser.add_argument(
        "--show-raw-comments",
        action="store_true",
        help="Print the raw comment tuples returned alongside the answer.",
    )
    parser.add_argument(
        "--no-citations",
        action="store_true",
        help="Strip citation markers and omit citation metadata from the answer payload.",
    )
    parser.add_argument(
        "--list-stacks",
        action="store_true",
        help="List registered retrieval stacks and exit.",
    )
    parser.set_defaults(live_llm=True)
    return parser.parse_args(list(argv) if argv is not None else None)


def _resolve_stack(name: Optional[str]):
    """Return a RetrievalStack instance based on the requested name."""
    from backend.retrieval.stacks import get_stack, list_stacks

    if name:
        try:
            return get_stack(name)
        except KeyError:
            loader = _known_stack_loader(name)
            if loader is None:
                available = ", ".join(list_stacks()) or "none"
                raise RuntimeError(
                    f"Stack '{name}' is not registered. Available stacks: {available}."
                )
            return loader()

    try:
        return get_stack()
    except LookupError:
        for candidate in ("faiss", "azure"):
            loader = _known_stack_loader(candidate)
            if loader:
                try:
                    return loader()
                except RuntimeError:
                    continue
        available = ", ".join(list_stacks()) or "none"
        raise RuntimeError(
            "No default retrieval stack registered and automatic fallbacks failed. "
            f"Available stacks after import: {available}."
        )


def _known_stack_loader(name: str):
    """Return a helper capable of loading the requested built-in stack."""
    lowered = name.lower()
    if lowered == "azure":
        return load_azure_stack
    if lowered == "faiss":
        return load_faiss_stack
    return None


def _print_context(context: List[Tuple[str, str, str, float, str]], include_vectors: bool) -> None:
    if not context:
        print("No context snippets were retrieved.")
        return
    print(f"Retrieved {len(context)} snippet(s):")
    for label, src, snippet, score, date_str in context:
        try:
            score_str = f"{float(score):.3f}"
        except (TypeError, ValueError):
            score_str = str(score)
        snippet_preview = shorten(snippet.replace("\n", " "), width=160, placeholder="...")
        print(f"  {label} {src} score={score_str} date={date_str}")
        print(f"     {snippet_preview}")
        if include_vectors:
            print("     (vector data requested; embeddings are captured in diagnostics but not printed)")


def _print_answer(payload: dict) -> None:
    print("\n=== Answer ===")
    print(payload.get("text") or "")
    print("\n=== Citations ===")
    citations = payload.get("citations") or {}
    if not citations:
        print("No citation metadata returned.")
    else:
        def _sort_key(token: str) -> Tuple[int, str]:
            try:
                return (int(token), token)
            except (TypeError, ValueError):
                return (sys.maxsize, token)

        for idx in sorted(citations, key=_sort_key):
            item = citations.get(idx, {})
            print(
                f"  [{idx}] {item.get('source_file', 'Unknown')} "
                f"(score={item.get('score', 'n/a')} date={item.get('date', 'n/a')})"
            )


def _print_raw_comments(comments: List[Tuple[str, str, str, float, str]]) -> None:
    if not comments:
        print("Raw comments list is empty.")
        return
    print("\n=== Raw Comments ===")
    for lbl, src, snippet, score, date_str in comments:
        try:
            score_str = f"{float(score):.3f}"
        except (TypeError, ValueError):
            score_str = str(score)
        snippet_preview = shorten(snippet.replace("\n", " "), width=160, placeholder="...")
        print(f"  [{lbl}] {src} score={score_str} date={date_str}")
        print(f"     {snippet_preview}")


def _resolve_llm(args: argparse.Namespace):
    if args.live_llm:
        try:
            from backend.llm.completions_client import CompletionsClient
        except ImportError as exc:
            raise RuntimeError(
                f"Failed to import CompletionsClient: {exc}. Ensure backend.llm dependencies are installed."
            ) from exc
        model = args.model or os.getenv("RFP_LLM_MODEL", DEFAULT_RFP_MODEL)
        return CompletionsClient(model=model)
    return DummyLLM(template=args.dummy_template)


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = _parse_args(argv)
    load_dotenv(override=False)

    if args.list_stacks:
        from backend.retrieval.stacks import list_stacks

        names = list_stacks()
        print("Registered stacks:", ", ".join(names) if names else "(none)")
        return

    try:
        retrieval_stack = _resolve_stack(args.stack)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        llm_client = _resolve_llm(args)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    from backend.answering.responder import Responder

    responder = Responder(
        llm_client=llm_client,
        search_mode=args.search_mode,
        fund=args.fund,
        k=args.k,
        length=args.length,
        approx_words=args.approx_words,
        min_confidence=args.min_confidence,
        include_citations=not args.no_citations,
        retrieval_stack=retrieval_stack,
    )

    context = responder.get_context(
        args.question,
        include_vectors=args.include_vectors,
    )
    if args.show_context:
        _print_context(context, include_vectors=args.include_vectors)

    answer_payload = responder.answer(
        args.question,
        include_citations=not args.no_citations,
        context=context,
    )

    _print_answer(answer_payload)

    if args.show_raw_comments:
        raw_comments = answer_payload.get("raw_comments") or []
        _print_raw_comments(raw_comments)


if __name__ == "__main__":
    main()
