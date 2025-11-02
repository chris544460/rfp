#!/usr/bin/env python3
"""
Locate answer slots inside DOCX questionnaires using rule heuristics plus LLM prompts.

The resulting JSON is consumed by `apply_answers_to_docx` and the Streamlit document
workflow.  Environment variables control which LLM framework is used along with
various performance toggles (spaCy, caching, staged prompts).

Requires environment variables for the chosen framework:
    #   • Framework selection: ANSWER_FRAMEWORK=openai|aladdin
    #   • OpenAI: set OPENAI_API_KEY (and optional OPENAI_MODEL)
    #   • Aladdin: set aladdin_studio_api_key, defaultWebServer, aladdin_user, aladdin_passwd
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, IO, List, Optional, Set, Tuple, Union

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from dotenv import load_dotenv

try:  # pragma: no cover - optional dependency
    import spacy  # type: ignore
    from spacy.matcher import Matcher  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - spaCy not available in environment
    spacy = None  # type: ignore[assignment]
    Matcher = None  # type: ignore[assignment]

from backend.llm.completions_client import CompletionsClient, get_openai_completion
from backend.prompts import read_prompt

DEBUG = True
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
    "gpt-4.1-nano-2025-04-14_research": {"in": 0.00005, "out": 0.0004, "cached_in": 0.000005},
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


# Load environment variables from a .env file if present
load_dotenv(override=True)

VENDORED_SPACY_DIR = Path(__file__).resolve().parent / "vendor" / "spacy_models"
if VENDORED_SPACY_DIR.exists():
    vendored = str(VENDORED_SPACY_DIR)
    if vendored not in sys.path:
        sys.path.append(vendored)
    os.environ.setdefault("SPACY_DATA", vendored)

# Framework and model selection
FRAMEWORK = os.getenv("ANSWER_FRAMEWORK", "aladdin")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-nano-2025-04-14_research")

FAST_DOCX = os.getenv("RFP_FAST_DOCX", "1") == "1"
USE_RULES_FIRST = os.getenv("RFP_DOCX_USE_RULES_FIRST", "1") == "1"
DOCX_CHUNK = int(os.getenv("RFP_DOCX_CHUNK", "40"))
SKIP_RAWTEXT = os.getenv("RFP_DOCX_SKIP_RAWTEXT", "1") == "1"
SKIP_CONTEXT_ASSESS = os.getenv("RFP_DOCX_SKIP_CONTEXT_ASSESS", "1") == "1"
SKIP_ANS_TYPE_LLM = os.getenv("RFP_DOCX_SKIP_ANS_TYPE_LLM", "1") == "1"
DISABLE_MC_LLM = os.getenv("RFP_DOCX_DISABLE_MC_LLM", "1") == "1"
USE_SPACY_QUESTION = os.getenv("RFP_DOCX_USE_SPACY_QUESTION", "1") == "1"

ENABLE_SLOTS_DISK_CACHE = os.getenv("RFP_ENABLE_SLOTS_DISK_CACHE", "0") == "1"
CACHE_DIR = Path(os.getenv("RFP_CACHE_DIR", ".rfp_cache"))
if ENABLE_SLOTS_DISK_CACHE:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        ENABLE_SLOTS_DISK_CACHE = False

_LLM_CACHE: Dict[Tuple[str, bool, str], str] = {}
_DOCX_NLP = None
_DOCX_MATCHER = None
_SPACY_FAILED = False


def _call_llm(prompt: str, json_output: bool = False) -> str:
    """Call the selected LLM framework and record usage."""
    if FRAMEWORK == "aladdin":
        client = CompletionsClient(model=MODEL)
        resp = client.get_completion(prompt, json_output=json_output)
    else:
        resp = get_openai_completion(prompt, MODEL, json_output=json_output)
    if isinstance(resp, tuple):
        content, usage = resp
    else:
        content, usage = resp, {}
    try:
        _record_usage(MODEL, usage)
    except Exception:
        pass
    return content


def _call_llm_cached(prompt: str, json_output: bool = False, model: Optional[str] = None) -> str:
    """Memoize LLM responses within a single run using prompt hashing."""
    cache_key = (
        (model or MODEL),
        bool(json_output),
        hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )
    if cache_key in _LLM_CACHE:
        return _LLM_CACHE[cache_key]
    content = _call_llm(prompt, json_output=json_output)
    _LLM_CACHE[cache_key] = content
    return content


def _ensure_docx_spacy() -> Optional[spacy.language.Language]:
    global _DOCX_NLP, _DOCX_MATCHER, _SPACY_FAILED
    if not USE_SPACY_QUESTION or _SPACY_FAILED or spacy is None or Matcher is None:
        if spacy is None or Matcher is None:
            _SPACY_FAILED = True
        return None
    if _DOCX_NLP is not None:
        return _DOCX_NLP
    try:
        nlp = spacy.load("en_core_web_sm", disable=["ner"])
    except Exception as exc:
        dbg(f"spaCy unavailable for DOCX question detection: {exc}")
        _SPACY_FAILED = True
        return None
    matcher = Matcher(nlp.vocab)
    matcher.add(
        "IMPERATIVE_QUESTION",
        [
            [{"LOWER": "please"}, {"POS": "VERB"}],
            [{"LOWER": "tell"}, {"LOWER": "me"}, {"POS": {"IN": ["VERB", "ADP"]}}],
        ],
    )
    _DOCX_NLP = nlp
    _DOCX_MATCHER = matcher
    return _DOCX_NLP


def _spacy_docx_is_question(text: str) -> bool:
    nlp = _ensure_docx_spacy()
    if nlp is None:
        return False
    doc = nlp(text)
    if _DOCX_MATCHER and _DOCX_MATCHER(doc):
        return True
    if doc and doc[0].tag_ == "VB":
        return True
    if any(tok.tag_.startswith("W") for tok in doc):
        return True
    return False

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
    answer_type: str = "text"  # text | multiple_choice | file | table | checkbox | multi-select | date | number
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


def _quick_question_candidate(text: str) -> bool:
    if not text:
        return False
    raw = text.strip()
    if not raw:
        return False
    lower = raw.lower()
    if "?" in raw:
        return True
    return any(phrase in lower for phrase in QUESTION_PHRASES)

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

    if USE_SPACY_QUESTION and _spacy_docx_is_question(t_raw):
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


_CELL_PLACEHOLDER_RE = re.compile(r"^(_+|\[\s*(?:insert|enter|provide)[^\]]*\])\s*$", re.IGNORECASE)


def _is_blank_cell_text(text: str) -> bool:
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if _CELL_PLACEHOLDER_RE.match(stripped):
        return True
    return False


def _table_to_matrix(table: Table) -> List[List[str]]:
    matrix: List[List[str]] = []
    try:
        row_count = len(table.rows)
        if row_count == 0:
            return matrix
        col_count = len(table.columns)
        for r in range(row_count):
            row_vals: List[str] = []
            for c in range(col_count):
                row_vals.append(_table_cell_text(table, r, c))
            matrix.append(row_vals)
    except Exception:
        return []
    return matrix


def _row_label(matrix: List[List[str]], row: int, exclude_col: int) -> str:
    if not (0 <= row < len(matrix)):
        return ""
    for idx, value in enumerate(matrix[row]):
        if idx == exclude_col:
            continue
        if value and not _is_blank_cell_text(value):
            return value.strip()
    return ""


def _column_label(matrix: List[List[str]], col: int, upto_row: int) -> str:
    if not matrix:
        return ""
    total_rows = len(matrix)
    if col < 0 or col >= len(matrix[0]):
        return ""
    start = min(max(upto_row, 0), total_rows - 1)
    for r in range(start, -1, -1):
        candidate = matrix[r][col]
        if candidate and not _is_blank_cell_text(candidate):
            return candidate.strip()
    # look ahead if nothing above
    for r in range(start + 1, total_rows):
        candidate = matrix[r][col]
        if candidate and not _is_blank_cell_text(candidate):
            return candidate.strip()
    return ""


def _identify_header_rows(matrix: List[List[str]]) -> Set[int]:
    header_rows: Set[int] = set()
    if not matrix:
        return header_rows
    header_keywords = {"question", "prompt", "requirement", "item", "description"}
    answer_keywords = {"response", "answer", "information", "details"}
    for r, row in enumerate(matrix[:3]):
        non_blank = [cell.strip().lower() for cell in row if cell and not _is_blank_cell_text(cell)]
        if not non_blank:
            continue
        if header_keywords.intersection(non_blank) and answer_keywords.intersection(non_blank):
            header_rows.add(r)
    return header_rows


def _slot_question_index(slot: QASlot) -> Optional[int]:
    meta = slot.meta or {}
    qb = meta.get("q_block")
    if qb is None:
        qb = meta.get("block")
    return qb

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

def _para_indent_info(p: Paragraph) -> Tuple[int, int]:
    """Return left and first-line indent in points (0 if absent)."""
    try:
        pf = p.paragraph_format
        left = int(pf.left_indent.pt) if pf.left_indent else 0
        first = int(pf.first_line_indent.pt) if pf.first_line_indent else 0
        return left, first
    except Exception:
        return 0, 0



def _paragraph_excerpt_lines(gi: int, paragraph: Paragraph) -> List[str]:
    style = _para_style_name(paragraph)
    align = _para_alignment(paragraph)
    num_id, ilvl = _para_num_info(paragraph)
    left, first = _para_indent_info(paragraph)
    raw = paragraph.text or ""
    leading_ws = len(raw) - len(raw.lstrip(" \t"))
    rich = _paragraph_rich_text(paragraph)
    header = (
        f"B[{gi}] PARAGRAPH style='{style}' align={align} numId={num_id} ilvl={ilvl} "
        f"left={left} first={first} leading_ws={leading_ws}"
    )
    text_line = f"B[{gi}] TEXT: {rich if rich else raw}"
    return [header, text_line]


def _table_excerpt_lines(
    gi: int,
    table: Table,
    table_index: int,
) -> Tuple[List[str], int]:
    try:
        rows = len(table.rows)
        cols = len(table.columns)
    except Exception:
        rows, cols = 0, 0
    lines = [
        f"B[{gi}] TABLE rows={rows} cols={cols} (table_index={table_index})"
    ]
    for r in range(rows):
        for c in range(cols):
            try:
                cell_text = _cell_rich_text(table.cell(r, c))
            except Exception:
                cell_text = ""
            lines.append(f"B[{gi}] [{r},{c}] TEXT: {cell_text}")
    return lines, table_index + 1


def _render_rich_excerpt(blocks: List[Union[Paragraph, Table]], start_index: int = 0) -> Tuple[str, Dict[int, int]]:
    """
    Produce a linearized, format-aware representation with global block indices.
    For tables, also return a map {global_block_index -> 0-based table_index}.
    """
    lines: List[str] = []
    table_idx_map: Dict[int, int] = {}
    running_table_index = 0
    for gi, block in enumerate(blocks, start=start_index):
        if isinstance(block, Paragraph):
            lines.extend(_paragraph_excerpt_lines(gi, block))
            continue
        if isinstance(block, Table):
            table_idx_map[gi] = running_table_index
            table_lines, running_table_index = _table_excerpt_lines(
                gi,
                block,
                running_table_index,
            )
            lines.extend(table_lines)
    excerpt = "\n".join(lines)
    return excerpt, table_idx_map


def _paragraph_structured_item(gi: int, paragraph: Paragraph) -> Dict[str, Any]:
    style = _para_style_name(paragraph)
    num_id, ilvl = _para_num_info(paragraph)
    left, first = _para_indent_info(paragraph)
    return {
        "index": gi,
        "type": "paragraph",
        "text": paragraph.text or "",
        "style": style,
        "numId": num_id,
        "ilvl": ilvl,
        "left": left,
        "first": first,
    }


def _table_structured_item(gi: int, table: Table) -> Dict[str, Any]:
    try:
        rows = len(table.rows)
        cols = len(table.columns)
    except Exception:
        rows, cols = 0, 0
    cells: List[Dict[str, Any]] = []
    for r in range(rows):
        for c in range(cols):
            try:
                cell_text = _cell_rich_text(table.cell(r, c))
            except Exception:
                cell_text = ""
            cells.append({"r": r, "c": c, "text": cell_text})
    return {
        "index": gi,
        "type": "table",
        "rows": rows,
        "cols": cols,
        "cells": cells,
    }


def _render_structured_excerpt(blocks: List[Union[Paragraph, Table]], start_index: int = 0) -> str:
    """Return JSON string describing blocks for precise LLM inspection."""
    items: List[Dict[str, Any]] = []
    for gi, b in enumerate(blocks, start=start_index):
        if isinstance(b, Paragraph):
            items.append(_paragraph_structured_item(gi, b))
            continue
        if isinstance(b, Table):
            items.append(_table_structured_item(gi, b))
    return json.dumps({"blocks": items}, ensure_ascii=False)

# ─────────────────── answer-type heuristics ───────────────────

_FILE_KWS = (
    "attach",
    "attachment",
    "upload",
    "enclose",
    "file",
    "document",
)
_TABLE_KWS = (
    "table",
    "spreadsheet",
    "complete the table",
    "fill in the table",
    "table below",
)
_MC_KWS = (
    "select",
    "choose",
    "check the box",
    "checkbox",
    "tick",
    "multiple choice",
    "which of the following",
)
_CHECKBOX_CHARS = "\u2610\u2611\u2612\u25a1\u25a0\u2713\u2714\u2717\u2718"


def _llm_mc_env_ready() -> bool:
    if FRAMEWORK == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            dbg("llm_extract_mc_choices unavailable: missing OPENAI_API_KEY")
            return False
        return True
    if FRAMEWORK == "aladdin":
        required = [
            "aladdin_studio_api_key",
            "defaultWebServer",
            "aladdin_user",
            "aladdin_passwd",
        ]
        missing = [env for env in required if not os.getenv(env)]
        if missing:
            dbg(
                "llm_extract_mc_choices unavailable: missing environment variables for aladdin: "
                + ", ".join(missing)
            )
            return False
        return True
    dbg(f"llm_extract_mc_choices unavailable: unsupported framework {FRAMEWORK}")
    return False


def _collect_mc_question_and_context(
    blocks: List[Union[Paragraph, Table]],
    q_block: int,
) -> Tuple[str, List[str]]:
    question = ""
    if isinstance(blocks[q_block], Paragraph):
        question = blocks[q_block].text or ""
    dbg(f"llm_extract_mc_choices for q_block {q_block}: '{question}'")

    following: List[str] = []
    for nb in blocks[q_block + 1 : q_block + 10]:
        if isinstance(nb, Paragraph):
            text = nb.text or ""
            if _looks_like_question(text):
                break
            following.append(text)
        else:
            break
    dbg(f"Context lines after question: {len(following)}")
    return question, following


def _build_mc_prompt(question: str, context_lines: List[str]) -> str:
    context = "\n".join(context_lines)
    template = read_prompt("mc_llm_scan")
    prompt = template.format(question=question, context=context)
    if SHOW_TEXT:
        print("\n--- PROMPT (llm_extract_mc_choices) ---")
        print(prompt)
        print("--- END PROMPT ---\n")
    return prompt


def _invoke_mc_llm(prompt: str) -> List[str]:
    try:
        resp = _call_llm_cached(prompt, json_output=True)
        if SHOW_TEXT:
            print("\n--- COMPLETION (llm_extract_mc_choices) ---")
            print(resp)
            print("--- END COMPLETION ---\n")
        options = json.loads(resp)
        dbg(f"LLM suggested options: {options}")
    except Exception as exc:
        dbg(f"llm_extract_mc_choices error: {exc}")
        return []
    if not isinstance(options, list):
        dbg("LLM response was not a list of options")
        return []
    return [opt for opt in options if isinstance(opt, str)]


def _match_llm_options_to_blocks(
    options: List[str],
    blocks: List[Union[Paragraph, Table]],
    q_block: int,
) -> List[Dict[str, object]]:
    choices: List[Dict[str, object]] = []
    for opt in options:
        opt_low = opt.lower()
        for offset, nb in enumerate(blocks[q_block + 1 : q_block + 10], start=1):
            if not isinstance(nb, Paragraph):
                break
            nb_text = nb.text or ""
            if _looks_like_question(nb_text):
                break
            if opt_low in nb_text.lower():
                choices.append(
                    {
                        "text": opt.strip(),
                        "prefix": "",
                        "block_index": q_block + offset,
                    }
                )
                dbg(f"Matched option '{opt.strip()}' to block {q_block + offset}")
                break
    return choices


def llm_extract_mc_choices(blocks: List[Union[Paragraph, Table]], q_block: int) -> List[Dict[str, object]]:
    """Use an LLM to guess multiple-choice options when heuristics fail."""
    if not _llm_mc_env_ready():
        return []

    question, following = _collect_mc_question_and_context(blocks, q_block)
    prompt = _build_mc_prompt(question, following)
    options = _invoke_mc_llm(prompt)
    if not options:
        return []

    choices = _match_llm_options_to_blocks(options, blocks, q_block)
    dbg(f"Final choices from LLM: {choices}")
    return choices


_MC_PREFIX_PATTERNS = [
    re.compile(rf"^[{_CHECKBOX_CHARS}]\s*"),
    re.compile(r"^\(\s*\)\s*"),
    re.compile(r"^\[\s*\]\s*"),
]


def _iter_mc_candidate_paragraphs(blocks: List[Union[Paragraph, Table]], q_block: int):
    for offset, block in enumerate(blocks[q_block + 1 : q_block + 10], start=1):
        if not isinstance(block, Paragraph):
            break
        text = (block.text or "").strip()
        if not text:
            continue
        if _looks_like_question(text):
            break
        yield offset, text


def _extract_choice_prefix(text: str) -> Optional[Tuple[str, str]]:
    for pattern in _MC_PREFIX_PATTERNS:
        match = pattern.match(text)
        if match:
            prefix = match.group(0)
            cleaned = text[match.end():].strip()
            return prefix, cleaned
    enum_match = _ENUM_PREFIX_RE.match(text)
    if enum_match:
        prefix = enum_match.group(0)
        cleaned = text[enum_match.end():].strip()
        return prefix, cleaned
    return None


def extract_mc_choices(blocks: List[Union[Paragraph, Table]], q_block: int) -> List[Dict[str, object]]:
    """Collect multiple choice options appearing after the question block.

    Each choice is returned as a dict with:
      - text:       cleaned option text
      - prefix:     leading marker/prefix (checkbox, enumeration, etc.)
      - block_index:index of the paragraph containing the option
    """
    choices: List[Dict[str, object]] = []
    for offset, text in _iter_mc_candidate_paragraphs(blocks, q_block):
        result = _extract_choice_prefix(text)
        if result is None:
            break
        prefix, cleaned = result
        choices.append(
            {
                "text": cleaned,
                "prefix": prefix,
                "block_index": q_block + offset,
            }
        )
    if not choices:
        dbg("Heuristic MC extraction found no choices.")
        if USE_LLM and not DISABLE_MC_LLM:
            dbg("Invoking LLM for multiple-choice options.")
            choices = llm_extract_mc_choices(blocks, q_block)
            dbg(f"LLM returned choices: {choices}")
    return choices


def _llm_answer_type_env_ready() -> bool:
    if FRAMEWORK == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            dbg("llm_infer_answer_type unavailable: missing OPENAI_API_KEY")
            return False
        return True
    if FRAMEWORK == "aladdin":
        required = [
            "aladdin_studio_api_key",
            "defaultWebServer",
            "aladdin_user",
            "aladdin_passwd",
        ]
        missing = [env for env in required if not os.getenv(env)]
        if missing:
            dbg(
                "llm_infer_answer_type unavailable: missing environment variables for aladdin: "
                + ", ".join(missing)
            )
            return False
        return True
    dbg(f"llm_infer_answer_type unavailable: unsupported framework {FRAMEWORK}")
    return False


def _collect_answer_type_context(
    blocks: List[Union[Paragraph, Table]],
    q_block: int,
) -> List[str]:
    lines: List[str] = []
    for nb in blocks[max(0, q_block - 2) : q_block]:
        if isinstance(nb, Paragraph):
            lines.append(nb.text or "")
    for nb in blocks[q_block + 1 : q_block + 10]:
        if isinstance(nb, Paragraph):
            text = nb.text or ""
            if _looks_like_question(text):
                break
            lines.append(text)
        else:
            break
    return lines


def _build_answer_type_prompt(question_text: str, context_lines: List[str]) -> str:
    context = "\n".join(context_lines)
    template = read_prompt("answer_type_llm_scan")
    prompt = template.format(question=question_text, context=context)
    if SHOW_TEXT:
        print("\n--- PROMPT (llm_infer_answer_type) ---")
        print(prompt)
        print("--- END PROMPT ---\n")
    return prompt


def _call_answer_type_llm(prompt: str) -> Optional[str]:
    try:
        resp = _call_llm_cached(prompt, model=MODEL)
        if SHOW_TEXT:
            print("\n--- COMPLETION (llm_infer_answer_type) ---")
            print(resp)
            print("--- END COMPLETION ---\n")
        return resp.strip().lower()
    except Exception as exc:
        dbg(f"llm_infer_answer_type error: {exc}")
        return None


def llm_infer_answer_type(question_text: str, blocks: List[Union[Paragraph, Table]], q_block: int) -> str:
    """Use an LLM to classify the expected answer type for a question."""
    if not _llm_answer_type_env_ready():
        return "text"

    context_lines = _collect_answer_type_context(blocks, q_block)
    prompt = _build_answer_type_prompt(question_text, context_lines)
    answer_type = _call_answer_type_llm(prompt)
    if answer_type in {"text", "multiple_choice", "file", "table"}:
        return answer_type
    return "text"


def infer_answer_type(question_text: str, blocks: List[Union[Paragraph, Table]], q_block: int) -> str:
    """Guess the expected answer format for a question.

    The heuristic uses keywords in the question text and looks ahead a few
    blocks to inspect formatting cues such as checkboxes or tables. If the
    heuristics are inconclusive, an LLM is consulted using nearby context.
    """

    t = (question_text or "").strip().lower()
    # Keyword-based checks first
    if any(kw in t for kw in _FILE_KWS):
        return "file"
    if any(kw in t for kw in _TABLE_KWS):
        return "table"
    if any(kw in t for kw in _MC_KWS):
        return "multiple_choice"

    # Look ahead at subsequent blocks for visual cues
    for nb in blocks[q_block + 1 : q_block + 6]:
        if isinstance(nb, Table):
            try:
                if len(nb.rows) > 1 and len(nb.columns) > 1:
                    return "table"
            except Exception:
                pass
            break
        elif isinstance(nb, Paragraph):
            txt = (nb.text or "").strip()
            if _looks_like_question(txt):
                break
            low = txt.lower()
            if any(ch in txt for ch in _CHECKBOX_CHARS):
                return "multiple_choice"
            if re.search(r"\[[x ]\]|\([x ]\)", txt):
                return "multiple_choice"
            if re.match(_ENUM_PREFIX_RE, txt) and not _looks_like_question(txt):
                return "multiple_choice"
            if "yes" in low and "no" in low and len(low.split()) <= 4:
                return "multiple_choice"
        else:
            break
    if SKIP_ANS_TYPE_LLM or not USE_LLM:
        return "text"
    return llm_infer_answer_type(question_text, blocks, q_block)

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
                if isinstance(nb, Paragraph):
                    nb_text = (nb.text or "").strip()
                    if _looks_like_question(nb_text):
                        break
                    if _is_blank_para(nb):
                        lvl_num = paragraph_level_from_numbering(b)
                        hint, hint_level = derive_outline_hint_and_level(text)
                        ctx_level = lvl_num or hint_level
                        slots.append(QASlot(
                            id=f"slot_{uuid.uuid4().hex[:8]}",
                            question_text=text,
                            answer_locator=AnswerLocator(type="paragraph", paragraph_index=p_index + j),
                            answer_type=infer_answer_type(text, blocks, i),
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
                if isinstance(nb, Table):
                    try:
                        if len(nb.rows) == 1 and len(nb.columns) == 1:
                            cell_text = (nb.cell(0, 0).text or "").strip()
                            if cell_text == "":
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
                                    answer_type=infer_answer_type(text, blocks, i),
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
    for i, b in enumerate(blocks):
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
                        answer_type=infer_answer_type(left, blocks, i),
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
                    if j < 0:
                        break
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
                        if i + j >= len(blocks):
                            break
                        nb = blocks[i + j]
                        if isinstance(nb, Paragraph):
                            nb_text = (nb.text or "").strip()
                            if _looks_like_question(nb_text):
                                break
                            if _is_blank_para(nb):
                                lvl_num = paragraph_level_from_numbering(prev) if isinstance(prev, Paragraph) else None
                                hint, hint_level = derive_outline_hint_and_level(q_text)
                                ctx_level = lvl_num or hint_level
                                slots.append(QASlot(
                                    id=f"slot_{uuid.uuid4().hex[:8]}",
                                    question_text=q_text,
                                    answer_locator=AnswerLocator(type="paragraph", paragraph_index=p_index + j),
                                    answer_type=infer_answer_type(q_text, blocks, i - k),
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


def _table_question_slots(
    question_text: str,
    table: Table,
    table_index: int,
    question_block: int,
) -> List[QASlot]:
    matrix = _table_to_matrix(table)
    if not matrix:
        return []
    header_rows = _identify_header_rows(matrix)
    slots: List[QASlot] = []
    base_prompt = (question_text or "Provide the requested information.").strip()
    for r, row in enumerate(matrix):
        for c, value in enumerate(row):
            if not _is_blank_cell_text(value):
                continue
            if r in header_rows:
                continue
            row_label = _row_label(matrix, r, c)
            col_label = _column_label(matrix, c, r - 1)
            if not row_label and not col_label:
                continue
            context_parts: List[str] = []
            if row_label:
                context_parts.append(row_label.strip())
            if col_label and (not row_label or col_label.lower() not in row_label.lower()):
                context_parts.append(col_label.strip())
            if not context_parts:
                context_parts.append(f"Row {r + 1}, Column {c + 1}")
            context = " / ".join(context_parts)
            prompt = f"{base_prompt} — {context}. Respond with a concise answer."
            slot = QASlot(
                id=f"slot_{uuid.uuid4().hex[:8]}",
                question_text=prompt,
                answer_locator=AnswerLocator(
                    type="table_cell",
                    table_index=table_index,
                    row=r,
                    col=c,
                ),
                answer_type="text",
                confidence=0.65,
                meta={
                    "detector": "table_reference_question",
                    "q_block": question_block,
                    "table_index": table_index,
                    "row_index": r,
                    "column_index": c,
                    "row_header": row_label,
                    "column_header": col_label,
                    "allow_table_reference": True,
                    "style_hint": "concise",
                },
            )
            slots.append(slot)
    return slots


def detect_question_followed_by_table(blocks: List[Union[Paragraph, Table]]) -> List[QASlot]:
    slots: List[QASlot] = []
    seen_tables: Set[int] = set()
    for idx, block in enumerate(blocks):
        if not isinstance(block, Paragraph):
            continue
        text = (block.text or "").strip()
        if not text:
            continue
        if not _mentions_table(text):
            continue
        if not (_quick_question_candidate(text) or _looks_like_question(text)):
            continue

        next_table_index: Optional[int] = None
        for j in range(idx + 1, len(blocks)):
            candidate = blocks[j]
            if isinstance(candidate, Table):
                next_table_index = j
                break
            if isinstance(candidate, Paragraph):
                paragraph_text = (candidate.text or "").strip()
                if paragraph_text and not _is_blank_para(candidate):
                    next_table_index = None
                    break
        if next_table_index is None:
            continue
        if next_table_index in seen_tables:
            continue

        table_block = blocks[next_table_index]
        table_position = _running_table_index(blocks, next_table_index)
        table_slots = _table_question_slots(text, table_block, table_position, idx)
        if table_slots:
            slots.extend(table_slots)
            seen_tables.add(next_table_index)
    return slots


def detect_question_followed_by_text(blocks: List[Union[Paragraph, Table]]) -> List[QASlot]:
    slots: List[QASlot] = []
    para_index = -1

    for i, block in enumerate(blocks):
        if isinstance(block, Paragraph):
            para_index += 1
            question_text = (block.text or "").strip()
            if not _looks_like_question(question_text):
                continue

            paragraph_counter = para_index
            for j in range(1, 4):
                if i + j >= len(blocks):
                    break
                next_block = blocks[i + j]
                if isinstance(next_block, Paragraph):
                    nb_text = (next_block.text or "").strip()
                    if not nb_text:
                        continue
                    if _looks_like_question(nb_text):
                        break
                    if _is_blank_para(next_block):
                        continue

                    answer_para_index = paragraph_counter
                    for k in range(i + 1, i + j + 1):
                        if isinstance(blocks[k], Paragraph):
                            answer_para_index += 1

                    lvl_num = paragraph_level_from_numbering(block)
                    hint, hint_level = derive_outline_hint_and_level(question_text)
                    outline_level = lvl_num or hint_level

                    slots.append(
                        QASlot(
                            id=f"slot_{uuid.uuid4().hex[:8]}",
                            question_text=question_text,
                            answer_locator=AnswerLocator(
                                type="paragraph",
                                paragraph_index=answer_para_index,
                            ),
                            answer_type=infer_answer_type(question_text, blocks, i),
                            confidence=0.55,
                            meta={
                                "detector": "para_question_followed",
                                "q_paragraph_index": para_index,
                                "q_block": i,
                                "outline": {"level": outline_level, "hint": hint},
                            },
                        )
                    )
                    break
                elif isinstance(next_block, Table):
                    break
                else:
                    continue

    return slots

# ─────────────────── optional LLM refiner ───────────────────

USE_LLM = True  # default is ON; can be disabled with --no-ai

def llm_refine(slots: List[QASlot], context_windows: List[str]) -> List[QASlot]:
    """
    Stub that could call an LLM to confirm/adjust low-confidence slots.
    Keep as no-op unless USE_LLM=True and you implement it.
    """
    if not USE_LLM:
        return slots

    refined: List[QASlot] = []
    for s, ctx in zip(slots, context_windows):
        if s.confidence >= 0.8:
            refined.append(s)
            continue
        template = read_prompt("docx_llm_refine")
        prompt = template.format(ctx=ctx)
        try:
            content = _call_llm_cached(prompt, json_output=True)
            if SHOW_TEXT:
                print("\n--- PROMPT (llm_refine) ---")
                print(prompt)
                print("--- COMPLETION (llm_refine) ---")
                print(content)
                print("--- END COMPLETION ---\n")
            js = json.loads(content)
            if js.get("is_question"):
                s.confidence = max(s.confidence, 0.85)
        except Exception:
            pass
        refined.append(s)
    return refined

# ─────────────────── LLM paragraph‑scan fallback ───────────────────

def llm_scan_blocks(blocks: List[Union[Paragraph, Table]], model: str = MODEL) -> List[QASlot]:
    """If rule‑based detectors find nothing, let an LLM propose Q→A blanks."""
    candidates, table_idx_map = _llm_scan_candidates(blocks, model=model)
    if not candidates:
        return []

    results: List[QASlot] = []
    for candidate in candidates:
        slot = _candidate_to_slot(candidate, blocks, table_idx_map, model=model)
        if slot:
            results.append(slot)
    return results


def _llm_scan_candidates(
    blocks: List[Union[Paragraph, Table]], *, model: str
) -> Tuple[List[Dict[str, Any]], Dict[int, int]]:
    excerpt, table_idx_map = _render_rich_excerpt(blocks)
    dbg(f"llm_scan_blocks (rich): {len(excerpt)} chars, model={model}")
    dbg(f"Sending prompt to LLM (first 400 chars): {excerpt[:400]}...")

    template = read_prompt("docx_llm_scan_blocks")
    prompt = template.format(doc=excerpt)
    if SHOW_TEXT:
        print("\n--- PROMPT (llm_scan_blocks) ---")
        print(prompt)
        print("--- END PROMPT ---\n")
    try:
        content = _call_llm_cached(prompt, json_output=True, model=model)
        js = json.loads(content)
        candidates = js.get("slots", []) or []
        if SHOW_TEXT:
            print("\n--- COMPLETION (llm_scan_blocks) ---")
            print(content)
            print("--- END COMPLETION ---\n")
        dbg(f"LLM returned {len(candidates)} slot candidates")
        dbg(f"LLM raw slot candidates: {candidates}")
        return candidates, table_idx_map
    except Exception as exc:
        dbg(f"LLM error: {exc}")
        return [], table_idx_map


def _candidate_to_slot(
    candidate: Dict[str, Any],
    blocks: List[Union[Paragraph, Table]],
    table_idx_map: Dict[int, int],
    *,
    model: str,
) -> Optional[QASlot]:
    dbg(f"Processing candidate: {candidate}")
    kind = (candidate.get("kind") or "").strip()
    try:
        if kind == "paragraph_after":
            return _slot_from_paragraph_candidate(candidate, blocks, model=model)
        if kind == "table_cell":
            return _slot_from_table_candidate(candidate, blocks, table_idx_map, model=model)
    except Exception as exc:
        dbg(f"Parse candidate error: {exc}")
    return None


def _slot_from_paragraph_candidate(
    candidate: Dict[str, Any],
    blocks: List[Union[Paragraph, Table]],
    *,
    model: str,
) -> Optional[QASlot]:
    q_block = int(candidate["question"]["block"])
    offset = max(1, min(3, int(candidate["answer"]["offset"])))
    if 0 <= q_block < len(blocks) and isinstance(blocks[q_block], Paragraph):
        q_text = (blocks[q_block].text or "").strip()
    else:
        q_text = ""
    slot = QASlot(
        id=f"slot_{uuid.uuid4().hex[:8]}",
        question_text=q_text,
        answer_locator=AnswerLocator(
            type="paragraph_after",
            paragraph_index=q_block,
            offset=offset,
        ),
        answer_type=infer_answer_type(q_text, blocks, q_block),
        confidence=0.6,
        meta={"detector": "llm_rich", "q_block": q_block, "offset": offset},
    )
    dbg(f"Appended slot from candidate: {slot}")
    return slot


def _slot_from_table_candidate(
    candidate: Dict[str, Any],
    blocks: List[Union[Paragraph, Table]],
    table_idx_map: Dict[int, int],
    *,
    model: str,
) -> Optional[QASlot]:
    q_block = int(candidate["question"]["block"])
    t_index = table_idx_map.get(q_block)
    if t_index is None:
        return None

    q_row = int(candidate["question"]["row"])
    q_col = int(candidate["question"]["col"])
    a_row = int(candidate["answer"]["row"])
    a_col = int(candidate["answer"]["col"])
    a_block = int(candidate["answer"]["block"])

    question_text = ""
    try:
        tbl = blocks[q_block]
        if isinstance(tbl, Table):
            question_text = (tbl.cell(q_row, q_col).text or "").strip()
    except Exception:
        pass

    slot = QASlot(
        id=f"slot_{uuid.uuid4().hex[:8]}",
        question_text=question_text,
        answer_locator=AnswerLocator(
            type="table_cell",
            table_index=t_index,
            row=a_row,
            col=a_col,
        ),
        answer_type=infer_answer_type(question_text, blocks, q_block),
        confidence=0.65,
        meta={
            "detector": "llm_rich",
            "q_block": q_block,
            "answer_block": a_block,
            "row": a_row,
            "col": a_col,
        },
    )
    dbg(f"Appended slot from candidate: {slot}")
    return slot

# ─────────────────── 2‑stage LLM helpers ───────────────────

def llm_detect_questions(
    blocks: List[Union[Paragraph, Table]],
    model: str = MODEL,
    chunk_size: int = DOCX_CHUNK,
) -> List[int]:
    """Return global block indices that look like questions using only the LLM."""
    # Pre-filter: remove empty blocks or paragraphs with fewer than 4 words
    filtered: List[Union[Paragraph, Table]] = []
    for b in blocks:
        if isinstance(b, Paragraph):
            # skip paragraphs with less than 4 words
            if len((b.text or "").split()) < 4:
                continue
        filtered.append(b)
    blocks = filtered
    found: List[int] = []
    for start in range(0, len(blocks), chunk_size):
        end = min(len(blocks), start + chunk_size)
        excerpt = _render_structured_excerpt(blocks[start:end], start_index=start)
        detect_template = read_prompt("docx_detect_questions")
        prompt = detect_template.format(excerpt=excerpt)
        if SHOW_TEXT:
            print("\n--- PROMPT (detect_questions) ---\n" + prompt + "\n--- END PROMPT ---\n")
        try:
            content = _call_llm_cached(prompt, json_output=True, model=model)
            if SHOW_TEXT:
                print("\n--- COMPLETION (detect_questions) ---\n" + content + "\n--- END COMPLETION ---\n")
            js = json.loads(content)
            questions = [int(i) for i in js.get("questions", [])]
            dbg(f"Model returned JSON (detect_questions) chunk {start}-{end}: {js}")
            # --- Begin debug print for each block ---
            flagged = set(questions)
            for rel_idx, b in enumerate(blocks[start:end]):
                gi = start + rel_idx
                if isinstance(b, Paragraph):
                    text = b.text or ""
                elif isinstance(b, Table):
                    # Combine all cell texts
                    cell_texts = []
                    try:
                        for row in b.rows:
                            for cell in row.cells:
                                cell_texts.append(_cell_rich_text(cell))
                        text = " | ".join(cell_texts)
                    except Exception:
                        text = ""
                else:
                    text = ""
                if gi in flagged:
                    dbg(f"Block {gi}: FLAGGED as question -> {text}")
                else:
                    dbg(f"Block {gi}: skipped -> {text}")
            # --- End debug print for each block ---
            found.extend(questions)
        except Exception as e:
            dbg(f"Error parsing detect_questions response (chunk {start}-{end}): {e}")

    unique_sorted = sorted(set(found))
    dbg(f"Questions indices extracted: {unique_sorted}")
    return unique_sorted


def _para_has_page_break(p: Paragraph) -> bool:
    """Return True if paragraph contains a hard page break."""
    for r in p.runs:
        try:
            for br in r._r.findall('.//' + qn('w:br')):
                if br.get(qn('w:type')) == 'page':
                    return True
        except Exception:
            continue
    return False


def _blocks_to_text_pages(blocks: List[Union[Paragraph, Table]]) -> List[str]:
    """Convert DOCX blocks into a list of page-level plain-text strings."""
    pages: List[str] = []
    current: List[str] = []
    for b in blocks:
        if isinstance(b, Paragraph):
            current.append(b.text or "")
            if _para_has_page_break(b):
                pages.append("\n".join(current).strip())
                current = []
        elif isinstance(b, Table):
            rows = []
            for row in b.rows:
                row_text = "\t".join((cell.text or "").strip() for cell in row.cells)
                rows.append(row_text)
            current.append("\n".join(rows))
    if current or not pages:
        pages.append("\n".join(current).strip())
    return pages


def _find_block_index_for_question(q: str, blocks: List[Union[Paragraph, Table]]) -> Optional[int]:
    """Find the global block index whose text contains the question string."""
    norm = re.sub(r"\s+", " ", q.lower().strip())
    for idx, b in enumerate(blocks):
        if isinstance(b, Paragraph):
            txt = re.sub(r"\s+", " ", (b.text or "").lower())
            if norm and norm in txt:
                return idx
        elif isinstance(b, Table):
            try:
                for row in b.rows:
                    for cell in row.cells:
                        txt = re.sub(r"\s+", " ", (cell.text or "").lower())
                        if norm and norm in txt:
                            return idx
            except Exception:
                continue
    return None


def llm_detect_questions_raw_text(
    blocks: List[Union[Paragraph, Table]],
    existing_questions: Set[str],
    model: str = MODEL,
    buffer: int = 200,
) -> List[int]:
    """Use page-level plain text to find additional question block indices."""
    pages = _blocks_to_text_pages(blocks)
    extra_blocks: List[int] = []
    for i, page in enumerate(pages):
        context = page
        if i > 0:
            context = pages[i - 1][-buffer:] + "\n" + context
        if i + 1 < len(pages):
            context = context + "\n" + pages[i + 1][:buffer]
        template = read_prompt("extract_questions")
        prompt = template.format(context=context)
        try:
            content = _call_llm_cached(prompt, model=model)
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        except Exception as e:
            dbg(f"Error in raw text detection page {i}: {e}")
            continue
        for q in lines:
            norm = q.lower().strip()
            if not norm or norm in existing_questions:
                continue
            idx = _find_block_index_for_question(q, blocks)
            if idx is not None:
                extra_blocks.append(idx)
                existing_questions.add(norm)
    return sorted(set(extra_blocks))


async def llm_locate_answer(blocks: List[Union[Paragraph, Table]], q_block: int, window: int = 3, model: str = MODEL) -> Optional[AnswerLocator]:
    """Given a question block index, ask the LLM to pick best answer location within ±window."""

    # Build context window
    start = max(0, q_block - window)
    end = min(len(blocks), q_block + window + 1)
    for i in range(q_block + 1, end):
        b = blocks[i]
        if isinstance(b, Paragraph) and _looks_like_question((b.text or "").strip()):
            end = i
            break
    local_blocks = blocks[start:end]
    excerpt, table_idx_map = _render_rich_excerpt(local_blocks)
    template = read_prompt("docx_locate_answer")
    prompt = template.format(start=start, excerpt=excerpt)
    if SHOW_TEXT:
        print(f"\n--- PROMPT (locate_answer q_block={q_block}) ---\n" + prompt + "\n--- END PROMPT ---\n")
    try:
        content = await asyncio.to_thread(_call_llm_cached, prompt, True, model)
    except Exception as e:
        dbg(f"LLM error (locate_answer q_block={q_block}): {e}")
        return None
    if SHOW_TEXT:
        print(
            f"\n--- COMPLETION (locate_answer q_block={q_block}) ---\n" + content + "\n--- END COMPLETION ---\n"
        )
    try:
        js = json.loads(content)
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


async def llm_assess_context(blocks: List[Union[Paragraph, Table]], q_block: int, model: str = MODEL) -> bool:
    """Return True if the question likely depends on previous context."""

    if SKIP_CONTEXT_ASSESS or not USE_LLM:
        return False

    start = max(0, q_block - 2)
    end = min(len(blocks), q_block + 1)
    local_blocks = blocks[start:end]
    excerpt, _ = _render_rich_excerpt(local_blocks)
    template = read_prompt("docx_assess_context")
    prompt = template.format(local_index=q_block - start, excerpt=excerpt)
    try:
        content = await asyncio.to_thread(_call_llm_cached, prompt, True, model)
    except Exception as e:
        dbg(f"LLM error (assess_context q_block={q_block}): {e}")
        return False
    try:
        js = json.loads(content)
        return bool(js.get("needs_context"))
    except Exception:
        return False

# ───────────────────────── pipeline ─────────────────────────

def _maybe_load_cached_slots(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[Path]]:
    """Return (payload, cache_path) when disk cache can be used."""
    if not ENABLE_SLOTS_DISK_CACHE:
        return None, None
    cache_path: Optional[Path] = None
    try:
        digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
        cache_path = CACHE_DIR / f"slots_{digest}_{MODEL}_fast{int(FAST_DOCX)}.json"
        if cache_path.exists():
            dbg(f"Loading DOCX slots from cache: {cache_path}")
            cached_payload = json.loads(cache_path.read_text("utf-8"))
            return _sanitize_cached_payload(cached_payload), cache_path
    except Exception as exc:
        dbg(f"Slot cache unavailable: {exc}")
        cache_path = None
    return None, cache_path


def _expand_doc_blocks(doc: docx.document.Document) -> List[Union[Paragraph, Table]]:
    """Return document blocks with explicit line breaks split into separate paragraphs."""
    blocks = list(_iter_block_items(doc))
    expanded: List[Union[Paragraph, Table]] = []
    for block in blocks:
        if isinstance(block, Paragraph) and "\n" in (block.text or ""):
            for line in (block.text or "").splitlines():
                p = Paragraph(block._p, doc)
                for run in list(p.runs):
                    p._p.remove(run._r)
                p.add_run(line)
                expanded.append(p)
        else:
            expanded.append(block)
    return expanded


def _validate_llm_environment() -> None:
    """Ensure required credentials are present before using the LLM path."""
    if FRAMEWORK == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY not set; cannot run in pure AI mode.")
        return
    if FRAMEWORK == "aladdin":
        required = [
            "aladdin_studio_api_key",
            "defaultWebServer",
            "aladdin_user",
            "aladdin_passwd",
        ]
        missing = [v for v in required if not os.getenv(v)]
        if missing:
            raise RuntimeError(
                f"Missing environment variables for aladdin framework: {', '.join(missing)}"
            )
        return
    raise RuntimeError(f"Unsupported completion framework: {FRAMEWORK}")


def _collect_slots_fast_docx(
    blocks: List[Union[Paragraph, Table]], *, model: str
) -> List[QASlot]:
    slots: List[QASlot] = []
    for detector in (
        detect_para_question_with_blank,
        detect_question_followed_by_text,
        detect_question_followed_by_table,
        detect_two_col_table_q_blank,
        detect_response_label_then_blank,
    ):
        slots.extend(detector(blocks))
    existing_blocks: Set[int] = {
        qb for qb in (_slot_question_index(s) for s in slots) if qb is not None
    }
    need_aug = not slots
    if not need_aug:
        for idx, block in enumerate(blocks):
            if idx in existing_blocks:
                continue
            if isinstance(block, Paragraph) and _quick_question_candidate(block.text or ""):
                need_aug = True
                break
    if need_aug:
        llm_candidates = llm_scan_blocks(blocks, model=model)
        for cand in llm_candidates:
            qb = _slot_question_index(cand)
            if qb is None or qb not in existing_blocks:
                slots.append(cand)
                if qb is not None:
                    existing_blocks.add(qb)
    return slots


def _heuristic_question_indices(blocks: List[Union[Paragraph, Table]]) -> List[int]:
    indices: List[int] = []
    for idx, block in enumerate(blocks):
        if not isinstance(block, Paragraph) or not block.text:
            continue
        raw = block.text.strip()
        low = raw.lower()
        reason = None
        if "?" in raw:
            reason = "contains '?'"
        elif "please" in low:
            reason = "contains 'please'"
        if reason:
            dbg(f"Heuristic flagged block {idx}: {reason} -> {raw}")
            indices.append(idx)
    return indices


def _remaining_block_view(
    blocks: List[Union[Paragraph, Table]],
    skip_indices: Set[int],
) -> Tuple[List[Union[Paragraph, Table]], Dict[int, int]]:
    filtered: List[Union[Paragraph, Table]] = []
    global_to_local: Dict[int, int] = {}
    for global_idx, block in enumerate(blocks):
        if global_idx in skip_indices:
            continue
        local_idx = len(filtered)
        filtered.append(block)
        global_to_local[local_idx] = global_idx
    return filtered, global_to_local


def _combine_candidate_indices(
    blocks: List[Union[Paragraph, Table]],
    *,
    model: str,
) -> List[int]:
    heuristics = _heuristic_question_indices(blocks)
    remaining_blocks, global_to_local = _remaining_block_view(blocks, set(heuristics))
    local_hits = llm_detect_questions(remaining_blocks, model=model)
    llm_indices = [global_to_local[idx] for idx in local_hits if idx in global_to_local]
    return sorted(set(heuristics + llm_indices))


async def _build_slot_async(
    blocks: List[Union[Paragraph, Table]],
    qb: int,
    *,
    model: str,
) -> Optional[QASlot]:
    try:
        loc = await llm_locate_answer(blocks, qb, window=3, model=model)
        if loc is None:
            return None
        q_text = (blocks[qb].text if isinstance(blocks[qb], Paragraph) else "").strip()
        slot_obj = QASlot(
            id=f"slot_{uuid.uuid4().hex[:8]}",
            question_text=q_text,
            answer_locator=loc,
            answer_type=infer_answer_type(q_text, blocks, qb),
            confidence=0.6,
            meta={"detector": "two_stage", "q_block": qb},
        )
        q_par = blocks[qb] if isinstance(blocks[qb], Paragraph) else None
        lvl_num = paragraph_level_from_numbering(q_par) if q_par else None
        hint, hint_level = derive_outline_hint_and_level(q_text)
        slot_obj.meta["outline"] = {"level": lvl_num or hint_level, "hint": hint}
        slot_obj.meta["needs_context"] = await llm_assess_context(blocks, qb, model=model)
        dbg(f"Created QASlot from q_block {qb}: {asdict(slot_obj)}")
        return slot_obj
    except Exception as err:
        dbg(f"Error processing q_block {qb}: {err}")
        return None


def _gather_slots_for_indices(
    indices: List[int],
    blocks: List[Union[Paragraph, Table]],
    *,
    model: str,
) -> List[QASlot]:
    async def _runner() -> List[Optional[QASlot]]:
        tasks = [asyncio.create_task(_build_slot_async(blocks, qb, model=model)) for qb in indices]
        return await asyncio.gather(*tasks)

    return [slot for slot in asyncio.run(_runner()) if slot]


def _augment_with_raw_text_slots(
    slots: List[QASlot],
    blocks: List[Union[Paragraph, Table]],
    *,
    model: str,
) -> List[QASlot]:
    if FAST_DOCX or SKIP_RAWTEXT:
        return slots
    existing_qtexts = {
        (slot.question_text or "").strip().lower()
        for slot in slots
        if slot.question_text
    }
    extra_blocks = llm_detect_questions_raw_text(
        blocks,
        existing_qtexts,
        model=model,
    )
    for qb in extra_blocks:
        extra_slot = asyncio.run(_build_slot_async(blocks, qb, model=model))
        if extra_slot:
            if extra_slot.meta is None:
                extra_slot.meta = {}
            extra_slot.meta["detector"] = "raw_text"
            slots.append(extra_slot)
    return slots


def _collect_slots_two_stage(
    blocks: List[Union[Paragraph, Table]], *, model: str
) -> List[QASlot]:
    candidate_indices = _combine_candidate_indices(blocks, model=model)
    slots = _gather_slots_for_indices(candidate_indices, blocks, model=model)
    slots = _augment_with_raw_text_slots(slots, blocks, model=model)
    if not slots:
        return llm_scan_blocks(blocks, model=model)
    return slots


def _collect_slots_with_llm(blocks: List[Union[Paragraph, Table]]) -> List[QASlot]:
    _validate_llm_environment()
    llm_model = MODEL
    if FAST_DOCX and USE_RULES_FIRST:
        return _collect_slots_fast_docx(blocks, model=llm_model)
    return _collect_slots_two_stage(blocks, model=llm_model)


def _collect_slots_without_llm(blocks: List[Union[Paragraph, Table]]) -> List[QASlot]:
    slots: List[QASlot] = []
    for detector in (
        detect_para_question_with_blank,
        detect_question_followed_by_text,
        detect_question_followed_by_table,
        detect_two_col_table_q_blank,
        detect_response_label_then_blank,
    ):
        slots.extend(detector(blocks))
    return slots


def _promote_missing_questions(
    slots: List[QASlot],
    blocks: List[Union[Paragraph, Table]],
) -> List[QASlot]:
    existing_blocks: Set[int] = {
        qb for qb in (_slot_question_index(slot) for slot in slots) if qb is not None
    }
    for idx, block in enumerate(blocks):
        if idx in existing_blocks:
            continue
        if not isinstance(block, Paragraph):
            continue
        text = (block.text or "").strip()
        if not text or not _looks_like_question(text):
            continue
        locator = AnswerLocator(type="paragraph_after", paragraph_index=idx, offset=1)
        slot = QASlot(
            id=f"slot_{uuid.uuid4().hex[:8]}",
            question_text=text,
            answer_locator=locator,
            answer_type=infer_answer_type(text, blocks, idx),
            confidence=0.35,
            meta={
                "detector": "heuristic_promoted",
                "q_block": idx,
                "promoted_from": "missing_blank",
            },
        )
        next_block = blocks[idx + 1] if idx + 1 < len(blocks) else None
        force_append = False
        if isinstance(next_block, Paragraph):
            next_text = (next_block.text or "").strip()
            if next_text and not _looks_like_question(next_text):
                force_append = True
        elif isinstance(next_block, Table):
            force_append = True
        if force_append:
            if slot.meta is None:
                slot.meta = {}
            slot.meta["force_insert_after_question"] = True
        slots.append(slot)
        existing_blocks.add(idx)
    return slots


def _attach_multiple_choice_choices(
    slots: List[QASlot],
    blocks: List[Union[Paragraph, Table]],
) -> None:
    for slot in slots:
        if slot.answer_type != "multiple_choice":
            continue
        qb = (slot.meta or {}).get("q_block")
        if qb is None:
            continue
        choices = extract_mc_choices(blocks, qb)
        if choices:
            if slot.meta is None:
                slot.meta = {}
            slot.meta["choices"] = choices


def _build_payload(
    path: str,
    slots: List[QASlot],
    skipped_slots: List[Dict[str, Any]],
    heuristic_skips: List[Dict[str, Any]],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "doc_type": "docx",
        "file": os.path.basename(path),
        "slots": [asdict(slot) for slot in slots],
    }
    if skipped_slots:
        payload["skipped_slots"] = skipped_slots
    if heuristic_skips:
        payload["heuristic_skips"] = heuristic_skips
    return payload


def _write_slots_cache(cache_path: Optional[Path], payload: Dict[str, Any]) -> None:
    if not ENABLE_SLOTS_DISK_CACHE or cache_path is None:
        return
    try:
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        dbg(f"Unable to write slot cache {cache_path}: {exc}")


DocxSource = Union[str, Path, IO[bytes]]


def extract_slots_from_docx(path: DocxSource) -> Dict[str, Any]:
    cached_payload: Optional[Dict[str, Any]] = None
    cache_path: Optional[Path] = None
    path_hint: Optional[str] = None

    if isinstance(path, (str, Path)):
        path_hint = str(path)
        cached_payload, cache_path = _maybe_load_cached_slots(path_hint)
        if cached_payload is not None:
            return cached_payload
        doc_source = path
    else:
        path_hint = getattr(path, "name", None)
        if path_hint:
            try:
                if Path(path_hint).exists():
                    cached_payload, cache_path = _maybe_load_cached_slots(path_hint)
                    if cached_payload is not None:
                        return cached_payload
            except Exception:
                cached_payload = None
                cache_path = None
        doc_source = path
        try:
            doc_source.seek(0)
        except Exception:
            pass

    doc = docx.Document(doc_source)
    blocks = _expand_doc_blocks(doc)

    dbg(f"extract_slots_from_docx: USE_LLM={USE_LLM}")
    dbg(f"Total blocks: {len(blocks)}")

    slots = (
        _collect_slots_with_llm(blocks)
        if USE_LLM
        else _collect_slots_without_llm(blocks)
    )
    slots = _promote_missing_questions(slots, blocks)
    _attach_multiple_choice_choices(slots, blocks)

    slots, skipped_slots = filter_slots(slots, blocks)
    attach_context(slots, blocks)
    deduped_slots = dedupe_slots(slots)
    heuristic_skips = collect_heuristic_skips(deduped_slots, blocks)
    dbg(f"Slot count: {len(deduped_slots)}")

    payload_name = path_hint or "in-memory.docx"
    payload = _build_payload(payload_name, deduped_slots, skipped_slots, heuristic_skips)
    _write_slots_cache(cache_path, payload)
    return payload


def _sanitize_cached_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure cached slot payloads obey current heuristics and metadata schema."""

    def _normalize_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
        meta = dict(meta or {})
        if "block" in meta and "q_block" not in meta:
            meta["q_block"] = meta.pop("block")
        return meta

    slots = payload.get("slots") or []
    cleaned: List[Dict[str, Any]] = []
    for slot in slots:
        meta = _normalize_meta(slot.get("meta") or {})
        question = (slot.get("question_text") or "").strip()
        if not question:
            dbg(
                "_sanitize_cached_payload dropping cached slot "
                f"{slot.get('id', '<unknown>')} blank question text"
            )
            continue
        slot["question_text"] = question
        slot["meta"] = meta
        cleaned.append(slot)
    payload["slots"] = cleaned

    skipped = payload.get("skipped_slots") or []
    cleaned_skipped: List[Dict[str, Any]] = []
    for entry in skipped:
        meta = _normalize_meta(entry.get("meta") or {})
        question = (entry.get("question_text") or "").strip()
        if not question:
            continue
        entry["question_text"] = question
        entry["meta"] = meta
        cleaned_skipped.append(entry)
    if cleaned_skipped:
        payload["skipped_slots"] = cleaned_skipped
    elif "skipped_slots" in payload:
        payload.pop("skipped_slots", None)

    heuristics = payload.get("heuristic_skips") or []
    cleaned_heuristic: List[Dict[str, Any]] = []
    for entry in heuristics:
        question = (entry.get("question_text") or "").strip()
        if not question:
            continue
        entry["question_text"] = question
        cleaned_heuristic.append(entry)
    if cleaned_heuristic:
        payload["heuristic_skips"] = cleaned_heuristic
    elif "heuristic_skips" in payload:
        payload.pop("heuristic_skips", None)
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
        for ancestor_level in range(level - 1, 0, -1):
            if ancestor_level in last_at_level:
                parent = last_at_level[ancestor_level]
                break

        ctx = {"level": int(level), "heading_chain": heads}
        if parent:
            ctx["parent_slot_id"] = parent.id
            ctx["parent_question_text"] = parent.question_text

        if s.meta is None:
            s.meta = {}
        s.meta["context"] = ctx
        last_at_level[level] = s

