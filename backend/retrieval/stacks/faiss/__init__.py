"""FAISS-based retrieval stack implementation."""

from __future__ import annotations

from .stack import FaissRetrievalStack, index_size, search

DEFAULT_STACK: FaissRetrievalStack | None = None

try:  # pragma: no cover - depends on deployment assets
    DEFAULT_STACK = FaissRetrievalStack()
except Exception:
    # Defer errors until the stack is first requested; this keeps module import
    # usable for tooling (embed.sh, Streamlit setup) even when indexes are absent.
    DEFAULT_STACK = None

__all__ = ["DEFAULT_STACK", "FaissRetrievalStack", "index_size", "search"]
