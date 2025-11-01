from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable


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
    filename = f"{name}.txt"

    # Direct lookup (legacy flat structure)
    direct_path = PROMPTS_DIR / filename
    if direct_path.exists():
        try:
            return direct_path.read_text(encoding="utf-8")
        except Exception:
            return default

    # Recursive lookup within nested prompt folders
    try:
        match = next(p for p in _iter_prompt_files() if p.name == filename)
        return match.read_text(encoding="utf-8")
    except StopIteration:
        return default


def _iter_prompt_files() -> Iterable[Path]:
    for path in PROMPTS_DIR.rglob("*.txt"):
        if path.is_file():
            yield path


def load_prompts(defaults: Dict[str, str]) -> Dict[str, str]:
    """Load multiple prompt templates given a mapping of name->default."""
    return {k: read_prompt(k, v) for k, v in defaults.items()}
