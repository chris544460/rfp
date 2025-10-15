#!/usr/bin/env python3
"""
encode.py  ⇒ send (answer.text) + (question.text) to Surface embedding,
    blend them into a single vector per record, then build a FAISS index.

Usage:
    python3 encode.py \
        --file structured_extraction/parsed_json_outputs/embedding_data.json \
        --output vector_store \
        --workers 4 \
        --model text-embedding-ada-002 \
        --weight 0.65
"""

import os
import json
import uuid
import argparse
import time
import concurrent.futures
from pathlib import Path
from typing import List, Dict

DEFAULT_EMBEDDING_FILE = Path(
    "structured_extraction/parsed_json_outputs/embedding_data.json"
)

import numpy as np
import faiss
import requests
from tqdm import tqdm
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:
    # Fallback for environments where urllib3 Retry import path differs
    from requests.packages.urllib3.util.retry import Retry  # type: ignore
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------
# Surface / Azure-OpenAI configuration
# ----------------------------------------

WEBSTER       = os.getenv("defaultWebServer", "https://webster.bfm.com")
EMB_URL       = f"{WEBSTER}/api/ai-platform/toolkit/embedding/v1/embedding:generate"
HEADERS_BASE  = {
    "Content-Type": "application/json",
    "VND.com.blackrock.API-Key": os.environ["aladdin_studio_api_key"],
}
AUTH          = HTTPBasicAuth(os.environ["aladdin_user"], os.environ["aladdin_passwd"])
RETRIES       = 5
TIMEOUT       = 90  # seconds
MODEL_DEFAULT = "text-embedding-ada-002"


def _build_session() -> requests.Session:
    """Create a requests Session with retry/backoff for 429 and 5xx.

    - Respects `Retry-After` header when present
    - Retries POST on 429/500/502/503/504
    - Connection pool sized for small thread pools
    """
    status_forcelist = (429, 500, 502, 503, 504)
    retry = Retry(
        total=RETRIES,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=status_forcelist,
        allowed_methods={"POST"},
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    sess = requests.Session()
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


SESSION = _build_session()

# I/O helpers

def load_records(json_file: str) -> List[Dict]:
    """
    Load all JSON records (each with 'text' and in metadata a 'question')
    from the given file. Returns a list of dicts with at least:
        { 'text': ..., 'metadata': {'id':..., 'question':..., ...} }
    """
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Verify every record has 'text' and metadata.question
    for rec in data:
        if "text" not in rec:
            raise ValueError("Each record must have a 'text' field.")
        if "metadata" not in rec or "question" not in rec["metadata"]:
            raise ValueError("Each record must have metadata.question present.")
    return data


def save_index(vectors: np.ndarray, out_dir: Path):
    """
    Build a FlatL2 FAISS index from the given (N, dim) array and write to disk.
    """
    dim = vectors.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(vectors)

    out_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(out_dir / "faiss.index"))


