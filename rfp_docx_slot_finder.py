#!/usr/bin/env python3
from __future__ import annotations
# Requires: export OPENAI_API_KEY=...  (and optionally OPENAI_MODEL=gpt-5-nano)
# rfp_docx_slot_finder.py

DEBUG = False
SHOW_TEXT = False  # when True, print full prompt/completion payloads

# ────────────── Token/cost tracking and pricing map ──────────────
TOTAL_INPUT_TOKENS = 0
TOTAL_OUTPUT_TOKENS = 0
TOTAL_COST_USD = 0.0

# very approximate per‑1K‑token costs in USD (Aug 2025 public pricing)
# GPT-5 nano: the fastest, cheapest version of GPT-5—great for summarization and classification tasks
MODEL_PRICING = {
    # $0.05 input / $0.005 cached input / $0.40 output per million tokens
    "gpt-5-nano": {"in": 0.00005, "out": 0.0004, "cached_in": 0.000005},
    "gpt-4o": {"in": 0.005, "out": 0.015},          # $5 / $15 per million
    "gpt-4o-mini": {"in": 0.003, "out": 0.009},     # hypothetical mini tier
    "gpt-4o-max": {"in": 0.007, "out": 0.021},      # hypothetical tier
}

def _record_usage(model: str, usage: Dict[str, int]):
    """Accumulate token usage and cost into globals."""
    global TOTAL_INPUT_TOKENS, TOTAL_OUTPUT_TOKENS, TOTAL_COST_USD
    prompt_toks = usage.get("prompt_tokens", 0)
    compl_toks = usage.get("completion_tokens", 0)
    TOTAL_INPUT_TOKENS += prompt_toks
    TOTAL_OUTPUT_TOKENS += compl_toks
    price = MODEL_PRICING.get(model, MODEL_PRICING.get("gpt-5-nano"))
    cost = (prompt_toks / 1000) * price["in"] + (compl_toks / 1000) * price["out"]
    TOTAL_COST_USD += cost
    if DEBUG:
        dbg(f"Cost for call [{model}]: input {prompt_toks} tok, output {compl_toks} tok → ${cost:.6f}")

def dbg(msg: str):
    if DEBUG:
        print(f"[DEBUG] {msg}")
import os, re, json, uuid, argparse
import sys
import math
import asyncio
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any, Tuple, Union

import docx
from docx.text.paragraph import Paragraph
from docx.table import Table

# ───────────────────────── models ─────────────────────────

@dataclass
class AnswerLocator:
    type: str  # "paragraph_after" | "paragraph" | "table_cell"
    paragraph_index: Optional[int] = None   # for paragraph / paragraph_after
    offset: int = 0                         # how many paragraphs after the anchor
    table_index: Optional[int] = None       # for table_cell
    row: Optional[int] = None
    col: Optional[int] = None

@dataclass
class QASlot:
    id: str
    question_text: str
    answer_locator: AnswerLocator
    answer_type: str = "text"  # text | checkbox | multi-select | date | number
    confidence: float = 0.5
    meta: Dict[str, Any] = None

# ───────────────────────── utils ─────────────────────────

def _iter_block_items(doc: docx.document.Document):
    """Yield paragraphs and tables in document order."""
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    parent = doc.element.body
    for child in parent.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, doc)
        elif isinstance(child, CT_Tbl):
            yield Table(child, doc)

def _is_blank_para(p: Paragraph) -> bool:
    t = (p.text or "").strip()
    if t == "":
        return True
    # underscore lines or placeholders like [Insert response]
    if re.fullmatch(r"_+\s*", t):
        return True
    if re.fullmatch(r"\[(?:insert|enter|provide)[^\]]*\]", t.lower()):
        return True
    # check if any run is explicitly underlined without real text
    if any(r.text and r.underline for r in p.runs) and len(t.replace("_","").strip()) == 0:
        return True
    return False

QUESTION_PHRASES = (
    "please describe", "please provide", "explain", "detail", "outline",
    "how do you", "how will you", "what is your", "what are your", "do you",
    "can you", "does your", "have you", "who", "when", "where", "why", "which"
)

# ─────────────────── outline / context helpers ───────────────────
_ENUM_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:\(?\d+(?:\.\d+)*\)?[.)]?)|"     # 1   1.1   2.3.4   (1)   1)
    r"(?:[A-Za-z][.)])|"                      # a)   A)   a.   A.
    r"(?:\([A-Za-z0-9]+\))"                 # (a)  (A)  (i)  (1)
    r")\s+"
)

