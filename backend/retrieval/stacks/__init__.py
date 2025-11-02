"""Registration and selection utilities for retrieval stacks."""

from __future__ import annotations

import importlib
import os
import sys

from .base import (
    RetrievalStack,
    current_stack_name,
    get_stack,
    list_stacks,
    register_stack,
    set_default_stack,
)

# Import built-in stacks so they register themselves on module import.
try:
    _faiss_pkg = importlib.import_module("backend.retrieval.stacks.faiss")
    # Populate compatibility aliases for legacy imports.
    sys.modules.setdefault("backend.retrieval.vector_store", _faiss_pkg)
    sys.modules.setdefault(
        "backend.retrieval.vector_store.search",
        importlib.import_module("backend.retrieval.stacks.faiss.stack"),
    )
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    _faiss_pkg = None  # type: ignore

try:  # pragma: no cover - optional dependency
    importlib.import_module("backend.retrieval.stacks.azure")
except ModuleNotFoundError:
    pass

# Allow environment configuration of the active stack (new name takes priority).
_env_stack = os.getenv("RFP_RETRIEVAL_STACK") or os.getenv("RFP_RETRIEVAL_BACKEND")
if _env_stack:
    try:
        set_default_stack(_env_stack)
    except KeyError:
        pass

__all__ = [
    "RetrievalStack",
    "current_stack_name",
    "get_stack",
    "list_stacks",
    "register_stack",
    "set_default_stack",
]
