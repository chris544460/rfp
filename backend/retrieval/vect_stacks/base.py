"""Compatibility layer bridging legacy vect_stacks API to the new stacks module."""

from __future__ import annotations

from typing import List, Optional

from backend.retrieval.stacks.base import (
    RetrievalStack,
    current_stack_name,
    get_stack,
    list_stacks,
    register_stack,
    set_default_stack,
)


def gather_vector_hits(
    query: str,
    fund: Optional[str] = None,
    *,
    mode: str = "answer",
    k: int = 6,
    include_vectors: bool = False,
) -> List[dict]:
    """Delegate to the active retrieval stack (legacy helper)."""

    stack = get_stack()
    return stack.search(
        query,
        mode=mode,
        k=k,
        fund_filter=fund,
        include_vectors=include_vectors,
    )


__all__ = [
    "RetrievalStack",
    "current_stack_name",
    "gather_vector_hits",
    "get_stack",
    "list_stacks",
    "register_stack",
    "set_default_stack",
]