def strip_enum_prefix(text: str) -> str:
    """Remove a single leading enumeration token like '1.1 ', '(a) ', 'A) '."""
    return _ENUM_PREFIX_RE.sub("", (text or ""), count=1)

def derive_outline_hint_and_level(text: str):
    """
    Return (hint, level) derived from visible enumeration.
    Examples: '1.1'→('1.1',2) ; '3)'→('3',1) ; '(a)'→('a',1)
    """
    t = (text or "").strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)+)[.)]?\s+", t)
    if m:
        num = m.group(1)
        return num, num.count(".") + 1
    m = re.match(r"^\s*\(?(\d+)\)?[.)]?\s+", t)
    if m:
        return m.group(1), 1
    m = re.match(r"^\s*\(?([A-Za-z])\)?[.)]?\s+", t)
    if m:
        return m.group(1), 1
    return None, None

def paragraph_level_from_numbering(p: Paragraph):
    """Return Word automatic numbering level (1‑based) if present."""
    try:
        numPr = p._p.pPr.numPr
        if numPr is None:
            return None
        ilvl = numPr.ilvl.val if numPr.ilvl is not None else None
        if ilvl is None:
            return None
        return int(ilvl) + 1
    except Exception:
        return None

def heading_chain(blocks, upto_block, max_back=80):
    """Return list of Heading‑style texts (top→nearest) above a block index."""
    chain = []
    for bi in range(max(0, upto_block - max_back), upto_block):
        b = blocks[bi]
        if isinstance(b, Paragraph):
            try:
                style = (b.style.name or "").lower()
            except Exception:
                style = ""
            if style.startswith("heading"):
                txt = (b.text or "").strip()
                if txt:
                    chain.append(txt)
    return chain

def _looks_like_question(text: str) -> bool:
    t_raw = (text or "").strip()
    if not t_raw:
        return False

    # Presence of a question mark anywhere is a strong signal
    if "?" in t_raw:
        return True

    # Remove enumeration prefix (e.g. '1.1 ', '(a) ') before heuristic checks
    t = strip_enum_prefix(t_raw).strip()
    t_lower = t.lower()

    # Question/request cues at the start of the text
    if any(t_lower.startswith(phrase) for phrase in QUESTION_PHRASES):
        return True

    # Question/request cues appearing anywhere in the text
    if any(phrase in t_lower for phrase in QUESTION_PHRASES):
        return True

    if t_raw.lower().startswith(("question:", "prompt:", "rfp question:")):
        return True

    # If original had an enumeration token and contains a cue anywhere
    if _ENUM_PREFIX_RE.match(t_raw) and any(phrase in t_lower for phrase in QUESTION_PHRASES):
        return True

    return False

def _para_style_name(p: Paragraph) -> str:
    try:
        return p.style.name or ""
    except Exception:
        return ""

def _table_cell_text(table: Table, r: int, c: int) -> str:
    try:
        return (table.cell(r, c).text or "").strip()
    except Exception:
        return ""

# ─────────────────── rich extraction helpers (format-aware) ───────────────────

def _run_to_markup(r) -> str:
    t = (r.text or "")
    if not t:
        return ""
    # Wrap with simple tags to preserve formatting signal
    if r.underline:
        t = f"<u>{t}</u>"
    if r.italic:
        t = f"<i>{t}</i>"
    if r.bold:
        t = f"<b>{t}</b>"
    return t

def _paragraph_rich_text(p: Paragraph) -> str:
    if not p.runs:
        return (p.text or "")
    parts = []
    for r in p.runs:
        parts.append(_run_to_markup(r))
    return "".join(parts) or (p.text or "")

def _cell_rich_text(cell) -> str:
    # Join paragraphs inside a cell, preserving run markup
    lines = []
    for par in cell.paragraphs:
        lines.append(_paragraph_rich_text(par))
    return "\n".join(lines).strip()

def _para_alignment(p: Paragraph) -> str:
    try:
        al = p.alignment
        if al is None:
            return "NONE"
        return str(al)  # Enum repr is fine
    except Exception:
        return "NONE"

def _para_num_info(p: Paragraph) -> Tuple[Optional[int], Optional[int]]:
    """
    Returns (numId, ilvl) if numbering is present, else (None, None).
    """
    try:
        numPr = p._p.pPr.numPr  # type: ignore
        if numPr is None:
            return (None, None)
        numId = numPr.numId.val if numPr.numId is not None else None
        ilvl = numPr.ilvl.val if numPr.ilvl is not None else None
        return (numId, ilvl)
    except Exception:
        return (None, None)

