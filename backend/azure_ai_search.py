"""Convenience wrapper around the Azure AI Search retrieval stack."""

from __future__ import annotations

from typing import List, Optional

from backend.retrieval.stacks.azure.stack import AzureSearchStack


def create_filter(tag: Optional[str], source: Optional[str]) -> Optional[str]:
    """Mirror the manager-provided filter helper for downstream callers."""

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
    """
    Thin adapter that mirrors the manager-provided helper.

    It delegates to the AzureSearchStack so that downstream code (tests, UI)
    can continue calling `search_data` without knowing about the stack interface.
    """

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
        """Fetch Azure AI Search results, optionally filtering by tag/source."""

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
