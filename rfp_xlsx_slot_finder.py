from __future__ import annotations

import json
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


def _extract_schema_from_xlsx_heuristic(path: str, debug: bool = True) -> List[Dict[str, Any]]:
    """Heuristic XLSX question/answer slot detection.

    This is the original implementation that scans for question cells and
    adjacent blank cells using pattern matching and positional heuristics.
    The function is kept for backwards compatibility and as a fallback when
    the LLM based pipeline is unavailable.

    Parameters
    ----------
    path:
        Path to the workbook on disk.
    debug:
        When ``True`` (the default) verbose status messages are printed
        describing how questions are detected.

    Returns
    -------
    list[dict]
        Each entry contains ``sheet``, ``question_cell``, ``question_text``,
        ``answer_cell``, ``detector`` and ``confidence`` keys describing a
        potential question/answer pair.  If no empty answer cell is
        detected next to a question, an entry is still produced with
        ``answer_cell`` set to ``None`` so that callers may decide how to
        handle it downstream.
    """
    if debug:
        print(f"Opening workbook: {path}")
    wb = load_workbook(path, data_only=True)
    schema: List[Dict[str, Any]] = []

    for ws in wb.worksheets:
        if debug:
            print(f"Scanning sheet '{ws.title}'")

        # 1) Prefer 2‑column Q/A tables with headers
        two_col = _two_col_header(ws)
        if two_col:
            q_col, a_col = two_col
            if debug:
                print(f"  Found Q/A header columns {q_col}/{a_col}")
            # start from the row after the header
            for r in range(2, ws.max_row + 1):
                qv = ws.cell(r, q_col).value
                av = ws.cell(r, a_col).value
                qtxt = str(qv).strip() if qv is not None else ""
                if debug:
                    print(f"    Row {r}: Q='{qtxt}' A='{av}'")
                if not qtxt:
                    continue
                if not _looks_like_question_text(qtxt):
                    # still allow explicit Q column phrasing
                    if not re.search(r"[.?]$", qtxt) and not any(p in qtxt.lower() for p in QUESTION_PHRASES):
                        if debug:
                            print("      Not a question – skipping")
                        continue
                # Record the question even if the answer cell already
                # contains text so we can surface it to downstream
                # components.  When the answer cell is blank we point to it;
                # otherwise ``answer_cell`` is ``None`` and writers should
                # skip filling.
                if av is None or (isinstance(av, str) and av.strip() == ""):
                    if debug:
                        print(f"      Added slot at {_addr(q_col, r)}->{_addr(a_col, r)}")
                    schema.append({
                        "sheet": ws.title,
                        "question_cell": _addr(q_col, r),
                        "question_text": qtxt,
                        "answer_cell": _addr(a_col, r),
                        "detector": "two_col_header",
                        "confidence": 0.85,
                    })
                else:
                    if debug:
                        print(
                            f"      Answer cell {_addr(a_col, r)} is not blank; recording question without slot"
                        )
                    schema.append(
                        {
                            "sheet": ws.title,
                            "question_cell": _addr(q_col, r),
                            "question_text": qtxt,
                            "answer_cell": None,
                            "detector": "two_col_header",
                            "confidence": 0.5,
                        }
                    )
        elif debug:
            print("  No two-column header found")

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
                if debug:
                    print(f"    Question-like cell {ws.title}:{_addr(c, r)} -> '{txt}'")

                matched = False

                # (a) Prefer right neighbor if blank
                if c + 1 <= ws.max_column and _is_blank_cell(ws.cell(r, c + 1)):
                    if debug:
                        print(f"      Right neighbor {_addr(c+1, r)} is blank")
                    schema.append({
                        "sheet": ws.title,
                        "question_cell": _addr(c, r),
                        "question_text": txt,
                        "answer_cell": _addr(c + 1, r),
                        "detector": "right_blank",
                        "confidence": 0.75,
                    })
                    matched = True

                # (b) Otherwise choose cell below if blank
                elif r + 1 <= ws.max_row and _is_blank_cell(ws.cell(r + 1, c)):
                    if debug:
                        print(f"      Below neighbor {_addr(c, r+1)} is blank")
                    schema.append({
                        "sheet": ws.title,
                        "question_cell": _addr(c, r),
                        "question_text": txt,
                        "answer_cell": _addr(c, r + 1),
                        "detector": "below_blank",
                        "confidence": 0.7,
                    })
                    matched = True

                # (c) Inline pairs like "Question:" in one cell and "Answer:" next cell
                elif c + 1 <= ws.max_column:
                    nxt = ws.cell(r, c + 1).value
                    if isinstance(nxt, str) and nxt.strip() == "":
                        if debug:
                            print(f"      Inline pair with empty cell {_addr(c+1, r)}")
                        schema.append({
                            "sheet": ws.title,
                            "question_cell": _addr(c, r),
                            "question_text": txt,
                            "answer_cell": _addr(c + 1, r),
                            "detector": "inline_pair",
                            "confidence": 0.65,
                        })
                        matched = True

                if not matched:
                    if debug:
                        print(
                            "      No adjacent blank cell found; recording question without answer slot"
                        )
                    schema.append(
                        {
                            "sheet": ws.title,
                            "question_cell": _addr(c, r),
                            "question_text": txt,
                            "answer_cell": None,
                            "detector": "question_only",
                            "confidence": 0.5,
                        }
                    )

        if debug:
            print(f"Finished sheet '{ws.title}', total slots: {len(schema)}")

    if debug:
        print(f"Done. Found {len(schema)} slots")
    return schema


