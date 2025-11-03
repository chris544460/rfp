"""Legacy compatibility shims for `backend.retrieval.vect_stacks` imports."""

from .base import (
    RetrievalStack,
    current_stack_name,
    gather_vector_hits,
    get_stack,
    list_stacks,
    register_stack,
    set_default_stack,
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
