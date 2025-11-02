"""Backwards-compatible vector search shim."""

from backend.retrieval.stacks.faiss import index_size, search

__all__ = ["index_size", "search"]
