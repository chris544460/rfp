#!/usr/bin/env python3
"""Quick smoke tests for retrieval stacks and the Responder pipeline."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from dataclasses import dataclass
from typing import Any, Optional


def _print_header(title: str) -> None:
    line = "=" * len(title)
    print(f"\n{line}\n{title}\n{line}")


def _format_exception(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _dump_hits(hits: list[dict[str, Any]], limit: int) -> None:
    if not hits:
        print("No hits returned.")
        return
    for idx, hit in enumerate(hits[:limit], start=1):
        meta = hit.get("meta") or {}
        source = meta.get("source", "unknown")
        snippet = (hit.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        score = float(hit.get("cosine", 0.0))
        print(f"[{idx:>2}] score={score:.3f} source={source} snippet={snippet}")
    if len(hits) > limit:
        print(f"... {len(hits) - limit} additional hits truncated ...")


def test_faiss(query: str, mode: str, k: int, fund: Optional[str], include_vectors: bool) -> bool:
    _print_header("Testing FAISS retrieval stack")
    try:
        from backend.stacks import FaissRetrievalStack
    except Exception as exc:
        print("Failed to import FAISS stack alias.")
        print(_format_exception(exc))
        return False
    if FaissRetrievalStack is None:
        print("FAISS stack is not available in this environment.")
        return False
    try:
        stack = FaissRetrievalStack()
    except Exception as exc:
        print("Failed to instantiate FaissRetrievalStack.")
        print(_format_exception(exc))
        return False
    try:
        hits = stack.search(
            query,
            mode=mode,
            k=k,
            fund_filter=fund,
            include_vectors=include_vectors,
        )
    except Exception as exc:
        print("Search raised an exception.")
        print(_format_exception(exc))
        return False
    print(f"Returned {len(hits)} hits.")
    _dump_hits(hits, limit=min(5, k))
    return True


def test_azure(query: str, mode: str, k: int, fund: Optional[str], include_vectors: bool) -> bool:
    _print_header("Testing Azure retrieval stack")
    try:
        from backend.stacks import AzureSearchStack
    except Exception as exc:
        print("Failed to import Azure stack alias.")
        print(_format_exception(exc))
        return False
    if AzureSearchStack is None:
        print("Azure stack is not available in this environment.")
        return False
    try:
        stack = AzureSearchStack()
    except Exception as exc:
        print("Failed to instantiate AzureSearchStack.")
        print(_format_exception(exc))
        return False
    try:
        hits = stack.search(
            query,
            mode=mode,
            k=k,
            fund_filter=fund,
            include_vectors=include_vectors,
        )
    except Exception as exc:
        print("Search raised an exception.")
        print(_format_exception(exc))
        return False
    print(f"Returned {len(hits)} hits.")
    _dump_hits(hits, limit=min(5, k))
    return True


class DummyLLM:
    """Minimal LLM stub so Responder can be exercised without external calls."""

    def __init__(self, reply: str) -> None:
        self.reply = reply or "Stubbed response."

    def get_completion(
        self,
        prompt: str = "",
        json_output: bool = False,
        *,
        messages: Optional[list[dict[str, str]]] = None,
    ) -> tuple[str, dict[str, Any]]:
        payload = {
            "json_output": json_output,
            "messages": messages or [],
            "prompt_len": len(prompt),
        }
        return self.reply, payload


@dataclass
class ResponderOptions:
    query: str
    retrieval: str
    include_citations: bool
    answer_text: str


def _build_retrieval_stack(name: str):
    lname = name.lower()
    if lname == "faiss":
        from backend.stacks import FaissRetrievalStack

        if FaissRetrievalStack is None:
            raise RuntimeError("FAISS stack not available.")
        return FaissRetrievalStack()
    if lname == "azure":
        from backend.stacks import AzureSearchStack

        if AzureSearchStack is None:
            raise RuntimeError("Azure stack not available.")
        return AzureSearchStack()
    raise ValueError(f"Unsupported retrieval stack '{name}'.")


def test_responder(opts: ResponderOptions) -> bool:
    _print_header("Testing Responder pipeline")
    try:
        from backend.answering.responder import Responder
    except Exception as exc:
        print("Failed to import Responder.")
        print(_format_exception(exc))
        return False
    try:
        stack = _build_retrieval_stack(opts.retrieval)
    except Exception as exc:
        print("Unable to prepare retrieval stack for Responder.")
        print(_format_exception(exc))
        return False
    llm = DummyLLM(opts.answer_text)
    try:
        responder = Responder(
            llm_client=llm,
            include_citations=opts.include_citations,
            retrieval_stack=stack,
            k=5,
        )
    except Exception as exc:
        print("Responder initialisation failed.")
        print(_format_exception(exc))
        return False
    try:
        answer = responder.answer(opts.query, include_citations=opts.include_citations)
    except Exception as exc:
        print("Responder.answer raised an exception.")
        print(_format_exception(exc))
        return False
    print("Responder returned:")
    formatted = json.dumps(answer, indent=2, ensure_ascii=False)
    print(textwrap.indent(formatted, prefix="  "))
    return True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run smoke tests for retrieval stacks and Responder.",
    )
    parser.add_argument(
        "--component",
        choices=["faiss", "azure", "responder", "all"],
        default="all",
        help="Which component to exercise.",
    )
    parser.add_argument(
        "--query",
        default="What is the latest update?",
        help="Query or question to use for searches.",
    )
    parser.add_argument(
        "--mode",
        default="answer",
        help="Retrieval mode (answer, question, blend, dual).",
    )
    parser.add_argument("--k", type=int, default=3, help="Number of hits to request.")
    parser.add_argument(
        "--fund",
        default=None,
        help="Fund filter passed to the retrieval stack.",
    )
    parser.add_argument(
        "--include-vectors",
        action="store_true",
        help="Return embeddings with retrieval results.",
    )
    parser.add_argument(
        "--responder-stack",
        default="faiss",
        help="Retrieval stack to use for Responder smoke test (faiss or azure).",
    )
    parser.add_argument(
        "--responder-answer",
        default="This is a stubbed answer produced by DummyLLM.",
        help="Answer text emitted by the dummy LLM when exercising Responder.",
    )
    parser.add_argument(
        "--responder-include-citations",
        action="store_true",
        help="Ask Responder to include citations in the response.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    success = True
    if args.component in {"faiss", "all"}:
        success &= test_faiss(args.query, args.mode, args.k, args.fund, args.include_vectors)
    if args.component in {"azure", "all"}:
        success &= test_azure(args.query, args.mode, args.k, args.fund, args.include_vectors)
    if args.component in {"responder", "all"}:
        opts = ResponderOptions(
            query=args.query,
            retrieval=args.responder_stack,
            include_citations=args.responder_include_citations,
            answer_text=args.responder_answer,
        )
        success &= test_responder(opts)
    print("\nOverall result:", "SUCCESS" if success else "FAILURE")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