def _render_rich_excerpt(blocks: List[Union[Paragraph, Table]]) -> Tuple[str, Dict[int, int]]:
    """
    Produce a linearized, format-aware representation with global block indices.
    For tables, also return a map {global_block_index -> 0-based table_index}.
    """
    lines: List[str] = []
    table_idx_map: Dict[int, int] = {}
    running_table_index = 0
    for gi, b in enumerate(blocks):
        if isinstance(b, Paragraph):
            style = _para_style_name(b)
            align = _para_alignment(b)
            numId, ilvl = _para_num_info(b)
            rich = _paragraph_rich_text(b)
            text = (b.text or "").strip()
            lines.append(f"B[{gi}] PARAGRAPH style='{style}' align={align} numId={numId} ilvl={ilvl}")
            lines.append(f"B[{gi}] TEXT: {rich if rich else text}")
        elif isinstance(b, Table):
            # Table header line
            try:
                rows = len(b.rows)
                cols = len(b.columns)
            except Exception:
                rows, cols = 0, 0
            table_idx_map[gi] = running_table_index
            lines.append(f"B[{gi}] TABLE rows={rows} cols={cols} (table_index={running_table_index})")
            for r in range(rows):
                row_line = []
                for c in range(cols):
                    try:
                        cell_text = _cell_rich_text(b.cell(r, c))
                    except Exception:
                        cell_text = ""
                    lines.append(f"B[{gi}] [{r},{c}] TEXT: {cell_text}")
            running_table_index += 1
    excerpt = "\n".join(lines)
    return excerpt, table_idx_map

# ─────────────────── rule-based detectors ───────────────────

def detect_para_question_with_blank(blocks: List[Union[Paragraph, Table]]) -> List[QASlot]:
    slots: List[QASlot] = []
    p_index = -1  # count paragraphs so we can locate
    for i, b in enumerate(blocks):
        if isinstance(b, Paragraph):
            p_index += 1
            text = (b.text or "").strip()
            if not _looks_like_question(text):
                continue

            # Look ahead up to 3 blocks for blank area or underscores or a 1x1 empty table
            conf_base = 0.6
            style = _para_style_name(b)
            if "Question" in style:
                conf_base += 0.1

            # 1) blank paragraphs
            for j in range(1, 4):
                if i + j >= len(blocks):
                    break
                nb = blocks[i + j]
                if isinstance(nb, Paragraph) and _is_blank_para(nb):
                    lvl_num = paragraph_level_from_numbering(b)
                    hint, hint_level = derive_outline_hint_and_level(text)
                    ctx_level = lvl_num or hint_level
                    slots.append(QASlot(
                        id=f"slot_{uuid.uuid4().hex[:8]}",
                        question_text=text,
                        answer_locator=AnswerLocator(type="paragraph", paragraph_index=p_index + j),
                        confidence=min(0.95, conf_base + 0.2),
                        meta={
                            "detector": "para_blank_after",
                            "q_paragraph_index": p_index,
                            "q_block": i,
                            "q_style": style,
                            "outline": {"level": ctx_level, "hint": hint}
                        }
                    ))
                    break
                # 2) immediate empty 1x1 table (used as a box to type into)
                if isinstance(nb, Table):
                    try:
                        if len(nb.rows) == 1 and len(nb.columns) == 1:
                            cell_text = (nb.cell(0, 0).text or "").strip()
                            if cell_text == "":
                                # need the running table index for locator
                                t_idx = _running_table_index(blocks, i + j)
                                lvl_num = paragraph_level_from_numbering(b)
                                hint, hint_level = derive_outline_hint_and_level(text)
                                ctx_level = lvl_num or hint_level
                                slots.append(QASlot(
                                    id=f"slot_{uuid.uuid4().hex[:8]}",
                                    question_text=text,
                                    answer_locator=AnswerLocator(
                                        type="table_cell", table_index=t_idx, row=0, col=0
                                    ),
                                    confidence=min(0.9, conf_base + 0.15),
                                    meta={
                                        "detector": "para_then_empty_1x1_table",
                                        "q_paragraph_index": p_index,
                                        "q_block": i,
                                        "outline": {"level": ctx_level, "hint": hint}
                                    }
                                ))
                                break
                    except Exception:
                        pass
    return slots

