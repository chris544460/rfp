"""Placeholder apply-answers module for .xlsx files.

Currently only extraction of text/formatting is supported.  This module
exists so that ``rfp_handlers`` can register an answer applier for the
``.xlsx`` extension.  The function defined here simply raises
``NotImplementedError``.
"""
from __future__ import annotations

from typing import Dict


def apply_answers_to_xlsx(xlsx_path: str, slots_json: str, answers_json: str, out_path: str) -> Dict[str, str]:
    raise NotImplementedError("Excel answer application not yet implemented")


__all__ = ["apply_answers_to_xlsx"]
