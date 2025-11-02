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
import os, json, uuid, datetime as dt, time
from pathlib import Path
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

try:  # pragma: no cover - optional heavy dependency
    import faiss  # type: ignore
except ModuleNotFoundError:
    try:
        import faiss_cpu as faiss  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - informative error
        raise ModuleNotFoundError(
            "The backend.retrieval.vector_search module requires the `faiss` library. "
            "Install `faiss-cpu` (pip install faiss-cpu) or provide FAISS binaries on PYTHONPATH."
        ) from exc
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

# Resolve the backend root dynamically (two levels up from this file)
BACKEND_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_DIR = BACKEND_ROOT / "retrieval"
VECTOR_DIR  = RETRIEVAL_DIR / "vector_store"
ANSWER_DIR  = VECTOR_DIR / "answer"
QUESTION_DIR= VECTOR_DIR / "question"
BLEND_DIR   = VECTOR_DIR / "blend"   # optional

RECORDS_PATH = (
    BACKEND_ROOT
    / "documents"
    / "xlsx"
    / "structured_extraction"
    / "parsed_json_outputs"
    / "embedding_data.json"
)

SURF_URL    = os.getenv(
    "SURFACE_EMB_URL",
    "https://webster.bfm.com/api/ai-platform/toolkit/embedding/v1/embedding:generate"
)
SURF_MODEL  = os.getenv("SURFACE_EMB_MODEL", "text-embedding-ada-002")
AUTH        = HTTPBasicAuth(os.getenv("aladdin_user"), os.getenv("aladdin_passwd"))
HEADERS_BASE = {
    "Content-Type": "application/json",
    "VND.com.blackrock.API-Key": os.environ.get("aladdin_studio_api_key"),
}

# -- load "answer-only" index --
_answer_index = faiss.read_index(str(ANSWER_DIR / "faiss.index"))
_answer_ids   = json.load(open(ANSWER_DIR / "metadata.json"))["ids"]

# -- load "question-only" index --
_question_index = faiss.read_index(str(QUESTION_DIR / "faiss.index"))
_question_ids   = json.load(open(QUESTION_DIR / "metadata.json"))["ids"]

# -- optionally load "blend" index --
try:
    _blend_index = faiss.read_index(str(BLEND_DIR / "faiss.index"))
    _blend_ids   = json.load(open(BLEND_DIR / "metadata.json"))["ids"]
except:
    _blend_index = None
    _blend_ids   = []

# -- load raw Q-A records for retrieving text + tags --
_records: List[Dict] = json.load(open(RECORDS_PATH, "r", encoding="utf-8"))

# ----------------------------------
# bookkeeping: all indexes share the same D
# ----------------------------------
dim = _answer_index.d


def _embed(text: str) -> np.ndarray:
    """
    Embed a single text via Surface / AzureOpenAI and normalize to unit length.
    """
    hdrs = HEADERS_BASE.copy()
    hdrs.update({
        "VND.com.blackrock.Request-ID": str(uuid.uuid4()),
        "VND.com.blackrock.Origin-Timestamp": dt.datetime.utcnow()
            .replace(microsecond=0).astimezone().isoformat(),
    })
    payload = {"text": text, "modelId": SURF_MODEL}
    r = requests.post(SURF_URL, json=payload, headers=hdrs, auth=AUTH, timeout=90)
    try:
        r.raise_for_status()
    except requests.HTTPError as exc:
        try:
            detail_payload = r.json()
            detail = json.dumps(detail_payload)
        except ValueError:
            detail = r.text
        err_msg = (
            "Surface embedding request failed "
            f"status={r.status_code} reason={r.reason} "
            f"model={SURF_MODEL} text_len={len(text)} "
            f"response={detail[:500]}"
        )
        print(f"[vector_search] {err_msg}")
        raise requests.HTTPError(err_msg, response=r) from exc
    vec = np.asarray(r.json()["vector"], dtype="float32").reshape(1, dim)
    vec /= np.linalg.norm(vec, axis=1, keepdims=True)
    return vec


def _cosine_from_l2(d2: float | np.ndarray) -> float | np.ndarray:
    """
    Convert squared L2 -> cosine for unit-norm embeddings.
    """
    return 1.0 - d2 / 2.0