def _running_table_index(blocks: List[Union[Paragraph, Table]], upto: int) -> int:
    """Return table index counting from 0 in document order up to position `upto`."""
    t = 0
    for k, b in enumerate(blocks[:upto+1]):
        if isinstance(b, Table):
            if k == upto:
                return t
            t += 1
    return t

def detect_two_col_table_q_blank(blocks: List[Union[Paragraph, Table]]) -> List[QASlot]:
    slots: List[QASlot] = []
    table_counter = -1
    for b in blocks:
        if isinstance(b, Table):
            table_counter += 1
            if len(b.columns) != 2:
                continue
            # Header detection
            header_left = _table_cell_text(b, 0, 0).lower()
            header_right = _table_cell_text(b, 0, 1).lower()
            has_header = any(k in header_left for k in ("question", "prompt")) or any(k in header_right for k in ("answer", "response"))
            start_row = 1 if has_header else 0

            for r in range(start_row, len(b.rows)):
                left = _table_cell_text(b, r, 0)
                right = _table_cell_text(b, r, 1)
                if not left:
                    continue
                # Question-like left, empty right
                if _looks_like_question(left) and right == "":
                    conf = 0.8 if has_header else 0.7
                    slots.append(QASlot(
                        id=f"slot_{uuid.uuid4().hex[:8]}",
                        question_text=left.strip(),
                        answer_locator=AnswerLocator(type="table_cell", table_index=table_counter, row=r, col=1),
                        confidence=conf,
                        meta={"detector": "table_2col_q_left_blank_right", "has_header": has_header}
                    ))
    return slots

def detect_response_label_then_blank(blocks: List[Union[Paragraph, Table]]) -> List[QASlot]:
    slots: List[QASlot] = []
    p_index = -1
    for i, b in enumerate(blocks):
        if isinstance(b, Paragraph):
            p_index += 1
            t = (b.text or "").strip()
            # Match patterns like "Response:" or "Answer:"
            if re.match(r"^(Response|Answer)\s*:\s*$", t, flags=re.IGNORECASE):
                # backtrack to find nearest prior question paragraph within 3 items
                q_text, q_idx = None, None
                back_p_idx = p_index
                for k in range(1, 4):
                    j = i - k
                    if j < 0: break
                    prev = blocks[j]
                    if isinstance(prev, Paragraph):
                        back_p_idx -= 1
                        if _looks_like_question((prev.text or "").strip()):
                            q_text = (prev.text or "").strip()
                            q_idx = back_p_idx
                            break
                # look forward to find first blank paragraph
                if q_text is not None:
                    for j in range(1, 4):
                        if i + j >= len(blocks): break
                        nb = blocks[i + j]
                        if isinstance(nb, Paragraph) and _is_blank_para(nb):
                            lvl_num = paragraph_level_from_numbering(prev) if isinstance(prev, Paragraph) else None
                            hint, hint_level = derive_outline_hint_and_level(q_text)
                            ctx_level = lvl_num or hint_level
                            slots.append(QASlot(
                                id=f"slot_{uuid.uuid4().hex[:8]}",
                                question_text=q_text,
                                answer_locator=AnswerLocator(type="paragraph", paragraph_index=p_index + j),
                                confidence=0.75,
                                meta={
                                    "detector": "response_label_then_blank",
                                    "q_paragraph_index": q_idx,
                                    "q_block": (i - k),
                                    "outline": {"level": ctx_level, "hint": hint}
                                }
                            ))
                            break
    return slots

# ─────────────────── optional LLM refiner ───────────────────

USE_LLM = True  # default is ON; can be disabled with --no-ai