def _normalize_question_text(text: str) -> str:
    return strip_enum_prefix((text or "").strip()).lower()


def _diagnose_paragraph(text: str) -> Dict[str, object]:
    raw = (text or "").strip()
    positives: List[str] = []
    negatives: List[str] = []

    if not raw:
        negatives.append("blank paragraph")
        return {
            "looks_like": False,
            "positives": positives,
            "negatives": negatives,
            "ends_with_q": False,
        }

    ends_with_q = raw.rstrip().endswith("?")
    if "?" in raw:
        positives.append("contains '?'")

    stripped = strip_enum_prefix(raw).strip()
    lowered = stripped.lower()

    starts_with = next((phrase for phrase in QUESTION_PHRASES if lowered.startswith(phrase)), None)
    if starts_with:
        positives.append(f"starts with '{starts_with}'")

    contains_cue = next((phrase for phrase in QUESTION_PHRASES if phrase in lowered), None)
    if contains_cue and contains_cue != starts_with:
        positives.append(f"contains '{contains_cue}'")

    if raw.lower().startswith(("question:", "prompt:", "rfp question:")):
        positives.append("prefixed with question label")

    if _ENUM_PREFIX_RE.match(raw) and contains_cue:
        positives.append("enumerated with cue phrase")

    if USE_SPACY_QUESTION and _spacy_docx_is_question(raw):
        positives.append("spaCy detector match")

    looks_like = bool(positives)

    if not looks_like:
        word_count = len(raw.split())
        if word_count < 4:
            negatives.append("fewer than 4 words")
        if "?" not in raw:
            negatives.append("no question mark")
        if not contains_cue:
            negatives.append("no cue phrase detected")
        if raw.endswith(":"):
            negatives.append("ends with ':' (likely label)")
        if raw.isupper() and len(raw) > 6:
            negatives.append("all caps (likely heading)")

    return {
        "looks_like": looks_like,
        "positives": positives,
        "negatives": negatives,
        "ends_with_q": ends_with_q,
    }


