"""Convenience exports for legacy retrieval imports."""

from __future__ import annotations

from typing import Any

AzureSearchStack: Any
FaissRetrievalStack: Any

try:  # pragma: no cover - optional dependency
    from backend.retrieval.stacks.azure.stack import AzureSearchStack  # type: ignore
except Exception:  # pragma: no cover
    AzureSearchStack = None  # type: ignore

try:  # pragma: no cover - optional dependency
    from backend.retrieval.stacks.faiss.stack import FaissRetrievalStack  # type: ignore
except Exception:  # pragma: no cover
    FaissRetrievalStack = None  # type: ignore

__all__ = []
if AzureSearchStack is not None:
    __all__.append("AzureSearchStack")
if FaissRetrievalStack is not None:
    __all__.append("FaissRetrievalStack")
