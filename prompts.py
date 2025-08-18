from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


def _resolve_prompts_dir() -> Path:
    """Locate the directory containing prompt template files."""
    env = os.getenv("RFP_PROMPTS_DIR")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p
    here = Path(__file__).resolve().parent / "prompts"
    if here.is_dir():
        return here
    cwdp = Path.cwd() / "prompts"
    if cwdp.is_dir():
        return cwdp
    # Fallback still returns the 'here' path (reads will raise FileNotFoundError if missing)
    return Path(__file__).resolve().parent / "prompts"

PROMPTS_DIR = _resolve_prompts_dir()


def read_prompt(name: str, default: str = "") -> str:
    """Read a prompt template from PROMPTS_DIR or return default."""
    p = PROMPTS_DIR / f"{name}.txt"
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return default


def load_prompts(defaults: Dict[str, str]) -> Dict[str, str]:
    """Load multiple prompt templates given a mapping of name->default."""
    return {k: read_prompt(k, v) for k, v in defaults.items()}
