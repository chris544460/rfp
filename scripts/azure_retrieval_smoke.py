#!/usr/bin/env python3
"""Quick CLI to exercise the Azure AI Search retrieval stack."""

from __future__ import annotations

import argparse
import sys
from textwrap import shorten
from typing import Any, Dict, Iterable, Optional

from backend.utils.dotenv import load_dotenv
from scripts.retrieval_smoke_utils import load_azure_stack


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a semantic + vector search against Azure AI Search.",
    )
    parser.add_argument("query", help="Query text to send to Azure AI Search.")
    parser.add_argument(
        "--k",
        type=int,
        default=6,
        help="Maximum number of hits to display (default: 6).",
    )
    parser.add_argument(
        "--mode",
        default="answer",
        help="Retrieval mode hint (kept for compatibility; Azure ignores it).",
    )
    parser.add_argument(
        "--fund",
        dest="fund_filter",
        help="Optional fund tag to filter results (matches Azure 'tags').",
    )
    parser.add_argument(
        "--include-vectors",
        action="store_true",
        help="Include embedding vectors in the printed payload.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Pretty-print the raw JSON payload for each hit.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _print_hits(hits: Iterable[Dict[str, Any]], *, raw: bool, include_vectors: bool) -> None:
    hits_list = list(hits)
    if not hits_list:
        print("No hits returned.")
        return

    print(f"Retrieved {len(hits_list)} hit(s):")
    for idx, hit in enumerate(hits_list, start=1):
        score = hit.get("score", hit.get("cosine", "n/a"))
        try:
            score_str = f"{float(score):.3f}"
        except (TypeError, ValueError):
            score_str = str(score)
        doc_id = hit.get("id", "unknown")
        source = (hit.get("meta") or {}).get("source", "unknown")
        snippet = shorten((hit.get("text") or "").strip(), width=160, placeholder="...")
        print(f"  {idx}. score={score_str} id={doc_id} source={source}")
        print(f"     snippet: {snippet}")
        if raw:
            payload = dict(hit)
            if not include_vectors and "embedding" in payload:
                payload.pop("embedding", None)
            print("     raw:", payload)


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = _parse_args(argv)
    load_dotenv(override=False)
    try:
        stack = load_azure_stack()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    hits = stack.search(
        args.query,
        mode=args.mode,
        k=args.k,
        fund_filter=args.fund_filter,
        include_vectors=args.include_vectors,
    )
    _print_hits(hits, raw=args.raw, include_vectors=args.include_vectors)


if __name__ == "__main__":
    main()