# ---------------------------------------------------------------------------
# LLM driven pipeline
# ---------------------------------------------------------------------------


def profile_workbook(path: str) -> Dict[str, Any]:
    """Create a profile of every cell in the workbook.

    The profile captures basic style information so that subsequent LLM calls
    have rich context about how cells are formatted.  The structure returned is
    a ``dict`` mapping sheet names to a ``dict`` with ``max_row``, ``max_col``
    and a ``cells`` list containing the per-cell details.
    """

    wb = load_workbook(path, data_only=True)
    profile: Dict[str, Any] = {}
    for ws in wb.worksheets:
        cells: List[Dict[str, Any]] = []
        merged: set[Tuple[int, int]] = set()
        for rng in ws.merged_cells.ranges:
            merged.update({(r, c) for r, c in rng.cells})
        for row in ws.iter_rows():
            for cell in row:
                cells.append(
                    {
                        "row": cell.row,
                        "col": cell.column,
                        "value": cell.value,
                        "bold": bool(cell.font and cell.font.bold),
                        "italic": bool(cell.font and cell.font.italic),
                        "font": getattr(cell.font, "name", None),
                        "fill": _color_to_hex(cell.fill.start_color),
                        "border": {
                            "left": bool(cell.border.left.style),
                            "right": bool(cell.border.right.style),
                            "top": bool(cell.border.top.style),
                            "bottom": bool(cell.border.bottom.style),
                        },
                        "alignment": getattr(cell.alignment, "horizontal", None),
                        "locked": bool(getattr(cell.protection, "locked", False)),
                        "merged": (cell.row, cell.column) in merged,
                    }
                )
        profile[ws.title] = {
            "max_row": ws.max_row,
            "max_col": ws.max_column,
            "cells": cells,
        }
    return profile


def _call_llm(prompt_file: str, payload: dict, *, model: str) -> Any:
    """Helper to invoke the LLM with a prompt template and JSON payload."""

    from answer_composer import get_openai_completion

    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", prompt_file)
    with open(prompt_path, "r", encoding="utf-8") as f:
        template = f.read()
    prompt = template.replace("{{data}}", json.dumps(payload))
    content, _ = get_openai_completion(prompt, model, json_output=True)
    return json.loads(content)


def _llm_macro_regions(profile: Dict[str, Any], *, model: str) -> List[Dict[str, Any]]:
    """LLM step #1 – identify large rectangular regions in each sheet."""

    summaries = []
    for sheet, info in profile.items():
        summaries.append(
            {
                "sheet": sheet,
                "max_row": info["max_row"],
                "max_col": info["max_col"],
            }
        )
    try:
        return _call_llm("xlsx_macro_regions.txt", summaries, model=model)
    except Exception:
        return []


def _llm_zone_refinement(
    profile: Dict[str, Any], regions: List[Dict[str, Any]], *, model: str
) -> List[Dict[str, Any]]:
    """LLM step #2 – refine macro regions into potential answer zones."""

    zones: List[Dict[str, Any]] = []
    for region in regions:
        sheet_profile = profile.get(region.get("sheet"), {})
        payload = {"region": region, "cells": sheet_profile.get("cells", [])}
        try:
            zones.extend(
                _call_llm("xlsx_zone_refinement.txt", payload, model=model)
            )
        except Exception:
            continue
    return zones


def _llm_extract_candidates(
    profile: Dict[str, Any], zones: List[Dict[str, Any]], *, model: str
) -> List[Dict[str, Any]]:
    """LLM step #3 – from each zone extract candidate answer slots."""

    candidates: List[Dict[str, Any]] = []
    for zone in zones:
        sheet_profile = profile.get(zone.get("sheet"), {})
        payload = {"zone": zone, "cells": sheet_profile.get("cells", [])}
        try:
            candidates.extend(
                _call_llm("xlsx_slot_candidates.txt", payload, model=model)
            )
        except Exception:
            continue
    return candidates


def _llm_score_and_assign(
    candidates: List[Dict[str, Any]], *, model: str
) -> List[Dict[str, Any]]:
    """LLM steps #4 and #5 – score candidates and pick winners."""

    if not candidates:
        return []
    try:
        scored = _call_llm("xlsx_slot_scoring.txt", candidates, model=model)
    except Exception:
        return []

    chosen: Dict[str, Dict[str, Any]] = {}
    for cand in scored:
        qid = cand.get("question_id") or cand.get("question_cell")
        best = chosen.get(qid)
        if not best or cand.get("score", 0) > best.get("score", 0):
            chosen[qid] = cand
    return list(chosen.values())


