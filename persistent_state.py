from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Dict, Any


_STATE_DIR = Path(".app_state")
_STATE_DIR.mkdir(parents=True, exist_ok=True)


def _path_for(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "default") if c.isalnum() or c in ("-", "_"))[:64]
    return _STATE_DIR / f"latest_doc_run_{safe}.json"


def load_latest_doc_run(session_id: str) -> Optional[Dict[str, Any]]:
    p = _path_for(session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_latest_doc_run(session_id: str, run_context: Dict[str, Any]) -> None:
    p = _path_for(session_id)
    try:
        p.write_text(json.dumps(run_context, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # Best-effort persistence; ignore failures silently.
        pass


def clear_latest_doc_run(session_id: str) -> None:
    p = _path_for(session_id)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        # Best-effort cleanup; ignore failures silently.
        pass
