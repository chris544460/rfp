"""Backwards-compatible vector search shim."""

from backend.retrieval.vector_store import index_size, search

__all__ = ["index_size", "search"]