def llm_refine(slots: List[QASlot], context_windows: List[str]) -> List[QASlot]:
    """
    Stub that could call OpenAI to confirm/adjust low-confidence slots.
    Keep as no-op unless USE_LLM=True and you implement it.
    """
    if not USE_LLM:
        return slots
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        return slots

    refined: List[QASlot] = []
    for s, ctx in zip(slots, context_windows):
        if s.confidence >= 0.8:
            refined.append(s)
            continue
        prompt = (
            "You are given a DOCX excerpt. Identify if it contains a question and the precise "
            "location of the answer area (e.g., 'immediately after in next paragraph', "
            "'table cell: table=2,row=3,col=2'). Return JSON: "
            "{'is_question': bool, 'question_text': str, 'answer': {'kind': 'paragraph_after'|'table_cell',"
            "'offset': int, 'table_index': int|null, 'row': int|null, 'col': int|null}}.\n\n"
            f"EXCERPT:\n{ctx}"
        )
        # Minimal example using chat.completions (works widely)
        try:
            resp = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-5-nano"),
                response_format={"type": "json_object"},
                messages=[{"role":"user","content":prompt}]
            )
            if SHOW_TEXT:
                print("\n--- PROMPT (llm_refine) ---")
                print(prompt)
                print("--- COMPLETION (llm_refine) ---")
                print(resp.choices[0].message.content)
                print("--- END COMPLETION ---\n")
            try:
                _record_usage(os.getenv("OPENAI_MODEL", "gpt-5-nano"), resp.usage.model_dump())
            except Exception:
                pass
            js = json.loads(resp.choices[0].message.content)
            if js.get("is_question"):
                # You could update s.question_text and s.answer_locator with js here
                s.confidence = max(s.confidence, 0.85)
        except Exception:
            pass
        refined.append(s)
    return refined

# ─────────────────── LLM paragraph‑scan fallback ───────────────────

def llm_scan_blocks(blocks: List[Union[Paragraph, Table]], model: str = "gpt-5-nano") -> List[QASlot]:
    """If rule‑based detectors find nothing, let an LLM propose Q→A blanks."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        return []

    excerpt, table_idx_map = _render_rich_excerpt(blocks)
    dbg(f"llm_scan_blocks (rich): {len(excerpt)} chars, model={model}")
    dbg(f"Sending prompt to LLM (first 400 chars): {excerpt[:400]}...")

    instruction = (
        "You are given a linearized, format-aware representation of a .docx file.\n"
        "Each block has a global index B[i]. Blocks are PARAGRAPH or TABLE. TABLE blocks also list each cell as B[i] [r,c] TEXT: ...\n"
        "Your task: find QUESTION prompts and the associated blank answer areas.\n"
        "Valid cases include:\n"
        "  • Paragraph question followed by blank area in the next 1–3 paragraphs (or end-of-document → treat as blank area after it).\n"
        "  • Table row/column where a cell with a question pairs with an adjacent empty cell used for the answer.\n"
        "Return STRICT JSON with key 'slots' as a list. Each element must be one of:\n"
        "  {\"kind\":\"paragraph_after\",\"question\":{\"block\":int},\"answer\":{\"offset\":int}}\n"
        "  {\"kind\":\"table_cell\",\"question\":{\"block\":int,\"row\":int,\"col\":int},\"answer\":{\"block\":int,\"row\":int,\"col\":int}}\n"
        "Use the same global B[i] indices shown. If none, return {\"slots\":[]}.\n"
        "Edge case: if the document has a single paragraph ending with '?', return one paragraph_after with offset=1.\n"
    )
    if SHOW_TEXT:
        print("\n--- PROMPT (llm_scan_blocks) ---")
        print(instruction + "\n\nDOC:\n" + excerpt)
        print("--- END PROMPT ---\n")
    try:
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a precise document analyzer. Only output valid JSON."},
                {"role": "user", "content": instruction + "\n\nDOC:\n" + excerpt}
            ],
        )
        try:
            _record_usage(model, resp.usage.model_dump())
        except Exception:
            pass
        js = json.loads(resp.choices[0].message.content)
        cand = js.get("slots", []) or []
        if SHOW_TEXT:
            print("\n--- COMPLETION (llm_scan_blocks) ---")
            print(resp.choices[0].message.content)
            print("--- END COMPLETION ---\n")
        dbg(f"LLM returned {len(cand)} slot candidates")
        dbg(f"LLM raw slot candidates: {cand}")
    except Exception as e:
        dbg(f"LLM error: {e}")
        return []

    results: List[QASlot] = []
    for it in cand:
        dbg(f"Processing candidate: {it}")
        try:
            kind = (it.get("kind") or "").strip()
            if kind == "paragraph_after":
                q_block = int(it["question"]["block"])
                offset = max(1, min(3, int(it["answer"]["offset"])))
                # derive question text if possible
                if 0 <= q_block < len(blocks) and isinstance(blocks[q_block], Paragraph):
                    q_text = (blocks[q_block].text or "").strip()
                else:
                    q_text = ""
                results.append(QASlot(
                    id=f"slot_{uuid.uuid4().hex[:8]}",
                    question_text=q_text,
                    answer_locator=AnswerLocator(type="paragraph_after", paragraph_index=q_block, offset=offset),
                    confidence=0.6,
                    meta={"detector": "llm_rich", "block": q_block, "offset": offset}
                ))
                dbg(f"Appended slot from candidate: {results[-1]}")
            elif kind == "table_cell":
                q_block = int(it["question"]["block"])
                qr = int(it["question"]["row"])
                qc = int(it["question"]["col"])
                ab = int(it["answer"]["block"])
                ar = int(it["answer"]["row"])
                ac = int(it["answer"]["col"])
                # table index mapping
                t_index = table_idx_map.get(q_block)
                if t_index is None:
                    # if the model referenced a table block incorrectly, skip
                    continue
                # derive question text if possible
                q_text = ""
                try:
                    tbl = blocks[q_block]
                    if isinstance(tbl, Table):
                        q_text = (tbl.cell(qr, qc).text or "").strip()
                except Exception:
                    pass
                results.append(QASlot(
                    id=f"slot_{uuid.uuid4().hex[:8]}",
                    question_text=q_text,
                    answer_locator=AnswerLocator(type="table_cell", table_index=t_index, row=ar, col=ac),
                    confidence=0.65,
                    meta={"detector": "llm_rich", "q_block": q_block, "answer_block": ab, "row": ar, "col": ac}
                ))
                dbg(f"Appended slot from candidate: {results[-1]}")
        except Exception as e:
            dbg(f"Parse candidate error: {e}")
            continue
    return results

# ─────────────────── 2‑stage LLM helpers ───────────────────

def llm_detect_questions(blocks: List[Union[Paragraph, Table]], model: str = "gpt-5-nano") -> List[int]:
    """Return global block indices that look like questions."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        return []

    excerpt, _ = _render_rich_excerpt(blocks)
    prompt = (
        "Below is a format‑aware listing of a DOCX document. "
        "Identify every block that asks a question or requests information."
        "This includes sub‑questions, prompts without a question mark, and items that begin after tabs or numbering.\n\n"
        + excerpt
        + "\n\nReturn STRICT JSON like {\"questions\": [0, 5, 12]} (empty list if none)."
    )
    if SHOW_TEXT:
        print("\n--- PROMPT (detect_questions) ---\n" + prompt + "\n--- END PROMPT ---\n")
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        _record_usage(model, resp.usage.model_dump())
    except Exception:
        pass
    if SHOW_TEXT:
        print("\n--- COMPLETION (detect_questions) ---\n" + resp.choices[0].message.content + "\n--- END COMPLETION ---\n")
    try:
        js = json.loads(resp.choices[0].message.content)
        questions = [int(i) for i in js.get("questions", [])]
        dbg(f"Model returned JSON (detect_questions): {js}")
        dbg(f"Questions indices extracted: {questions}")
        return questions
    except Exception as e:
        dbg(f"Error parsing detect_questions response: {e}")
        return []