def dedupe_slots(slots: List[QASlot]) -> List[QASlot]:
    """Collapse slots with identical questions and overlapping locator ranges."""

    def norm_q(text: str) -> str:
        return strip_enum_prefix(text or "").strip().lower()

    def loc_range(slot: QASlot):
        loc = slot.answer_locator
        if loc.type == "table_cell":
            return ("cell", loc.table_index, loc.row, loc.col)
        if loc.type == "paragraph":
            return (loc.paragraph_index, loc.paragraph_index)
        if loc.type == "paragraph_after":
            start = (loc.paragraph_index or 0) + 1
            end = (loc.paragraph_index or 0) + loc.offset
            return (start, end)
        return None

    def more_specific(a: QASlot, b: QASlot) -> QASlot:
        ra, rb = loc_range(a), loc_range(b)
        if ra is None or rb is None:
            return a
        if len(ra) == 4 and len(rb) == 4:
            return a  # same cell → keep first
        la = ra[1] - ra[0]
        lb = rb[1] - rb[0]
        if la != lb:
            return a if la < lb else b
        pr = {"paragraph": 2, "paragraph_after": 1}
        return a if pr.get(a.answer_locator.type, 0) >= pr.get(b.answer_locator.type, 0) else b

    out: List[QASlot] = []
    for s in slots:
        nq = norm_q(s.question_text)
        r = loc_range(s)
        dup_idx = None
        for i, ex in enumerate(out):
            if norm_q(ex.question_text) != nq:
                continue
            er = loc_range(ex)
            if r is None or er is None:
                continue
            if len(r) == 4 and len(er) == 4:
                if r == er:
                    dup_idx = i
                    break
            elif len(r) == 2 and len(er) == 2:
                if not (r[1] < er[0] or r[0] > er[1]):
                    dup_idx = i
                    break
        if dup_idx is not None:
            out[dup_idx] = more_specific(out[dup_idx], s)
        else:
            out.append(s)
    return out


