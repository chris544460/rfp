from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Color
from openpyxl.utils import get_column_letter


def _color_to_hex(color: Optional[Color]) -> Optional[str]:
    """Convert an openpyxl Color to a RGB hex string.

    openpyxl colors may include an alpha channel (ARGB).  This helper
    normalizes them to ``RRGGBB`` if possible.  If the color is theme- or
    indexed-based, ``None`` is returned.
    """
    if color is None:
        return None
    if getattr(color, "type", None) != "rgb":
        return None
    rgb = getattr(color, "rgb", None)
    if not isinstance(rgb, str):
        return None
    # ``rgb`` is usually ``AARRGGBB``; drop the alpha channel if present
    if len(rgb) == 8:
        rgb = rgb[2:]
    return rgb


QUESTION_PHRASES = (
    "please describe", "please provide", "explain", "detail", "outline",
    "how do you", "how will you", "what is your", "what are your", "do you",
    "can you", "does your", "have you", "who", "when", "where", "why", "which"
)


def _looks_like_question_text(t: str) -> bool:
    raw = (t or "").strip()
    if not raw:
        return False
    if "?" in raw:
        return True
    low = raw.lower()
    if any(low.startswith(p) for p in QUESTION_PHRASES):
        return True
    if any(p in low for p in QUESTION_PHRASES):
        return True
    if low.startswith(("question:", "prompt:", "rfp question:")):
        return True
    return False


def _is_blank_cell(cell) -> bool:
    v = cell.value
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return True
        if re.fullmatch(r"_+\s*", s):
            return True
        if re.fullmatch(r"\[(?:insert|enter|provide)[^\]]*\]", s.lower()):
            return True
    return False


def _addr(col_idx: int, row_idx: int) -> str:
    return f"{get_column_letter(col_idx)}{row_idx}"


def _two_col_header(ws) -> Optional[Tuple[int, int]]:
    """Return (q_col, a_col) if we detect a 'Question'/'Answer' header row."""
    if ws.max_row < 2 or ws.max_column < 2:
        return None
    # Scan first 5 rows for headers
    scan_rows = min(ws.max_row, 5)
    for r in range(1, scan_rows + 1):
        for c in range(1, ws.max_column):
            left = (ws.cell(r, c).value or "").strip().lower() if isinstance(ws.cell(r, c).value, str) else str(ws.cell(r, c).value or "").lower()
            right = (ws.cell(r, c + 1).value or "").strip().lower() if isinstance(ws.cell(r, c + 1).value, str) else str(ws.cell(r, c + 1).value or "").lower()
            if ("question" in left and "answer" in right) or ("prompt" in left and "response" in right):
                return (c, c + 1)
    return None


def extract_schema_from_xlsx(path: str) -> List[Dict[str, Any]]:
    """
    Return a list[dict] with:
      {
        'sheet': <sheet name>,
        'question_cell': 'A10',
        'question_text': 'Please describe...',
        'answer_cell': 'B10',
        'detector': 'two_col_header' | 'right_blank' | 'below_blank' | 'inline_pair',
        'confidence': float,
      }
    """
    wb = load_workbook(path, data_only=True)
    schema: List[Dict[str, Any]] = []

    for ws in wb.worksheets:
        # 1) Prefer 2‑column Q/A tables with headers
        two_col = _two_col_header(ws)
        if two_col:
            q_col, a_col = two_col
            # start from the row after the header
            for r in range(2, ws.max_row + 1):
                qv = ws.cell(r, q_col).value
                av = ws.cell(r, a_col).value
                qtxt = str(qv).strip() if qv is not None else ""
                if not qtxt:
                    continue
                if not _looks_like_question_text(qtxt):
                    # still allow explicit Q column phrasing
                    if not re.search(r"[.?]$", qtxt) and not any(p in qtxt.lower() for p in QUESTION_PHRASES):
                        continue
                # Only create a slot if the answer cell is empty/blank
                if av is None or (isinstance(av, str) and av.strip() == ""):
                    schema.append({
                        "sheet": ws.title,
                        "question_cell": _addr(q_col, r),
                        "question_text": qtxt,
                        "answer_cell": _addr(a_col, r),
                        "detector": "two_col_header",
                        "confidence": 0.85,
                    })

        # 2) Heuristic scan: single question cell with blank neighbor right/below
        #    (skip rows already captured by the header scan)
        for r in range(1, ws.max_row + 1):
            for c in range(1, ws.max_column + 1):
                cell = ws.cell(r, c)
                val = cell.value
                if not isinstance(val, str):
                    continue
                txt = val.strip()
                if not _looks_like_question_text(txt):
                    continue

                # (a) Prefer right neighbor if blank
                if c + 1 <= ws.max_column and _is_blank_cell(ws.cell(r, c + 1)):
                    schema.append({
                        "sheet": ws.title,
                        "question_cell": _addr(c, r),
                        "question_text": txt,
                        "answer_cell": _addr(c + 1, r),
                        "detector": "right_blank",
                        "confidence": 0.75,
                    })
                    continue

                # (b) Otherwise choose cell below if blank
                if r + 1 <= ws.max_row and _is_blank_cell(ws.cell(r + 1, c)):
                    schema.append({
                        "sheet": ws.title,
                        "question_cell": _addr(c, r),
                        "question_text": txt,
                        "answer_cell": _addr(c, r + 1),
                        "detector": "below_blank",
                        "confidence": 0.7,
                    })
                    continue

                # (c) Inline pairs like "Question:" in one cell and "Answer:" next cell
                if c + 1 <= ws.max_column:
                    nxt = ws.cell(r, c + 1).value
                    if isinstance(nxt, str) and nxt.strip() == "":
                        schema.append({
                            "sheet": ws.title,
                            "question_cell": _addr(c, r),
                            "question_text": txt,
                            "answer_cell": _addr(c + 1, r),
                            "detector": "inline_pair",
                            "confidence": 0.65,
                        })

    return schema


# Back‑compat alias so the CLI can import ask_sheet_schema from rfp
def ask_sheet_schema(xlsx_path: str) -> List[Dict[str, Any]]:
    return extract_schema_from_xlsx(xlsx_path)


def extract_slots_from_xlsx(path: str) -> Dict[str, Any]:
    """Extract text and formatting from an .xlsx file.

    The returned structure mirrors the DOCX slot finder in spirit but is
    simplified.  It returns a dictionary with ``doc_type`` set to
    ``"xlsx"`` and a ``sheets`` list, where each sheet contains a list of
    populated cells.  Each cell records its address (e.g. ``A1``), the
    text value, and basic formatting attributes (font color, bold,
    italic, background color, and border styles).
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    sheets: List[Dict[str, Any]] = []
    for ws in wb.worksheets:
        cells: List[Dict[str, Any]] = []
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                cell_info: Dict[str, Any] = {
                    "address": cell.coordinate,
                    "row": cell.row,
                    "column": get_column_letter(cell.column),
                    "value": str(cell.value),
                    "font_color": _color_to_hex(cell.font.color),
                    "bold": bool(cell.font.bold),
                    "italic": bool(cell.font.italic),
                    "bg_color": _color_to_hex(cell.fill.start_color),
                    "border": {
                        "left": cell.border.left.style,
                        "right": cell.border.right.style,
                        "top": cell.border.top.style,
                        "bottom": cell.border.bottom.style,
                    },
                }
                cells.append(cell_info)
        sheets.append({"name": ws.title, "cells": cells})
    return {"doc_type": "xlsx", "file": os.path.basename(path), "sheets": sheets}


__all__ = ["extract_slots_from_xlsx", "extract_schema_from_xlsx", "ask_sheet_schema"]