async def llm_locate_answer(blocks: List[Union[Paragraph, Table]], q_block: int, window: int = 3, model: str = "gpt-5-nano") -> Optional[AnswerLocator]:
    """Given a question block index, ask the LLM to pick best answer location within ±window."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        return None

    # Build context window
    start = max(0, q_block - window)
    end = min(len(blocks), q_block + window + 1)
    local_blocks = blocks[start:end]
    excerpt, table_idx_map = _render_rich_excerpt(local_blocks)
    prompt = (
        f"The following excerpt is from a DOCX. Block indices are local (start={start}).\n"
        "Identify where the ANSWER area begins for the question in local block index 0 (the first block). "
        "Return STRICT JSON either:\n"
        "  {\"kind\": \"paragraph_after\", \"offset\": int} or\n"
        "  {\"kind\": \"table_cell\", \"row\": int, \"col\": int}\n"
        "If unsure, assume paragraph_after offset=1.\n\n" +
        excerpt
    )
    if SHOW_TEXT:
        print(f"\n--- PROMPT (locate_answer q_block={q_block}) ---\n" + prompt + "\n--- END PROMPT ---\n")
    try:
        resp = await client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        dbg(f"OpenAI error (locate_answer q_block={q_block}): {e}")
        return None
    try:
        _record_usage(model, resp.usage.model_dump())
    except Exception:
        pass
    if SHOW_TEXT:
        print(
            f"\n--- COMPLETION (locate_answer q_block={q_block}) ---\n"
            + resp.choices[0].message.content
            + "\n--- END COMPLETION ---\n"
        )
    try:
        js = json.loads(resp.choices[0].message.content)
        dbg(f"Model returned JSON (locate_answer q_block={q_block}): {js}")
        kind = js.get("kind")
        if kind == "paragraph_after":
            offset = max(1, min(3, int(js.get("offset", 1))))
            locator = AnswerLocator(type="paragraph_after", paragraph_index=q_block, offset=offset)
            dbg(f"Mapped model response to AnswerLocator (paragraph_after): {locator}")
            return locator
        elif kind == "table_cell":
            row = int(js.get("row", 0))
            col = int(js.get("col", 0))
            # map local q_block (0) back to global q_block to get table index
            global_block_index = q_block
            _, table_idx_map_global = _render_rich_excerpt(blocks)
            t_index = table_idx_map_global.get(global_block_index)
            locator = AnswerLocator(type="table_cell", table_index=t_index, row=row, col=col)
            dbg(f"Mapped model response to AnswerLocator (table_cell): {locator}")
            return locator
    except Exception as e:
        dbg(f"Error parsing locate_answer for q_block {q_block}: {e}")
        return None


async def llm_assess_context(blocks: List[Union[Paragraph, Table]], q_block: int, model: str = "gpt-5-nano") -> bool:
    """Return True if the question likely depends on previous context."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        return False

    start = max(0, q_block - 2)
    end = min(len(blocks), q_block + 1)
    local_blocks = blocks[start:end]
    excerpt, _ = _render_rich_excerpt(local_blocks)
    prompt = (
        f"The following excerpt comes from a DOCX document. The candidate question is at local block index {q_block - start}. "
        "Does this question rely on the preceding text (for example, is it a follow-up or sub-question)? "
        "Return STRICT JSON like {\"needs_context\": true} or {\"needs_context\": false}.\n\n"
        + excerpt
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        dbg(f"OpenAI error (assess_context q_block={q_block}): {e}")
        return False
    try:
        _record_usage(model, resp.usage.model_dump())
    except Exception:
        pass
    try:
        js = json.loads(resp.choices[0].message.content)
        return bool(js.get("needs_context"))
    except Exception:
        return False

# ───────────────────────── pipeline ─────────────────────────

def extract_slots_from_docx(path: str) -> Dict[str, Any]:
    doc = docx.Document(path)
    blocks = list(_iter_block_items(doc))

    dbg(f"extract_slots_from_docx: USE_LLM={USE_LLM}")
    dbg(f"Total blocks: {len(blocks)}")

    # If LLM mode (USE_LLM) is active, skip all rule-based detectors entirely.
    if USE_LLM:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY not set; cannot run in pure AI mode.")
        llm_model = os.getenv("OPENAI_MODEL", "gpt-5-nano")
        q_indices = llm_detect_questions(blocks, model=llm_model)

        async def process_q_block(qb: int) -> Optional[QASlot]:
            try:
                loc = await llm_locate_answer(blocks, qb, window=3, model=llm_model)
                if loc is None:
                    return None
                q_text = (blocks[qb].text if isinstance(blocks[qb], Paragraph) else "").strip()
                slot_obj = QASlot(
                    id=f"slot_{uuid.uuid4().hex[:8]}",
                    question_text=q_text,
                    answer_locator=loc,
                    confidence=0.6,
                    meta={"detector": "two_stage", "q_block": qb}
                )
                # Enrich slot_obj.meta with outline
                q_par = blocks[qb] if isinstance(blocks[qb], Paragraph) else None
                lvl_num = paragraph_level_from_numbering(q_par) if q_par else None
                hint, hint_level = derive_outline_hint_and_level(q_text)
                slot_obj.meta["outline"] = {"level": lvl_num or hint_level, "hint": hint}
                slot_obj.meta["needs_context"] = await llm_assess_context(blocks, qb, model=llm_model)
                dbg(f"Created QASlot from q_block {qb}: {asdict(slot_obj)}")
                return slot_obj
            except Exception as e:
                dbg(f"Error processing q_block {qb}: {e}")
                return None

        async def gather_slots() -> List[Optional[QASlot]]:
            tasks = [asyncio.create_task(process_q_block(qb)) for qb in q_indices]
            return await asyncio.gather(*tasks)

        slots = [s for s in asyncio.run(gather_slots()) if s]
        if not slots:  # fallback to legacy single scan
            slots = llm_scan_blocks(blocks, model=llm_model)
    else:
        # Fallback legacy rule‑based path (only when LLM explicitly disabled)
        slots: List[QASlot] = []
        for detector in (detect_para_question_with_blank,
                         detect_two_col_table_q_blank,
                         detect_response_label_then_blank):
            slots.extend(detector(blocks))
        # optional: refine if we still want refinement when LLM disabled (keep off)
    
    attach_context(slots, blocks)
    dbg(f"Slot count: {len(slots)}")
    dbg(f"Final payload preview: {json.dumps({'doc_type': 'docx', 'file': os.path.basename(path), 'slots': [asdict(s) for s in dedupe_slots(slots)]}, indent=2)[:1000]}")
    payload = {
        "doc_type": "docx",
        "file": os.path.basename(path),
        "slots": [asdict(s) for s in dedupe_slots(slots)]
    }#
    return payload

# ─────────────────── context attachment ───────────────────
def attach_context(slots: List[QASlot], blocks):
    """
    Populate slot.meta['context'] with: level, heading_chain and optional parent_*.
    """
    ordered = sorted(slots, key=lambda s: (s.meta or {}).get("q_block", 0))
    last_at_level = {}
    for s in ordered:
        qb = (s.meta or {}).get("q_block", 0)
        level = (s.meta or {}).get("outline", {}).get("level") or 1
        heads = heading_chain(blocks, qb)

        parent = None
        for l in range(level - 1, 0, -1):
            if l in last_at_level:
                parent = last_at_level[l]
                break

        ctx = {"level": int(level), "heading_chain": heads}
        if parent:
            ctx["parent_slot_id"] = parent.id
            ctx["parent_question_text"] = parent.question_text

        if s.meta is None:
            s.meta = {}
        s.meta["context"] = ctx
        last_at_level[level] = s

def dedupe_slots(slots: List[QASlot]) -> List[QASlot]:
    """Remove obvious duplicates (same question text + same locator)."""
    seen = set()
    out = []
    for s in slots:
        key = (s.question_text.strip().lower(),
               s.answer_locator.type,
               s.answer_locator.paragraph_index,
               s.answer_locator.offset,
               s.answer_locator.table_index,
               s.answer_locator.row,
               s.answer_locator.col)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out

# ───────────────────────── CLI ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Detect QA slots in a DOCX RFP.")
    ap.add_argument("docx_path", help="Path to .docx")
    ap.add_argument("-o", "--out", default=None, help="Write JSON to this path")
    ap.add_argument("--ai", action="store_true", help="(deprecated) AI mode is always on; flag kept for backward compatibility")
    ap.add_argument("--debug", action="store_true", help="Print verbose debug info")
    ap.add_argument("--show-text", action="store_true", help="Dump full prompt and completion text for each API call (verbose)")
    args = ap.parse_args()

    # Set debug global
    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        print("### DEBUG MODE ON ###")

    global SHOW_TEXT
    SHOW_TEXT = args.show_text

    # AI is always enabled; --ai is legacy, --no-ai removed.
    global USE_LLM
    USE_LLM = True  # AI is always on; --ai is optional/legacy

    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY is not set (required).", file=sys.stderr)
        sys.exit(1)

    # Validate file existence
    if not os.path.isfile(args.docx_path):
        print(f"Error: File '{args.docx_path}' does not exist.", file=sys.stderr)
        sys.exit(1)
    # Validate .docx extension
    if not args.docx_path.lower().endswith(".docx"):
        print(f"Error: File '{args.docx_path}' does not have a .docx extension.", file=sys.stderr)
        sys.exit(1)
    try:
        result = extract_slots_from_docx(args.docx_path)
    except Exception as e:
        print(f"Error: Failed to process DOCX file '{args.docx_path}'. The file may be invalid or corrupted.\nDetails: {e}", file=sys.stderr)
        sys.exit(1)
    # Print token/cost summary if debug enabled
    if DEBUG:
        print("--- TOKEN / COST SUMMARY ---")
        print(f"Prompt tokens:  {TOTAL_INPUT_TOKENS}")
        print(f"Completion tokens: {TOTAL_OUTPUT_TOKENS}")
        print(f"Total estimated cost: ${TOTAL_COST_USD:.4f}")
    js = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(js)
        print(f"Wrote {args.out}")
    else:
        print(js)

if __name__ == "__main__":
    main()