def _faiss_search(
    idx: faiss.Index,
    all_ids: List[str],
    qvec: np.ndarray,
    k: int,
    fund_filter: Optional[str],
    *,
    origin: str,
    include_vectors: bool,
) -> List[Dict]:
    """
    Helper: search one FAISS index with over-retrieval, filter by fund_filter,
    and return up to k hits as a list of dicts.
    """
    capacity = len(all_ids)
    if capacity == 0 or k <= 0:
        return []

    over_k = min(capacity, max(k * 5, 32))

    while True:
        D, I = idx.search(qvec, over_k)  # squared-L2 distances
        out: List[Dict[str, object]] = []
        cnt = 1
        for dist2, idx_row in zip(D[0], I[0]):
            row_id = int(idx_row)
            if row_id < 0:
                continue
            if fund_filter:
                rec_tags = _records[row_id]["metadata"].get("tags", [])
                if fund_filter not in rec_tags:
                    continue
            rec = _records[row_id]
            entry: Dict[str, object] = {
                "rank": cnt,
                "id": all_ids[row_id],
                "text": rec["text"],
                "meta": rec["metadata"],
                "l2_sq": float(dist2),
                "cosine": float(_cosine_from_l2(dist2)),
                "origin": origin,
                "raw_index": row_id,
            }
            if include_vectors:
                try:
                    entry["embedding"] = idx.reconstruct(row_id).astype("float32").tolist()
                except Exception:
                    entry["embedding_error"] = "reconstruct_failed"
            out.append(entry)
            cnt += 1
            if cnt > k:
                break

        if len(out) >= k or over_k >= capacity:
            return out

        over_k = min(capacity, over_k * 2)


def search(
    query: str,
    k: int = 6,
    *,
    mode: str = "answer",   # "answer", "question", "blend", or "dual"
    fund_filter: Optional[str] = None,
    include_vectors: bool = False,
) -> List[Dict]:
    """
    Dual-index search:
      * mode="answer"   -> search only in _answer_index
      * mode="question" -> search only in _question_index
      * mode="blend"    -> search only in _blend_index (if built)
      * mode="dual"     -> search both "answer" and "question" in parallel, merge & rerank

    fund_filter (e.g. "Fund - EFVI") prunes by metadata.tags.
    """
    assert mode in ("answer", "question", "blend", "dual")

    # (1) Embed the query once
    qvec = _embed(query)  # shape = (1, D)

    # (2) Single-index modes just invoke _faiss_search directly
    if mode == "answer":
        return _faiss_search(
            _answer_index,
            _answer_ids,
            qvec,
            k,
            fund_filter,
            origin="vector:answer",
            include_vectors=include_vectors,
        )
    if mode == "question":
        return _faiss_search(
            _question_index,
            _question_ids,
            qvec,
            k,
            fund_filter,
            origin="vector:question",
            include_vectors=include_vectors,
        )
    if mode == "blend":
        assert _blend_index is not None, "No blend index found on disk"
        return _faiss_search(
            _blend_index,
            _blend_ids,
            qvec,
            k,
            fund_filter,
            origin="vector:blend",
            include_vectors=include_vectors,
        )

    # (3) mode == "dual": run answer-only and question-only searches in parallel
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_ans = executor.submit(
            _faiss_search,
            _answer_index,
            _answer_ids,
            qvec,
            k,
            fund_filter,
            origin="vector:answer",
            include_vectors=include_vectors,
        )
        future_qn  = executor.submit(
            _faiss_search,
            _question_index,
            _question_ids,
            qvec,
            k,
            fund_filter,
            origin="vector:question",
            include_vectors=include_vectors,
        )

        ans_hits = future_ans.result()
        qn_hits  = future_qn.result()

    # (4) Merge & rerank: deduplicate by recordID, keep the higher cosine, sort descending
    merged: Dict[str, Dict] = {}
    for h in ans_hits + qn_hits:
        key = h["id"]
        if key not in merged or h["cosine"] > merged[key]["cosine"]:
            merged[key] = h

    best = sorted(merged.values(), key=lambda x: x["cosine"], reverse=True)[:k]
    for i, x in enumerate(best, start=1):
        x["rank"] = i
    return best


def index_size(mode: str) -> int:
    """Return the number of records available for the requested mode."""

    mode = mode.lower()
    if mode == "answer":
        return len(_answer_ids)
    if mode == "question":
        return len(_question_ids)
    if mode == "blend":
        if _blend_index is None:
            raise ValueError("No blend index available on disk")
        return len(_blend_ids)
    if mode == "dual":
        return len(_records)
    raise ValueError(f"Unknown mode '{mode}'")


# --------------------- smoke-test ---------------------
if __name__ == "__main__":
    print("Answer-only:")
    for h in search("What is your LTV target?", k=5, mode="answer"):
        print(f"  {h['rank']}. cos={h['cosine']:.3f} | {h['text'][:60]}…")

    print("\nQuestion-only:")
    for h in search("What is your LTV target?", k=5, mode="question"):
        print(f"  {h['rank']}. cos={h['cosine']:.3f} | {h['text'][:60]}…")

    if _blend_index is not None:
        print("\nBlend:")
        for h in search("What is your LTV target?", k=5, mode="blend"):
            print(f"  {h['rank']}. cos={h['cosine']:.3f} | {h['text'][:60]}…")

    print("\nDual (answer + question):")
    for h in search("What is your LTV target?", k=5, mode="dual"):
        print(f"  {h['rank']}. cos={h['cosine']:.3f} | {h['text'][:60]}…")