def _resolve_slot_question_text(slot: QASlot, blocks: List[Union[Paragraph, Table]]) -> str:
    text = (slot.question_text or "").strip()
    if text:
        return text
    meta = slot.meta or {}
    qb = meta.get("q_block")
    if isinstance(qb, int) and 0 <= qb < len(blocks):
        block = blocks[qb]
        if isinstance(block, Paragraph):
            return (block.text or "").strip()
        if isinstance(block, Table):
            row = meta.get("row")
            col = meta.get("col")
            try:
                if row is not None and col is not None:
                    return (block.cell(int(row), int(col)).text or "").strip()
            except Exception:
                pass
    return text


_TABLE_PATTERN = re.compile(r"\btable(s)?\b", re.IGNORECASE)


def _mentions_table(text: str) -> bool:
    return bool(_TABLE_PATTERN.search(text))


def _record_skip(
    skipped: List[Dict[str, Any]],
    slot: QASlot,
    meta: Dict[str, Any],
    resolved: str,
    reason: str,
) -> None:
    entry: Dict[str, Any] = {
        "id": slot.id,
        "question_text": resolved,
        "detector": meta.get("detector"),
        "reason": reason,
        "meta": dict(meta),
    }
    qb = meta.get("q_block")
    if isinstance(qb, int):
        entry["q_block"] = qb
    skipped.append(entry)
    dbg(
        "filter_slots dropping slot "
        f"{slot.id} reason={reason} preview={resolved[:80]}"
    )


