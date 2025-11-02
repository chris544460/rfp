"""Base interfaces and registry for pluggable retrieval stacks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class RetrievalStack(ABC):
    """Abstract interface every retrieval stack must implement."""

    name: str

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        mode: str,
        k: int,
        fund_filter: Optional[str],
        include_vectors: bool,
    ) -> List[Dict[str, object]]:
        """Return the top-k hits for the query."""

    @abstractmethod
    def index_size(self, mode: str) -> int:
        """Return the number of records available for the requested mode."""


_REGISTRY: Dict[str, RetrievalStack] = {}
_DEFAULT_STACK: Optional[str] = None


def register_stack(stack: RetrievalStack, *, default: bool = False) -> None:
    """Register a stack instance, optionally promoting it to the default."""
    key = stack.name.lower()
    _REGISTRY[key] = stack

    global _DEFAULT_STACK
    if default or _DEFAULT_STACK is None:
        _DEFAULT_STACK = key


def get_stack(name: Optional[str] = None) -> RetrievalStack:
    """Return the requested stack (or the default when name is omitted)."""
    key = (name.lower() if name else _DEFAULT_STACK)
    if key is None:
        raise LookupError("No retrieval stack has been registered.")
    try:
        return _REGISTRY[key]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(f"Retrieval stack '{name}' is not registered.") from exc


def set_default_stack(name: str) -> None:
    """Make the named stack the new default selection."""
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(f"Retrieval stack '{name}' is not registered.")
    global _DEFAULT_STACK
    _DEFAULT_STACK = key


def current_stack_name() -> Optional[str]:
    """Return the lowercase name of the active default stack."""
    return _DEFAULT_STACK


def list_stacks() -> List[str]:
    """Return the registered stack names."""
    return sorted(_REGISTRY.keys())
