#!/usr/bin/env python3
"""
Thin facade around ``scripts.cli_streamlit_app`` mirroring Streamlit behaviours.

Usage examples:

    python -m scripts.streamlit_app_cli document path/to/file.docx
    python -m scripts.streamlit_app_cli ask --question "Describe your risk framework"

The CLI subcommands and flags are identical to ``cli_streamlit_app.py``; this
wrapper simply provides a name that makes it obvious the tool exercises the
same flows as ``streamlit_app.py``.
"""

from __future__ import annotations

import sys
from typing import Sequence

from scripts.cli_streamlit_app import main as _cli_main


def main(argv: Sequence[str] | None = None) -> None:
    """Delegate to ``cli_streamlit_app.main`` with the provided arguments."""
    if argv is None:
        argv = sys.argv[1:]
    _cli_main(argv)


if __name__ == "__main__":
    main()

