"""Shared helpers for retrieval smoke-test scripts."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.retrieval.stacks.base import RetrievalStack


def load_azure_stack() -> "RetrievalStack":
    """Return an initialised AzureSearchStack or raise RuntimeError with context."""
    try:
        module = importlib.import_module("backend.retrieval.stacks.azure.stack")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Azure retrieval stack is not installed. Install dependencies with: pip install azure-search-documents"
        ) from exc

    AzureSearchStack = getattr(module, "AzureSearchStack")
    default_stack = getattr(module, "DEFAULT_AZURE_STACK", None)

    if default_stack is not None:
        return default_stack

    try:
        return AzureSearchStack()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{exc}\nExpected configuration file at backend/retrieval/stacks/azure/config.json."
        ) from exc
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc}\nEnsure AZURE_AI_SEARCH_KEY is set in your environment or .env file."
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive catch
        raise RuntimeError(f"Failed to initialise AzureSearchStack: {exc}") from exc


def load_faiss_stack() -> "RetrievalStack":
    """Return an initialised FaissRetrievalStack or raise RuntimeError with context."""
    try:
        module = importlib.import_module("backend.retrieval.stacks.faiss.stack")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "FAISS stack import failed. Install FAISS with: pip install faiss-cpu"
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive catch
        raise RuntimeError(f"Unexpected failure importing FAISS stack: {exc}") from exc

    FaissRetrievalStack = getattr(module, "FaissRetrievalStack")
    default_stack = getattr(module, "DEFAULT_STACK", None)

    if default_stack is not None:
        return default_stack

    try:
        return FaissRetrievalStack()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{exc}\nEnsure FAISS assets exist under backend/retrieval/stacks/faiss/vector_store."
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive catch
        raise RuntimeError(f"Failed to initialise FaissRetrievalStack: {exc}") from exc
