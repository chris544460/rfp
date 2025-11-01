"""Shim module re-exporting the vector search implementation."""

from vector_search.search import index_size, search  # noqa: F401

__all__ = ["search", "index_size"]