def extract_schema_from_xlsx(
    path: str,
    debug: bool = True,
    *,
    use_llm: bool = True,
    model: str = "gpt-4o-mini",
) -> List[Dict[str, Any]]:
    """Public entry point selecting the LLM pipeline or heuristic fallback."""

    if use_llm and os.getenv("OPENAI_API_KEY"):
        try:
            profile = profile_workbook(path)
            regions = _llm_macro_regions(profile, model=model)
            zones = _llm_zone_refinement(profile, regions, model=model)
            candidates = _llm_extract_candidates(profile, zones, model=model)
            final = _llm_score_and_assign(candidates, model=model)
            if final:
                return final
        except Exception as exc:
            if debug:
                print(f"LLM pipeline failed: {exc}. Falling back to heuristic")

    return _extract_schema_from_xlsx_heuristic(path, debug=debug)


# Back‑compat alias so the CLI can import ask_sheet_schema from rfp
def ask_sheet_schema(xlsx_path: str, debug: bool = True) -> List[Dict[str, Any]]:
    """Compatibility wrapper for :func:`extract_schema_from_xlsx`.

    Parameters
    ----------
    xlsx_path:
        Path to the workbook to analyze.
    debug:
        Pass ``True`` to enable verbose debugging output (default).
    """
    return extract_schema_from_xlsx(xlsx_path, debug=debug)


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


def extract_cell_features(path: str) -> List[Dict[str, Any]]:
    """Flatten a workbook into a feature table for LLM classification.

    Each returned row has ``sheet``, ``row``, ``col``, ``cell`` address, the
    raw ``text`` value and a ``features`` dictionary containing simple style
    and text markers that may hint at questions.
    """
    wb = load_workbook(path, data_only=True)
    rows: List[Dict[str, Any]] = []
    interrogatives = (
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "do",
        "does",
        "can",
        "is",
        "are",
        "will",
        "should",
        "please",
    )
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                text = str(cell.value)
                low = text.strip().lower()
                features = {
                    "has_question_mark": "?" in text,
                    "starts_with_interrogative": bool(
                        re.match(rf"^({'|'.join(interrogatives)})\b", low)
                    ),
                    "bold": bool(cell.font and cell.font.bold),
                    "font_size": getattr(cell.font, "sz", None),
                    "fill_color": _color_to_hex(cell.fill.start_color),
                }
                rows.append(
                    {
                        "sheet": ws.title,
                        "row": cell.row,
                        "col": cell.column,
                        "cell": cell.coordinate,
                        "text": text,
                        "features": features,
                    }
                )
    return rows


def llm_classify_cells(
    cells: List[Dict[str, Any]], *, model: str = "gpt-4o-mini", batch_size: int = 8
) -> List[Dict[str, Any]]:
    """Classify cell schemas using an LLM.

    ``cells`` is expected to be the output of :func:`extract_cell_features`.
    The function batches the payload to reduce round trips.  Each batch is
    sent to an LLM prompt that labels the cells as ``QUESTION_HEADER``,
    ``QUESTION_BODY`` or ``NOT_QUESTION`` and returns a confidence score.
    """
    from answer_composer import get_openai_completion

    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "xlsx_classify_cells.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        template = f.read()

    results: List[Dict[str, Any]] = []
    for i in range(0, len(cells), batch_size):
        batch = cells[i : i + batch_size]
        payload = json.dumps(
            [
                {"cell": c["cell"], "text": c["text"], "features": c["features"]}
                for c in batch
            ]
        )
        prompt = template.replace("{{cells}}", payload)
        content, _ = get_openai_completion(prompt, model, json_output=True)
        try:
            classified = json.loads(content)
        except Exception:
            continue
        results.extend(classified)

    # merge classifications back into the original structures
    by_cell = {r.get("cell"): r for r in results}
    for row in cells:
        r = by_cell.get(row["cell"])
        if r:
            row["label"] = r.get("label")
            row["confidence"] = r.get("confidence")
    return cells


def extract_questions_with_llm(path: str, model: str = "gpt-4o-mini") -> List[Dict[str, Any]]:
    """High level convenience wrapper combining feature extraction and LLM
    classification.

    The function returns rows labelled as question headers or bodies.  It does
    not attempt sophisticated block assembly but provides a foundation for
    downstream clustering.
    """
    table = extract_cell_features(path)
    classified = llm_classify_cells(table, model=model)
    return [row for row in classified if row.get("label") in {"QUESTION_HEADER", "QUESTION_BODY"}]


__all__ = [
    "extract_slots_from_xlsx",
    "extract_schema_from_xlsx",
    "ask_sheet_schema",
    "profile_workbook",
    "extract_cell_features",
    "llm_classify_cells",
    "extract_questions_with_llm",
]
