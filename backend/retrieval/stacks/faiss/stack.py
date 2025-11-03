#!/usr/bin/env python3
"""
dual-index vector search

Modes:
  * "answer"  -> search only in the answer-only index (W=0)
  * "question"-> search only in the question-only index (W=1)
  * "blend"   -> search only in the blend index (W=0.65), if you built it
  * "dual"    -> run both "answer" and "question" in parallel, then merge & rerank

Usage:
    hits = search("some query", k=6, mode="dual", fund_filter="Fund - EFVI")
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import requests
from backend.utils.dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

from backend.retrieval.stacks.base import RetrievalStack, register_stack

try:  # pragma: no cover - optional heavy dependency
    import faiss  # type: ignore
except ModuleNotFoundError:
    try:
        import faiss_cpu as faiss  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - informative error
        raise ModuleNotFoundError(
            "The backend.retrieval.vector_store module requires the `faiss` library. "
            "Install `faiss-cpu` (pip install faiss-cpu) or provide FAISS binaries on PYTHONPATH."
        ) from exc

load_dotenv()

# Resolve directories relative to the FAISS stack package.
STACK_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = STACK_ROOT.parents[2]
RETRIEVAL_DIR = BACKEND_ROOT / "retrieval"
VECTOR_DIR = RETRIEVAL_DIR / "vector_store"
ANSWER_DIR = VECTOR_DIR / "answer"
QUESTION_DIR = VECTOR_DIR / "question"
BLEND_DIR = VECTOR_DIR / "blend"  # optional

STRUCTURED_EXTRACTION_DIR = STACK_ROOT / "structured_extraction"
PARSED_OUTPUT_DIR = STRUCTURED_EXTRACTION_DIR / "parsed_json_outputs"
RECORDS_PATH = PARSED_OUTPUT_DIR / "embedding_data.json"

SURF_URL = os.getenv(
    "SURFACE_EMB_URL",
    "https://webster.bfm.com/api/ai-platform/toolkit/embedding/v1/embedding:generate",
)
SURF_MODEL = os.getenv("SURFACE_EMB_MODEL", "text-embedding-ada-002")
AUTH = HTTPBasicAuth(os.getenv("aladdin_user"), os.getenv("aladdin_passwd"))
HEADERS_BASE = {
    "Content-Type": "application/json",
    "VND.com.blackrock.API-Key": os.environ.get("aladdin_studio_api_key"),
}


class FaissRetrievalStack(RetrievalStack):
    """FAISS-backed retrieval stack used by default in the RFP responder."""

    _VALID_MODES = ("answer", "question", "blend", "dual")

    def __init__(
        self,
        *,
        answer_dir: Path = ANSWER_DIR,
        question_dir: Path = QUESTION_DIR,
        blend_dir: Path = BLEND_DIR,
        records_path: Path = RECORDS_PATH,
        name: str = "faiss",
    ) -> None:
        super().__init__(name=name)

        self._answer_index = faiss.read_index(str(answer_dir / "faiss.index"))
        with open(answer_dir / "metadata.json", "r", encoding="utf-8") as fh:
            self._answer_ids = json.load(fh)["ids"]

        self._question_index = faiss.read_index(str(question_dir / "faiss.index"))
        with open(question_dir / "metadata.json", "r", encoding="utf-8") as fh:
            self._question_ids = json.load(fh)["ids"]

        try:
            self._blend_index = faiss.read_index(str(blend_dir / "faiss.index"))
            with open(blend_dir / "metadata.json", "r", encoding="utf-8") as fh:
                self._blend_ids = json.load(fh)["ids"]
        except Exception:
            self._blend_index = None
            self._blend_ids: List[str] = []

        with open(records_path, "r", encoding="utf-8") as fh:
            self._records: List[Dict[str, object]] = json.load(fh)

        self._dim = self._answer_index.d

    @staticmethod
    def _cosine_from_l2(d2: float | np.ndarray) -> float | np.ndarray:
        """Convert squared L2 -> cosine for unit-norm embeddings."""
        return 1.0 - d2 / 2.0

    def _embed(self, text: str) -> np.ndarray:
        """Embed a single text via Surface / AzureOpenAI and normalize to unit length."""
        headers = HEADERS_BASE.copy()
        headers.update(
            {
                "VND.com.blackrock.Request-ID": str(uuid.uuid4()),
                "VND.com.blackrock.Origin-Timestamp": dt.datetime.utcnow()
                .replace(microsecond=0)
                .astimezone()
                .isoformat(),
            }
        )
        payload = {"text": text, "modelId": SURF_MODEL}
        response = requests.post(SURF_URL, json=payload, headers=headers, auth=AUTH, timeout=90)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            try:
                detail_payload = response.json()
                detail = json.dumps(detail_payload)
            except ValueError:
                detail = response.text
            err_msg = (
                "Surface embedding request failed "
                f"status={response.status_code} reason={response.reason} "
                f"model={SURF_MODEL} text_len={len(text)} "
                f"response={detail[:500]}"
            )
            print(f"[vector_search] {err_msg}")
            raise requests.HTTPError(err_msg, response=response) from exc
        vec = np.asarray(response.json()["vector"], dtype="float32").reshape(1, self._dim)
        vec /= np.linalg.norm(vec, axis=1, keepdims=True)
        return vec

    def _faiss_search(
        self,
        idx: faiss.Index,
        all_ids: List[str],
        qvec: np.ndarray,
        k: int,
        fund_filter: Optional[str],
        *,
        origin: str,
        include_vectors: bool,
    ) -> List[Dict[str, object]]:
        """Helper: search one FAISS index, apply fund filter, and return up to k hits."""
        capacity = len(all_ids)
        if capacity == 0 or k <= 0:
            return []

        over_k = min(capacity, max(k * 5, 32))

        while True:
            distances, indices = idx.search(qvec, over_k)  # squared-L2 distances
            out: List[Dict[str, object]] = []
            rank_counter = 1
            for dist2, idx_row in zip(distances[0], indices[0]):
                row_id = int(idx_row)
                if row_id < 0:
                    continue
                if fund_filter:
                    tags = self._records[row_id]["metadata"].get("tags", [])  # type: ignore[assignment]
                    if fund_filter not in tags:
                        continue
                record = self._records[row_id]
                entry: Dict[str, object] = {
                    "rank": rank_counter,
                    "id": all_ids[row_id],
                    "text": record["text"],
                    "meta": record["metadata"],
                    "l2_sq": float(dist2),
                    "cosine": float(self._cosine_from_l2(dist2)),
                    "origin": origin,
                    "raw_index": row_id,
                }
                if include_vectors:
                    try:
                        entry["embedding"] = idx.reconstruct(row_id).astype("float32").tolist()
                    except Exception:
                        entry["embedding_error"] = "reconstruct_failed"
                out.append(entry)
                rank_counter += 1
                if rank_counter > k:
                    break

            if len(out) >= k or over_k >= capacity:
                return out

            over_k = min(capacity, over_k * 2)

    def search(
        self,
        query: str,
        *,
        mode: str = "answer",
        k: int = 6,
        fund_filter: Optional[str] = None,
        include_vectors: bool = False,
    ) -> List[Dict[str, object]]:
        """Run the FAISS search pipeline for the requested mode."""
        assert mode in self._VALID_MODES, f"Unsupported retrieval mode '{mode}'"

        qvec = self._embed(query)  # shape = (1, D)

        if mode == "answer":
            return self._faiss_search(
                self._answer_index,
                self._answer_ids,
                qvec,
                k,
                fund_filter,
                origin="vector:answer",
                include_vectors=include_vectors,
            )
        if mode == "question":
            return self._faiss_search(
                self._question_index,
                self._question_ids,
                qvec,
                k,
                fund_filter,
                origin="vector:question",
                include_vectors=include_vectors,
            )
        if mode == "blend":
            assert self._blend_index is not None, "No blend index found on disk"
            return self._faiss_search(
                self._blend_index,
                self._blend_ids,
                qvec,
                k,
                fund_filter,
                origin="vector:blend",
                include_vectors=include_vectors,
            )

        # mode == "dual": run answer-only and question-only searches in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_answer = executor.submit(
                self._faiss_search,
                self._answer_index,
                self._answer_ids,
                qvec,
                k,
                fund_filter,
                origin="vector:answer",
                include_vectors=include_vectors,
            )
            future_question = executor.submit(
                self._faiss_search,
                self._question_index,
                self._question_ids,
                qvec,
                k,
                fund_filter,
                origin="vector:question",
                include_vectors=include_vectors,
            )
            answer_hits = future_answer.result()
            question_hits = future_question.result()

        merged: Dict[str, Dict[str, object]] = {}
        for hit in answer_hits + question_hits:
            key = str(hit["id"])
            if key not in merged or hit["cosine"] > merged[key]["cosine"]:
                merged[key] = hit

        best = sorted(merged.values(), key=lambda x: x["cosine"], reverse=True)[:k]
        for idx, hit in enumerate(best, start=1):
            hit["rank"] = idx
        return best

    def index_size(self, mode: str) -> int:
        """Return the number of records available for the requested mode."""
        norm_mode = mode.lower()
        if norm_mode == "answer":
            return len(self._answer_ids)
        if norm_mode == "question":
            return len(self._question_ids)
        if norm_mode == "blend":
            if self._blend_index is None:
                raise ValueError("No blend index available on disk")
            return len(self._blend_ids)
        if norm_mode == "dual":
            return len(self._records)
        raise ValueError(f"Unknown mode '{mode}'")


# Instantiate and register the default stack immediately on import when assets are available.
try:  # pragma: no cover - depends on deployment assets
    DEFAULT_STACK = FaissRetrievalStack()
except Exception:
    DEFAULT_STACK = None  # type: ignore
else:
    register_stack(DEFAULT_STACK, default=True)


def search(
    query: str,
    k: int = 6,
    *,
    mode: str = "answer",
    fund_filter: Optional[str] = None,
    include_vectors: bool = False,
) -> List[Dict[str, object]]:
    """Module-level helper delegating to the default FAISS stack."""
    if DEFAULT_STACK is None:
        raise RuntimeError("FAISS stack is not initialized; vector_store assets missing?")
    return DEFAULT_STACK.search(
        query,
        mode=mode,
        k=k,
        fund_filter=fund_filter,
        include_vectors=include_vectors,
    )


def index_size(mode: str) -> int:
    """Return the number of records available for the requested mode."""
    if DEFAULT_STACK is None:
        raise RuntimeError("FAISS stack is not initialized; vector_store assets missing?")
    return DEFAULT_STACK.index_size(mode)


__all__ = ["FaissRetrievalStack", "DEFAULT_STACK", "index_size", "search"]
