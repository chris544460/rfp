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

    try:
        stack = get_stack()
    except LookupError as exc:
        # Attempt to lazily instantiate the FAISS stack so legacy callers get
        # a more actionable error when vector assets are missing.
        try:
            from backend.retrieval.stacks.faiss.stack import FaissRetrievalStack

            stack = FaissRetrievalStack()
            register_stack(stack, default=True)
        except Exception as inner_exc:  # pragma: no cover - depends on env assets
            raise RuntimeError(
                "No retrieval stack has been registered. Ensure FAISS indexes are "
                "built (run backend/retrieval/stacks/faiss/embeddings/embed.sh) and "
                "optional dependencies are installed."
            ) from inner_exc
        else:
            register_stack(stack, default=True)
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