def filter_slots(
    slots: List[QASlot], blocks: List[Union[Paragraph, Table]]
) -> Tuple[List[QASlot], List[Dict[str, Any]]]:
    """Drop slots whose questions fail heuristics or reference tables."""

    gated_detectors = {"llm_rich", "two_stage", "raw_text"}
    cleaned: List[QASlot] = []
    skipped: List[Dict[str, Any]] = []
    for slot in slots:
        meta = slot.meta or {}
        detector = meta.get("detector")
        resolved = _resolve_slot_question_text(slot, blocks)
        if not resolved:
            _record_skip(skipped, slot, meta, resolved, "blank_question_text")
            continue
        if resolved != (slot.question_text or "").strip():
            slot.question_text = resolved
        # Previously, slots mentioning tables were skipped unless explicitly allowed.
        # Tables are now allowed; do not skip solely due to table references.
        # Keeping detection utilities for potential diagnostics, but no skip here.
        if _looks_like_question(resolved):
            cleaned.append(slot)
            continue
        if meta.get("force_insert_after_question"):
            cleaned.append(slot)
            continue
        if detector not in gated_detectors:
            _record_skip(skipped, slot, meta, resolved, "heuristic_veto")
            continue
        _record_skip(skipped, slot, meta, resolved, "llm_veto")
    return cleaned, skipped


