from __future__ import annotations

import os
from typing import List, Dict, Any, Optional, Callable

from openpyxl import load_workbook
from openpyxl.comments import Comment


def _to_text_and_citations(ans: object) -> tuple[str, Dict[str, str]]:
    """Normalize answer objects to (text, {cit_num -> snippet})."""
    if isinstance(ans, dict):
        text = str(ans.get("text", ""))
        raw = ans.get("citations") or {}
        cits: Dict[str, str] = {}
        for k, v in raw.items():
            key = str(k)
            if isinstance(v, dict):
                cits[key] = v.get("text") or v.get("snippet") or v.get("content") or str(v)
            else:
                cits[key] = str(v)
        return text, cits
    return str(ans or ""), {}


def write_excel_answers(
    schema: List[Dict[str, Any]],
    answers: List[object],
    source_xlsx_path: str,
    out_xlsx_path: str,
    *,
    mode: str = "fill",  # "fill" | "replace" | "append"
    generator: Optional[Callable[..., object]] = None,
    include_comments: Optional[bool] = None,  # defaults to env RFP_INCLUDE_COMMENTS
) -> Dict[str, int]:
    """
    Drop‑in replacement for the old CLI helper. Applies answers into the Excel file.

    schema[i] is expected to include:
      - 'sheet' (or 'sheet_name')
      - 'answer_cell' (if omitted we fall back to 'question_cell'; if set to
        ``None`` the answer will be skipped)
      - 'question_text' (used only if we need to generate a missing answer)

    answers[i] can be a string or a dict like:
      {"text": "...", "citations": {1: "snippet", 2: "snippet", ...}}
    """
    inc_comments = (
        (os.getenv("RFP_INCLUDE_COMMENTS", "1") == "1")
        if include_comments is None
        else bool(include_comments)
    )

    wb = load_workbook(source_xlsx_path)
    ws_by_name = {ws.title: ws for ws in wb.worksheets}

    applied = 0
    skipped = 0

    # Ensure answers aligns with schema (allow None entries and generate if a generator is provided)
    if len(answers) < len(schema):
        answers = answers + [None] * (len(schema) - len(answers))

    for idx, ent in enumerate(schema):
        sheet_name = ent.get("sheet") or ent.get("sheet_name")
        if "answer_cell" in ent:
            addr = ent.get("answer_cell")
        else:
            addr = ent.get("question_cell") or ent.get("cell") or ent.get("address")

        if not sheet_name or not addr:
            if sheet_name and ent.get("answer_cell") is None:
                qtxt = (ent.get("question_text") or "").strip()
                print(
                    f"Skipping answer for '{qtxt}' on sheet '{sheet_name}': no answer slot"
                )
            skipped += 1
            continue

        ws = ws_by_name.get(sheet_name) or wb.active
        cell = ws[addr]

        ans = answers[idx]
        if ans is None and generator:
            q = (ent.get("question_text") or "").strip()
            ans = generator(q)

        text, citations = _to_text_and_citations(ans)

        if mode == "replace":
            cell.value = text
        elif mode == "append":
            prior = cell.value or ""
            cell.value = (prior + ("\n" if prior else "") + text)
        else:  # "fill" (default)
            # If cell is blank write; otherwise append on a new line to avoid clobbering
            if cell.value is None or str(cell.value).strip() == "":
                cell.value = text
            else:
                cell.value = f"{cell.value}\n{text}"

        # Wrap long text
        try:
            cell.alignment = cell.alignment.copy(wrap_text=True)
        except Exception:
            pass

        # Put citations into a single Excel comment
        if inc_comments and citations:
            try:
                comment_txt = "\n\n".join(str(v) for v in citations.values())
                cell.comment = Comment(comment_txt, "RFPBot")
            except Exception:
                pass

        applied += 1

    wb.save(out_xlsx_path)
    return {"applied": applied, "skipped": skipped, "total": len(schema)}


__all__ = ["write_excel_answers"]