def save_metadata(ids: List[str], out_dir: Path):
    """
    Save metadata (just the list of ids) alongside the index.
    """
    with open(out_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump({"ids": ids}, f, indent=2)

# HTTP wrapper for single-text embedding via Surface

def embed_one(text: str, model: str) -> List[float]:
    """
    Embed a single text string via Surface / Azure-OpenAI endpoint.
    Returns a raw float list of length = embedding_dim.
    Retries up to RETRIES times if the HTTP call fails.
    """
    payload = {"text": text, "modelId": model}
    for attempt in range(1, RETRIES + 1):
        HEADERS = HEADERS_BASE.copy()
        HEADERS["VND.com.blackrock.Request-ID"] = str(uuid.uuid4())
        HEADERS["VND.com.blackrock.Origin-Timestamp"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        r = SESSION.post(EMB_URL, json=payload, headers=HEADERS, auth=AUTH, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()["vector"]  # Surface returns "vector" key
        # If rate limited, honor Retry-After when present; else exponential backoff with jitter
        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                sleep_for = float(retry_after)
            except ValueError:
                sleep_for = attempt * 2.0
        else:
            sleep_for = attempt * 2.0
        if r.status_code == 429:
            # Light logging to aid troubleshooting without being noisy
            print("Received 429 Too Many Requests — backing off...")
        if attempt == RETRIES:
            r.raise_for_status()
        time.sleep(sleep_for)

# ----------------------------------------
# main
# ----------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Load JSON records with 'text' and metadata.question, embed both, blend, and build FAISS index."
    )
    ap.add_argument(
        "--file",
        default=str(DEFAULT_EMBEDDING_FILE),
        help=(
            "Path to the JSON file of records (each must have 'text' and metadata.question). "
            "Defaults to structured_extraction/parsed_json_outputs/embedding_data.json."
        ),
    )
    ap.add_argument(
        "--output", required=True,
        help="Directory where faiss.index and metadata.json will be written."
    )
    ap.add_argument(
        "--workers", type=int, default=4,
        help="How many threads to use for parallel embedding."
    )
    ap.add_argument(
        "--model", default=MODEL_DEFAULT,
        help="Surface embedding model to use (e.g. text-embedding-ada-002)."
    )
    ap.add_argument(
        "--weight", type=float, default=0.5,
        help="Question weight w ∈ [0,1]. Final vector = normalize((1-w)*A + w*Q)."
    )
    args = ap.parse_args()

    # 1) Load all records
    print(f"Loading records from {args.file} ...")
    recs = load_records(args.file)
    N = len(recs)

    # Extract the answer-texts and question-texts, and ids
    ans_texts = [rec["text"] for rec in recs]
    q_texts   = [rec["metadata"]["question"] for rec in recs]
    ids       = [rec["metadata"].get("id", str(i)) for i, rec in enumerate(recs)]

    print(f"✔️ {N} records loaded")
    print(f"Embedding with model = '{args.model}', question_weight = {args.weight}")

    # 2) Embed
    # Preserve order: use executor.map which yields results in input order.
    # Also avoid embedding the unused side when weight is 0.0 or 1.0.
    w = float(args.weight)
    EPS = 1e-9
    ans_vecs: np.ndarray | None = None
    q_vecs: np.ndarray | None = None

    if w <= EPS:
        print("⚙️ Embedding all answer-texts … (w≈0: skipping questions)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            ans_vecs_list = list(tqdm(ex.map(lambda t: embed_one(t, args.model), ans_texts), total=N))
        ans_vecs = np.array(ans_vecs_list, dtype="float32")
    elif w >= 1.0 - EPS:
        print("⚙️ Embedding all question-texts … (w≈1: skipping answers)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            q_vecs_list = list(tqdm(ex.map(lambda t: embed_one(t, args.model), q_texts), total=N))
        q_vecs = np.array(q_vecs_list, dtype="float32")
    else:
        print("⚙️ Embedding all answer-texts …")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            ans_vecs_list = list(tqdm(ex.map(lambda t: embed_one(t, args.model), ans_texts), total=N))
        ans_vecs = np.array(ans_vecs_list, dtype="float32")

        print("⚙️ Embedding all question-texts …")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            q_vecs_list = list(tqdm(ex.map(lambda t: embed_one(t, args.model), q_texts), total=N))
        q_vecs = np.array(q_vecs_list, dtype="float32")

    # 4) Blend answer-vectors + question-vectors
    print(f"Blending vectors with weight w = {w} …")
    if w <= EPS:
        # answer-only
        assert ans_vecs is not None
        blended = ans_vecs / np.linalg.norm(ans_vecs, axis=1, keepdims=True)
    elif w >= 1.0 - EPS:
        # question-only
        assert q_vecs is not None
        blended = q_vecs / np.linalg.norm(q_vecs, axis=1, keepdims=True)
    else:
        assert ans_vecs is not None and q_vecs is not None
        ans_norm = ans_vecs / np.linalg.norm(ans_vecs, axis=1, keepdims=True)
        q_norm   = q_vecs   / np.linalg.norm(q_vecs,   axis=1, keepdims=True)
        blended  = (1.0 - w) * ans_norm + w * q_norm
        blended  = blended / np.linalg.norm(blended, axis=1, keepdims=True)

    # 5) Build and save FAISS index from blended vectors
    print("Building FAISS index …")
    out_dir = Path(args.output)
    save_index(blended, out_dir)
    save_metadata(ids, out_dir)

    print(f"✅ Done — wrote {out_dir/'faiss.index'} and {out_dir/'metadata.json'}")

if __name__ == "__main__":
    main()