def collect_heuristic_skips(slots: List[QASlot], blocks: List[Union[Paragraph, Table]]) -> List[Dict[str, Any]]:
    """Return diagnostics for question-like paragraphs that lack slots."""

    slot_lookup: Dict[str, List[Dict[str, object]]] = {}
    question_blocks: Set[int] = set()

    for slot in slots:
        meta = slot.meta or {}
        qb = meta.get("q_block")
        if isinstance(qb, int):
            question_blocks.add(qb)
        norm = _normalize_question_text(slot.question_text)
        if norm:
            slot_lookup.setdefault(norm, []).append(
                {
                    "slot_id": slot.id,
                    "q_block": qb,
                    "detector": meta.get("detector"),
                }
            )

    diagnostics: List[Dict[str, Any]] = []
    seen_norms: Set[str] = set()

    for idx, block in enumerate(blocks):
        if not isinstance(block, Paragraph):
            continue
        text = (block.text or "").strip()
        if not text:
            continue
        if idx in question_blocks:
            continue

        diag = _diagnose_paragraph(text)
        if not diag.get("looks_like"):
            continue

        norm = _normalize_question_text(text)
        prev_seen = bool(norm and norm in seen_norms)
        if norm:
            seen_norms.add(norm)

        slot_hits = slot_lookup.get(norm, []) if norm else []
        if slot_hits:
            slot_ids = ", ".join(item["slot_id"] for item in slot_hits)
            detectors = sorted({item["detector"] for item in slot_hits if item.get("detector") and item.get("detector") != "unknown"})
            if any(item.get("q_block") is None for item in slot_hits):
                detector_note = f" ({', '.join(detectors)})" if detectors else ""
                reason = (
                    "question text matches slot(s) "
                    f"{slot_ids} but extractor did not record a paragraph index{detector_note}"
                )
            else:
                reason = f"question text already covered by slot(s) {slot_ids}"
        else:
            factors: List[str] = []
            next_block = blocks[idx + 1] if idx + 1 < len(blocks) else None
            if prev_seen:
                factors.append("duplicate question text encountered earlier in the document")
            if isinstance(next_block, Table):
                factors.append(
                    "the next block is a table; automatic slot insertion inside tables is disabled"
                )
            elif isinstance(next_block, Paragraph):
                next_text = (next_block.text or "").strip()
                if next_text and not _looks_like_question(next_text):
                    factors.append(
                        "the following paragraph already contains answer text and cannot be overwritten automatically"
                    )
                elif not next_text:
                    factors.append(
                        "a blank paragraph follows, but the extractor still declined to insert due to prior safeguards"
                    )
            else:
                if next_block is None:
                    factors.append("question appears at the end of the document")
            if not factors:
                factors.append("heuristics saw a question, but no safe answer location was identified")
            reason = "; ".join(factors)

        diagnostics.append(
            {
                "paragraph_index": idx,
                "question_text": text,
                "reason": reason,
                "positives": diag.get("positives") or [],
                "negatives": diag.get("negatives") or [],
            }
        )

    return diagnostics

