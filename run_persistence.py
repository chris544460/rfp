"""Persistence helpers for document run contexts and artifacts.

This module saves the latest document run context (including Q/A pairs)
to a JSON file under `runs/latest_run.json`, and stores generated
artifacts (like answered DOCX/XLSX files) under `runs/artifacts/`.

The Streamlit notebook can call these helpers to persist the current
run and to restore it on app reload.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
from uuid import uuid4


BASE_DIR = Path.cwd()
RUNS_DIR = BASE_DIR / "runs"
ARTIFACTS_DIR = RUNS_DIR / "artifacts"
LATEST_FILE = RUNS_DIR / "latest_run.json"


def ensure_runs_dir() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize_filename(name: str) -> str:
    # Basic sanitization: remove path separators and control chars
    name = os.path.basename(name)
    return "".join(c for c in name if c.isprintable() and c not in "\\/:*?\"<>|") or "file"


def persist_artifact(
    key: str,
    *,
    label: str,
    data: bytes,
    file_name: str,
    mime: Optional[str] = None,
    order: int = 0,
) -> Dict[str, object]:
    """Persist an artifact to disk and return a serializable record.

    Returns a dict with: key, label, file_name, mime, order, path.
    """
    ensure_runs_dir()
    safe_name = _sanitize_filename(file_name)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    uniq = uuid4().hex[:6]
    out_name = f"{ts}_{uniq}_{safe_name}"
    out_path = ARTIFACTS_DIR / out_name
    with out_path.open("wb") as f:
        f.write(data)
    return {
        "key": key,
        "label": label,
        "file_name": file_name,
        "mime": mime,
        "order": int(order or 0),
        "path": str(out_path.relative_to(RUNS_DIR)),  # store path relative to runs/
    }


def persist_latest_run(run_context: Dict[str, object]) -> None:
    """Write the latest run context to disk (JSON)."""
    ensure_runs_dir()
    with LATEST_FILE.open("w", encoding="utf-8") as f:
        json.dump(run_context, f, ensure_ascii=False, indent=2)


def load_latest_run() -> Optional[Dict[str, object]]:
    """Load the latest run context from disk if present."""
    try:
        with LATEST_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        # If the file is corrupted or unreadable, ignore silently.
        return None


def load_artifacts(run_context: Dict[str, object]) -> List[Dict[str, object]]:
    """Return a list of artifact dicts with bytes for download hydration.

    Each returned dict has: key, label, file_name, mime, order, data.
    """
    ensure_runs_dir()
    artifacts = []
    for rec in (run_context.get("artifacts") or []):
        try:
            rel = rec.get("path")
            if not rel:
                continue
            path = RUNS_DIR / str(rel)
            with path.open("rb") as f:
                data = f.read()
            artifacts.append(
                {
                    "key": rec.get("key") or rec.get("file_name") or str(uuid4()),
                    "label": rec.get("label") or rec.get("file_name") or "Download",
                    "file_name": rec.get("file_name") or Path(path).name,
                    "mime": rec.get("mime"),
                    "order": int(rec.get("order") or 0),
                    "data": data,
                }
            )
        except Exception:
            # Skip missing/broken artifacts silently.
            continue
    return artifacts

