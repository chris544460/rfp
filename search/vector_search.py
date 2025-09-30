"""Shim module re-exporting the vector search implementation."""

from vector_search.search import search  # noqa: F401

__all__ = ["search"]
