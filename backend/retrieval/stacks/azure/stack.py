"""Azure AI Search-backed retrieval stack."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from backend.retrieval.stacks.base import RetrievalStack, register_stack

try:  # pragma: no cover - optional dependency
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.models import VectorizedQuery
    _AZURE_IMPORT_ERROR: Optional[Exception] = None
except ModuleNotFoundError as exc:  # pragma: no cover - informative but non-fatal
    AzureKeyCredential = SearchClient = VectorizedQuery = None  # type: ignore
    _AZURE_IMPORT_ERROR = ModuleNotFoundError(
        "The Azure retrieval stack requires the 'azure-search-documents' package. "
        "Install it with: pip install azure-search-documents"
    )
    _AZURE_IMPORT_ERROR.__cause__ = exc

from .completitions import Completitions


class AzureSearchStack(RetrievalStack):
    """Retrieval stack that queries Azure AI Search with semantic + vector scoring."""

    CONFIG_FILENAME = "config.json"

    def __init__(self, *, name: str = "azure") -> None:
        super().__init__(name=name)

        if _AZURE_IMPORT_ERROR is not None:
            raise _AZURE_IMPORT_ERROR

        config_path = Path(__file__).with_name(self.CONFIG_FILENAME)
        if not config_path.exists():
            raise FileNotFoundError(
                f"Azure retrieval stack config missing: {config_path}. "
                "Edit config.json with your Azure AI Search endpoint + index."
            )

        with open(config_path, "r", encoding="utf-8") as config_file:
            self._config: Dict[str, str] = json.load(config_file)

        self._endpoint = self._config.get("aiSearchEndpoint")
        self._index_name = self._config.get("indexName")
        if not self._endpoint or not self._index_name:
            raise ValueError("Azure config must include 'aiSearchEndpoint' and 'indexName'.")

        api_key = os.getenv("AZURE_AI_SEARCH_KEY")
        if not api_key:
            raise RuntimeError(
                "AZURE_AI_SEARCH_KEY environment variable is not set. Provide your Azure Search admin key."
            )

        self._semantic_config = self._config.get("semanticConfiguration", "default")
        self._vector_field = self._config.get("vectorField", "embedding")
        self._content_field = self._config.get("contentField", "content")
        self._tags_field = self._config.get("tagsField", "tags")
        self._source_field = self._config.get("sourceField", "source")
        self._metadata_field = self._config.get("metadataField", "metadata")
        self._embedding_model = self._config.get("embeddingModel", "text-embedding-ada-002")

        credential = AzureKeyCredential(api_key)
        self._search_client = SearchClient(
            endpoint=self._endpoint,
            index_name=self._index_name,
            credential=credential,
        )
        self._embedding_client = Completitions(document_name="AzureSearchStack")

    def search(
        self,
        query: str,
        *,
        mode: str = "answer",
        k: int = 6,
        fund_filter: Optional[str] = None,
        include_vectors: bool = False,
    ) -> List[Dict[str, object]]:
        """
        Execute a semantic + vector search against Azure AI Search.

        The Azure stack does not differentiate between answer/question modes;
        the `mode` argument is accepted for compatibility with other stacks.
        """
        if not query:
            return []

        embedding = self._embedding_client.get_embedding(
            query, model=self._embedding_model
        )
        if not embedding:
            return []

        azure_filter = self._build_filter(fund_filter)
        overfetch = max(k * 2, 20)
        vector_query = VectorizedQuery(
            vector=embedding,
            k_nearest_neighbors=overfetch,
            fields=self._vector_field,
        )

        search_iterable = self._search_client.search(
            search_text=query,
            vector_queries=[vector_query],
            filter=azure_filter,
            semantic_configuration_name=self._semantic_config,
            query_type="semantic",
            top=overfetch,
            vector_filter_mode="postFilter",
            select=["*", self._vector_field],
        )

        hits: List[Dict[str, object]] = []
        for rank, item in enumerate(search_iterable, start=1):
            doc = dict(item)
            meta = doc.get(self._metadata_field) or {}
            if not isinstance(meta, dict):
                meta = {"metadata": meta}

            source = doc.get(self._source_field)
            tags = doc.get(self._tags_field, [])
            if fund_filter and tags and fund_filter not in tags:
                # Guard against mismatched casing or filters when Azure search doesn't enforce it
                continue

            score = float(doc.get("@search.score", 0.0))
            entry: Dict[str, object] = {
                "rank": rank,
                "id": doc.get("id"),
                "text": doc.get(self._content_field, ""),
                "meta": meta or {},
                "origin": "azure_search",
                "cosine": score,
                "score": score,
            }
            if source is not None:
                entry.setdefault("meta", {}).setdefault("source", source)
            if tags is not None:
                entry.setdefault("meta", {}).setdefault("tags", tags)

            if include_vectors:
                entry["embedding"] = doc.get(self._vector_field)

            hits.append(entry)
            if len(hits) >= k:
                break

        return hits

    def index_size(self, mode: str) -> int:
        """Azure stack cannot report index cardinality without an additional service call."""
        # We attempt to fetch document count via count() API when available.
        try:
            return int(self._search_client.get_document_count())
        except Exception:  # pragma: no cover - fallback path
            raise RuntimeError(
                "Azure retrieval stack does not support index_size lookup without query permissions."
            )

    def _build_filter(self, fund_filter: Optional[str]) -> Optional[str]:
        if not fund_filter:
            return None
        tag_field = self._tags_field or "tags"
        safe_value = fund_filter.replace("'", "''")
        return f"{tag_field}/any(t: t eq '{safe_value}')"


# Register stack on import when configuration is available.
try:  # pragma: no cover - environment dependent
    DEFAULT_AZURE_STACK = AzureSearchStack()
except Exception as exc:
    DEFAULT_AZURE_STACK = None  # type: ignore
else:
    register_stack(DEFAULT_AZURE_STACK)


__all__ = ["AzureSearchStack", "DEFAULT_AZURE_STACK"]