# ───────────────────────── CLI ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Detect QA slots in a DOCX RFP.")
    ap.add_argument("docx_path", help="Path to .docx")
    ap.add_argument("-o", "--out", default=None, help="Write JSON to this path")
    ap.add_argument("--ai", action="store_true", help="(deprecated) AI mode is always on; flag kept for backward compatibility")
    ap.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        default=True,
        help="Print verbose debug info (default on)",
    )
    ap.add_argument(
        "--no-debug",
        dest="debug",
        action="store_false",
        help="Disable debug info",
    )
    ap.add_argument("--show-text", action="store_true", help="Dump full prompt and completion text for each API call (verbose)")
    ap.add_argument(
        "--framework",
        choices=["openai", "aladdin"],
        default=os.getenv("ANSWER_FRAMEWORK", "aladdin"),
        help="Which completion framework to use",
    )
    ap.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-4.1-nano-2025-04-14_research"),
        help="Model name for the chosen framework",
    )
    if len(sys.argv) == 1:
        ap.print_help()
        sys.exit(1)
    args = ap.parse_args()

    # Set debug global
    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        print("### DEBUG MODE ON ###")
        print(f"[slot_finder] processing {args.docx_path}")

    global SHOW_TEXT
    SHOW_TEXT = args.show_text

    # AI is always enabled; --ai is legacy, --no-ai removed.
    global USE_LLM
    USE_LLM = True  # AI is always on; --ai is optional/legacy

    global FRAMEWORK, MODEL
    FRAMEWORK = args.framework
    MODEL = args.model

    if FRAMEWORK == "openai" and not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY is not set (required for openai framework).", file=sys.stderr)
        sys.exit(1)
    if FRAMEWORK == "aladdin":
        required = ["aladdin_studio_api_key", "defaultWebServer", "aladdin_user", "aladdin_passwd"]
        missing = [v for v in required if not os.getenv(v)]
        if missing:
            print(
                f"Error: Missing environment variables for aladdin framework: {', '.join(missing)}",
                file=sys.stderr,
            )
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
        if DEBUG:
            print("[slot_finder] extracting slots from DOCX")
        result = extract_slots_from_docx(args.docx_path)
        if DEBUG:
            print(f"[slot_finder] found {len(result.get('slots', []))} slots")
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
        if DEBUG:
            print(f"[slot_finder] wrote output to {args.out}")
        else:
            print(f"Wrote {args.out}")
    else:
        print(js)

if __name__ == "__main__":
    main()
    # Example:
    # python backend/documents/docx/slot_finder.py --docx sample.docx --out slots.json
