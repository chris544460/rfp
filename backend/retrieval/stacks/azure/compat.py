"""Legacy compatibility helpers for Azure AI Search.

This module mirrors the historic `backend.azure_ai_search` interface so that
callers can continue importing `RetrieverClient` and `create_filter` while the
modern retrieval stack implementation lives under `backend.retrieval.stacks`.
"""

from __future__ import annotations

from typing import List, Optional

from .stack import AzureSearchStack


def create_filter(tag: Optional[str], source: Optional[str]) -> Optional[str]:
    """Build an Azure Search OData filter mirroring the legacy helper."""

    if not tag and not source:
        return None

    parts: List[str] = []
    if tag:
        safe_tag = tag.replace("'", "''")
        parts.append(f"tags eq '{safe_tag}'")
    if source:
        safe_source = source.replace("'", "''")
        parts.append(f"source eq '{safe_source}'")
    return " and ".join(parts)


class RetrieverClient:
    """Adapter that preserves the legacy `search_data` helper."""

    def __init__(self) -> None:
        self._stack = AzureSearchStack()

    def search_data(
        self,
        query: str,
        source: Optional[str] = None,
        tag: Optional[str] = None,
        *,
        k: int = 20,
    ) -> List[dict]:
        """Fetch results using the Azure Search stack, optionally filtering."""

        hits = self._stack.search(query, fund_filter=tag, mode="answer", k=k)
        if source:
            source_lower = source.lower()
            hits = [
                hit
                for hit in hits
                if str(hit.get("meta", {}).get("source", "")).lower() == source_lower
            ]
        return hits


__all__ = ["RetrieverClient", "create_filter"]

