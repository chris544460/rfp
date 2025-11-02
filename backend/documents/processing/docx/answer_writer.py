#!/usr/bin/env python3
# rfp_docx_apply_answers.py
# Apply answers into a DOCX according to slots.json produced by rfp_docx_slot_finder.py

import argparse
import json
import os
import sys
import re
import asyncio
import importlib
import traceback
from types import ModuleType
from typing import List, Union, Optional, Dict, Tuple, Callable

import docx
from docx.text.paragraph import Paragraph
from docx.table import Table
from docx.oxml import OxmlElement
from docx.enum.text import WD_COLOR_INDEX
from .slot_extractor import _looks_like_question
from .comments import add_comment_to_run

# ---------------------------- Debug helpers ----------------------------
DEBUG = True
def dbg(msg: str):
    if DEBUG:
        print(f"[APPLY-DEBUG] {msg}")

# ---------------------------- DOC iteration ----------------------------
def iter_block_items(doc: docx.document.Document) -> List[Union[Paragraph, Table]]:
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    blocks: List[Union[Paragraph, Table]] = []
    parent = doc.element.body
    for child in parent.iterchildren():
        if isinstance(child, CT_P):
            blocks.append(Paragraph(child, doc))
        elif isinstance(child, CT_Tbl):
            blocks.append(Table(child, doc))
    return blocks

def build_indexes(doc: docx.document.Document) -> Tuple[
    List[Union[Paragraph, Table]],
    List[Paragraph],
    Dict[int, int],
    Dict[int, int]
]:
    blocks = iter_block_items(doc)
    paragraphs: List[Paragraph] = []
    block_to_para: Dict[int, int] = {}
    block_to_table: Dict[int, int] = {}
    running_table_index = 0
    for bi, b in enumerate(blocks):
        if isinstance(b, Paragraph):
            block_to_para[bi] = len(paragraphs)
            paragraphs.append(b)
        elif isinstance(b, Table):
            block_to_table[bi] = running_table_index
            running_table_index += 1
    return blocks, paragraphs, block_to_para, block_to_table

# ---------------------------- Utilities ----------------------------
_BLANK_RE = re.compile(r"_+\s*$")
_CHECKBOX_CHARS = "☐☑☒□■✓✔✗✘"
# Allow comma-separated citations like "[1,2]" or "[1, 2, 3]"
_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

def is_blank_para(p: Paragraph) -> bool:
    t = (p.text or "").strip()
    if t == "":
        return True
    if _BLANK_RE.match(t):
        return True
    if re.fullmatch(r"\[(?:insert|enter|provide)[^\]]*\]", t.lower()):
        return True
    try:
        if any(r.text and r.underline for r in p.runs) and len(t.replace("_","").strip()) == 0:
            return True
    except Exception:
        pass
    return False

def insert_paragraph_after(paragraph: Paragraph, text: str = "") -> Paragraph:
    new_p_elm = OxmlElement("w:p")
    paragraph._element.addnext(new_p_elm)
    new_p = Paragraph(new_p_elm, paragraph._parent)
    if text:
        new_p.add_run(text)
    return new_p

def normalize_question(q: str) -> str:
    return " ".join((q or "").strip().lower().split())

def _normalize_citations(raw: object) -> Dict[str, object]:
    if not raw:
        return {}
    result: Dict[str, object] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = []
        for i, item in enumerate(raw, 1):
            key = getattr(item, "get", lambda *_: None)("id") or getattr(item, "get", lambda *_: None)("num") or i
            items.append((key, item))
    else:
        return {}
    for key, val in items:
        if isinstance(val, dict):
            snippet = val.get("text") or val.get("snippet") or val.get("source_text") or val.get("content") or ""
            result[str(key)] = {"text": str(snippet), "source_file": val.get("source_file")}
        else:
            result[str(key)] = {"text": str(val)}
    return result

def _append_with_bold(paragraph: Paragraph, text: str, bold_state: bool) -> bool:
    """Append text to paragraph, interpreting **markers** as bold toggles."""
    if not text:
        return bold_state
    i = 0
    length = len(text)
    while i < length:
        if text.startswith("**", i):
            bold_state = not bold_state
            i += 2
            continue
        next_marker = text.find("**", i)
        if next_marker == -1:
            segment = text[i:]
            i = length
        else:
            segment = text[i:next_marker]
            i = next_marker
        if segment:
            run = paragraph.add_run(segment)
            if bold_state:
                run.bold = True
    return bold_state


def _parse_citation_numbers(raw_numbers: str) -> List[str]:
    return [num.strip() for num in raw_numbers.split(",") if num.strip()]


def _resolve_citation_entry(citations: Dict[object, object], num: str):
    entry = citations.get(num)
    if entry is None:
        try:
            entry = citations.get(int(num))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            entry = None
    return entry


def _extract_comment_payload(data: object) -> Tuple[Optional[str], Optional[str]]:
    snippet: Optional[str] = None
    source_file: Optional[str] = None
    if isinstance(data, dict):
        raw_snippet = data.get("text") or data.get("snippet") or data.get("content")
        snippet = str(raw_snippet) if raw_snippet else None
        source_value = data.get("source_file")
        source_file = str(source_value) if source_value else None
    elif data is not None:
        snippet = str(data)
    return snippet, source_file


def _append_citation_runs(
    paragraph: Paragraph,
    doc,
    citation_numbers: List[str],
    citations: Dict[object, object],
) -> None:
    for idx, num in enumerate(citation_numbers):
        run = paragraph.add_run(f"[{num}]")
        data = _resolve_citation_entry(citations, num)
        snippet, source_file = _extract_comment_payload(data)
        if snippet:
            add_comment_to_run(
                doc,
                run,
                snippet,
                bold_prefix="Source Text: ",
                source_file=source_file,
            )
        if idx < len(citation_numbers) - 1:
            paragraph.add_run(" ")


def _render_text_line(
    paragraph: Paragraph,
    line: str,
    citations: Dict[object, object],
    bold_state: bool,
    doc,
) -> bool:
    pos = 0
    for match in _CITATION_RE.finditer(line):
        if match.start() > pos:
            bold_state = _append_with_bold(paragraph, line[pos:match.start()], bold_state)
        citation_numbers = _parse_citation_numbers(match.group(1))
        if citation_numbers:
            _append_citation_runs(paragraph, doc, citation_numbers, citations)
        pos = match.end()
    if pos < len(line):
        bold_state = _append_with_bold(paragraph, line[pos:], bold_state)
    return bold_state


def _add_text_with_citations(paragraph: Paragraph, text: str, citations: Dict[object, object]) -> None:
    """Write text and attach Word comments to each [n] marker using Utilities helper."""
    doc = paragraph.part.document
    parts = text.split("\n")
    bold_state = False
    for index, line in enumerate(parts):
        bold_state = _render_text_line(paragraph, line, citations, bold_state, doc)
        if index < len(parts) - 1:
            paragraph.add_run().add_break()

# ---------------------------- Answers loader ----------------------------
def _populate_from_mapping(by_id: Dict[str, object], by_q: Dict[str, object], mapping: Dict[str, object]) -> None:
    for key, value in mapping.items():
        key_str = str(key)
        if key_str.startswith("slot_"):
            by_id[key_str] = value
        else:
            by_q[normalize_question(key_str)] = value


def _populate_from_grouped_dict(by_id: Dict[str, object], by_q: Dict[str, object], data: Dict[str, object]) -> None:
    for key, value in (data.get("by_id") or {}).items():
        by_id[str(key)] = value
    for key, value in (data.get("by_question") or {}).items():
        by_q[normalize_question(key)] = value


def _populate_from_list(by_id: Dict[str, object], by_q: Dict[str, object], items: List[object]) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if "slot_id" in item:
            by_id[str(item["slot_id"])] = item.get("answer", "")
        elif "question_text" in item:
            question = normalize_question(str(item["question_text"]))
            by_q[question] = item.get("answer", "")


def load_answers(answers_path: str) -> Tuple[Dict[str, object], Dict[str, object]]:
    with open(answers_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    by_id: Dict[str, object] = {}
    by_q: Dict[str, object] = {}

    if isinstance(data, dict):
        if "by_id" in data or "by_question" in data:
            _populate_from_grouped_dict(by_id, by_q, data)
        else:
            _populate_from_mapping(by_id, by_q, data)
        return by_id, by_q

    if isinstance(data, list):
        _populate_from_list(by_id, by_q, data)
        return by_id, by_q

    raise ValueError("Unsupported answers JSON structure.")

# ---------------------------- Locator resolution ----------------------------
def _coerce_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _get_paragraph_from_paragraphs(paragraphs: List[Paragraph], index: Optional[int]) -> Optional[Paragraph]:
    if index is None or not (0 <= index < len(paragraphs)):
        return None
    return paragraphs[index]


def _get_paragraph_from_blocks(blocks: List[Union[Paragraph, Table]], index: Optional[int]) -> Optional[Paragraph]:
    if index is None or not (0 <= index < len(blocks)):
        return None
    candidate = blocks[index]
    return candidate if isinstance(candidate, Paragraph) else None


def _extract_q_block_index(meta: Optional[Dict[str, object]]) -> Optional[int]:
    if not isinstance(meta, dict):
        return None
    return _coerce_int(meta.get("q_block"))


def _first_paragraph(*candidates: Optional[Paragraph]) -> Optional[Paragraph]:
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def resolve_anchor_paragraph(
    doc: docx.document.Document,
    blocks: List[Union[Paragraph, Table]],
    paragraphs: List[Paragraph],
    block_to_para: Dict[int, int],
    locator: Dict[str, object],
    meta: Optional[Dict[str, object]]
) -> Optional[Paragraph]:
    locator_type = str(locator.get("type", ""))
    paragraph_index = _coerce_int(locator.get("paragraph_index"))

    if locator_type == "paragraph":
        return _get_paragraph_from_paragraphs(paragraphs, paragraph_index)

    if locator_type == "paragraph_after":
        q_block_index = _extract_q_block_index(meta)
        return _first_paragraph(
            _get_paragraph_from_blocks(blocks, q_block_index),
            _get_paragraph_from_blocks(blocks, paragraph_index),
            _get_paragraph_from_paragraphs(paragraphs, paragraph_index),
        )

    return None

def _find_anchor_indices(
    blocks: List[Union[Paragraph, Table]],
    block_to_para: Dict[int, int],
    paragraphs: List[Paragraph],
    anchor_para: Paragraph,
) -> Tuple[Optional[int], int]:
    for block_index, block in enumerate(blocks):
        if isinstance(block, Paragraph) and block is anchor_para:
            return block_index, block_to_para[block_index]
    return None, paragraphs.index(anchor_para)


def _collect_following_paragraphs_from_blocks(
    blocks: List[Union[Paragraph, Table]],
    start_index: int,
) -> List[Paragraph]:
    collected: List[Paragraph] = []
    for block in blocks[start_index:]:
        if isinstance(block, Paragraph):
            text = (block.text or "").strip()
            if _looks_like_question(text):
                break
            collected.append(block)
    return collected


def _collect_following_paragraphs_from_paragraphs(
    paragraphs: List[Paragraph],
    start_index: int,
) -> List[Paragraph]:
    collected: List[Paragraph] = []
    for paragraph in paragraphs[start_index:]:
        text = (paragraph.text or "").strip()
        if _looks_like_question(text):
            break
        collected.append(paragraph)
    return collected


def _ensure_paragraph_offset(
    anchor_para: Paragraph,
    collected: List[Paragraph],
    offset: int,
) -> Paragraph:
    if len(collected) >= offset:
        return collected[offset - 1]

    needed = offset - len(collected)
    dbg(f"Not enough following paragraphs: need to insert {needed} after anchor")
    last = collected[-1] if collected else anchor_para
    created = last
    for _ in range(needed):
        created = insert_paragraph_after(created, "")
        collected.append(created)
    return collected[offset - 1]


def get_target_paragraph_after_anchor(
    blocks: List[Union[Paragraph, Table]],
    block_to_para: Dict[int, int],
    paragraphs: List[Paragraph],
    anchor_para: Paragraph,
    offset: int
) -> Paragraph:
    anchor_block_index, anchor_para_index = _find_anchor_indices(
        blocks,
        block_to_para,
        paragraphs,
        anchor_para,
    )

    if anchor_block_index is not None:
        following = _collect_following_paragraphs_from_blocks(
            blocks,
            anchor_block_index + 1,
        )
    else:
        following = _collect_following_paragraphs_from_paragraphs(
            paragraphs,
            anchor_para_index + 1,
        )

    return _ensure_paragraph_offset(anchor_para, following, offset)

# ---------------------------- Apply operations ----------------------------
def _extract_answer_components(answer: object) -> Tuple[str, Dict[str, object]]:
    if isinstance(answer, dict):
        text = str(answer.get("text", ""))
        citations = _normalize_citations(answer.get("citations"))
    else:
        text = str(answer)
        citations = {}
    return text, citations


def _apply_answer_text(
    paragraph: Paragraph,
    answer_text: str,
    citations: Dict[str, object],
    *,
    append: bool = False,
) -> None:
    if append:
        paragraph.add_run("\n")
    else:
        paragraph.text = ""
    _add_text_with_citations(paragraph, answer_text, citations)


def _get_next_paragraph(target: Paragraph) -> Optional[Paragraph]:
    next_p_element = target._p.getnext()
    if next_p_element is not None and next_p_element.tag.endswith("p"):
        return Paragraph(next_p_element, target._parent)
    return None


def _replace_if_matching(
    paragraph: Paragraph,
    answer_text: str,
    citations: Dict[str, object],
    answer_norm: str,
    *,
    note: str,
) -> bool:
    if (paragraph.text or "").strip() != answer_norm:
        return False
    paragraph.text = ""
    _add_text_with_citations(paragraph, answer_text, citations)
    dbg(note)
    return True


def apply_to_paragraph(target: Paragraph, answer: object, mode: str = "fill") -> None:
    answer_text, citations = _extract_answer_components(answer)
    existing = target.text or ""

    if mode == "replace":
        _apply_answer_text(target, answer_text, citations, append=False)
        return

    if mode == "append":
        _apply_answer_text(target, answer_text, citations, append=bool(existing))
        return

    if is_blank_para(target) or not existing.strip():
        _apply_answer_text(target, answer_text, citations, append=False)
        return

    answer_norm = answer_text.strip()
    if _replace_if_matching(
        target,
        answer_text,
        citations,
        answer_norm,
        note="Replaced existing matching answer in target paragraph.",
    ):
        return

    next_para = _get_next_paragraph(target)
    if next_para and _replace_if_matching(
        next_para,
        answer_text,
        citations,
        answer_norm,
        note="Replaced matching answer in subsequent paragraph.",
    ):
        return

    new_p = insert_paragraph_after(target, "")
    _add_text_with_citations(new_p, answer_text, citations)
    dbg("Target paragraph not blank; appended answer in a new paragraph below.")

def apply_to_table_cell(tbl: Table, row: int, col: int, answer: object, mode: str = "fill") -> None:
    try:
        cell = tbl.cell(row, col)
    except Exception:
        return
    if isinstance(answer, dict):
        answer_text = str(answer.get("text", ""))
        citations = _normalize_citations(answer.get("citations"))
    else:
        answer_text = str(answer)
        citations = {}
    current = cell.text or ""
    if mode == "replace":
        cell.text = ""
        p = cell.paragraphs[0]
        _add_text_with_citations(p, answer_text, citations)
        return
    if current.strip():
        p = cell.paragraphs[-1]
        p.add_run().add_break()
        _add_text_with_citations(p, answer_text, citations)
    else:
        cell.text = ""
        p = cell.paragraphs[0]
        _add_text_with_citations(p, answer_text, citations)



def _validate_choice_meta(
    choices_meta: List[Dict[str, object]],
    index: Optional[int],
) -> Optional[Dict[str, object]]:
    if not isinstance(choices_meta, list):
        return None
    if index is None or not (0 <= index < len(choices_meta)):
        return None
    return choices_meta[index]


def _resolve_choice_paragraph(
    blocks: List[Union[Paragraph, Table]],
    meta: Dict[str, object],
) -> Optional[Paragraph]:
    block_index = int(meta.get("block_index", -1))
    if not (0 <= block_index < len(blocks)):
        return None
    paragraph = blocks[block_index]
    return paragraph if isinstance(paragraph, Paragraph) else None


def _determine_choice_style(style: Optional[str], prefix: str) -> str:
    if style not in (None, "", "auto"):
        return style
    if any(ch in prefix for ch in _CHECKBOX_CHARS):
        return "checkbox"
    trimmed = prefix.strip()
    if trimmed in ("()", "[]"):
        return "fill"
    return "highlight"


def _mark_checkbox_style(paragraph: Paragraph, text: str) -> None:
    paragraph.text = re.sub(rf"[{_CHECKBOX_CHARS}]", "☑", text, count=1)


def _mark_fill_style(paragraph: Paragraph, text: str, prefix: str) -> None:
    mark = prefix[0] + "X" + prefix[1]
    paragraph.text = text.replace(prefix, mark, 1)


def _mark_highlight_style(paragraph: Paragraph) -> None:
    for run in paragraph.runs:
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _mark_with_style(paragraph: Paragraph, style: str, prefix: str, text: str) -> None:
    if style == "checkbox" and any(ch in prefix for ch in _CHECKBOX_CHARS):
        _mark_checkbox_style(paragraph, text)
    elif style == "fill" and prefix.strip() in ("()", "[]"):
        _mark_fill_style(paragraph, text, prefix)
    elif style == "highlight":
        _mark_highlight_style(paragraph)
    else:
        paragraph.text = "X " + text


def _add_choice_comment(
    doc: docx.document.Document,
    paragraph: Paragraph,
    comment_text: Optional[str],
) -> None:
    if not comment_text:
        return
    run = paragraph.runs[0] if paragraph.runs else paragraph.add_run()
    try:
        add_comment_to_run(
            doc,
            run,
            comment_text,
            bold_prefix="Source Text: ",
            source_file=None,
        )
    except Exception as exc:
        dbg(f"  -> error adding comment: {exc}")


def mark_multiple_choice(
    doc: docx.document.Document,
    blocks: List[Union[Paragraph, Table]],
    choices_meta: List[Dict[str, object]],
    index: int,
    style: Optional[str] = None,
    comment_text: Optional[str] = None,
) -> None:
    meta = _validate_choice_meta(choices_meta, index)
    if meta is None:
        return

    paragraph = _resolve_choice_paragraph(blocks, meta)
    if paragraph is None:
        return

    prefix = str(meta.get("prefix", ""))
    text = paragraph.text or ""
    resolved_style = _determine_choice_style(style, prefix)

    _mark_with_style(paragraph, resolved_style, prefix, text)
    _add_choice_comment(doc, paragraph, comment_text)



def _extract_slot_metadata(slot: Dict[str, object]) -> Tuple[str, str, Dict[str, object]]:
    sid = str(slot.get("id", ""))
    question_text = str(slot.get("question_text") or "").strip()
    meta = slot.get("meta") or {}
    return sid, question_text, meta


def _resolve_existing_answer(
    sid: str,
    question_text: str,
    by_id: Dict[str, object],
    by_q: Dict[str, object],
) -> Optional[object]:
    if sid in by_id:
        return by_id[sid]
    key = normalize_question(question_text)
    return by_q.get(key)


def _prepare_generation_kwargs(
    slot: Dict[str, object],
    meta: Dict[str, object],
) -> Dict[str, object]:
    kwargs: Dict[str, object] = {}
    if slot.get("answer_type") == "multiple_choice":
        choice_meta = meta.get("choices", [])
        kwargs["choices"] = [
            c.get("text") if isinstance(c, dict) else str(c)
            for c in choice_meta
        ]
        kwargs["choice_meta"] = choice_meta
    return kwargs


def _queue_generation_job(
    jobs: List[Tuple[str, str, Dict[str, object]]],
    slot: Dict[str, object],
    sid: str,
    question_text: str,
    meta: Dict[str, object],
) -> None:
    kwargs = _prepare_generation_kwargs(slot, meta)
    jobs.append((sid, question_text, kwargs))


def _generate_missing_answers(
    jobs: List[Tuple[str, str, Dict[str, object]]],
    generator: Optional[Callable[..., object]],
    gen_name: str,
) -> Tuple[Dict[str, object], int]:
    if not jobs or generator is None:
        return {}, 0

    async def run_all() -> List[Tuple[str, Optional[object]]]:
        async def worker(sid: str, question: str, kwargs: Dict[str, object]):
            try:
                ans = await asyncio.to_thread(generator, question, **kwargs)
                dbg(f"Generated answer via {gen_name} for slot {sid}: {ans}")
                return sid, ans
            except Exception as exc:
                dbg(f"Generator error for question '{question}': {exc}")
                dbg(f"Generator error details: {traceback.format_exc()}")
                return sid, None

        tasks = [asyncio.create_task(worker(*job)) for job in jobs]
        return await asyncio.gather(*tasks)

    results = asyncio.run(run_all())
    updates: Dict[str, object] = {}
    generated = 0
    for sid, ans in results:
        if ans is not None:
            updates[sid] = ans
            generated += 1
    return updates, generated


def _build_answers_map(
    slots: List[Dict[str, object]],
    *,
    by_id: Dict[str, object],
    by_q: Dict[str, object],
    generator: Optional[Callable[..., object]],
    gen_name: str,
) -> Tuple[Dict[str, Optional[object]], int]:
    answers: Dict[str, Optional[object]] = {}
    jobs: List[Tuple[str, str, Dict[str, object]]] = []

    for slot in slots:
        sid, question_text, meta = _extract_slot_metadata(slot)
        answer = _resolve_existing_answer(sid, question_text, by_id, by_q)

        if answer is None and generator is not None:
            if not question_text:
                dbg(f"Skipping generation for slot {sid}: blank question text")
            else:
                _queue_generation_job(jobs, slot, sid, question_text, meta)

        answers[sid] = answer

    updates, generated = _generate_missing_answers(jobs, generator, gen_name)
    for sid, value in updates.items():
        answers[sid] = value

    return answers, generated



def _format_citation_comment(raw: object) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    parts: List[str] = []
    for value in raw.values():
        if isinstance(value, dict):
            snippet = (
                value.get("text")
                or value.get("snippet")
                or value.get("content")
                or ""
            )
            source_file = value.get("source_file")
            piece = str(snippet)
            if source_file:
                piece += f"\nSource File:\n {source_file}"
            parts.append(piece)
        else:
            parts.append(str(value))
    return "\n\n".join(parts) if parts else None


def _apply_multiple_choice_slot(
    *,
    doc: docx.document.Document,
    blocks: List[Union[Paragraph, Table]],
    slot: Dict[str, object],
    answer: Dict[str, object],
) -> str:
    choice_meta = (slot.get("meta") or {}).get("choices", [])
    if not choice_meta:
        dbg("  -> no choice metadata present")
        return "bad_locator"
    idx = answer.get("choice_index")
    if idx is None:
        dbg("  -> could not resolve selected choice index")
        return "bad_locator"
    style = answer.get("style")
    comment_text = _format_citation_comment(answer.get("citations"))
    try:
        mark_multiple_choice(
            doc,
            blocks,
            choice_meta,
            int(idx),
            style,
            comment_text,
        )
        dbg("  -> marked choice in-place")
        return "applied"
    except Exception as exc:
        dbg(f"  -> error marking multiple choice: {exc}")
        return "bad_locator"


def _apply_table_cell_slot(
    *,
    doc: docx.document.Document,
    locator: Dict[str, object],
    answer: object,
    mode: str,
) -> str:
    t_index = locator.get("table_index")
    if t_index is None:
        dbg("  -> bad locator: missing table_index")
        return "bad_locator"
    try:
        table_idx = int(t_index)
        row = int(locator.get("row", 0))
        col = int(locator.get("col", 0))
    except Exception:
        dbg("  -> invalid table coordinates")
        return "bad_locator"
    if not (0 <= table_idx < len(doc.tables)):
        dbg(
            f"  -> table_index {table_idx} out of bounds; have {len(doc.tables)} tables"
        )
        return "table_oob"
    tbl = doc.tables[table_idx]
    apply_to_table_cell(tbl, row, col, answer, mode=mode)
    dbg(f"  -> wrote into table[{table_idx}] cell({row},{col})")
    return "applied"


def _apply_paragraph_slot(
    *,
    doc: docx.document.Document,
    blocks: List[Union[Paragraph, Table]],
    paragraphs: List[Paragraph],
    block_to_para: Dict[int, int],
    locator: Dict[str, object],
    meta: Dict[str, object],
    answer: object,
    mode: str,
    ltype: str,
) -> str:
    anchor = resolve_anchor_paragraph(doc, blocks, paragraphs, block_to_para, locator, meta)
    if anchor is None:
        dbg("  -> could not resolve anchor/target paragraph")
        return "bad_locator"

    force_after_question = bool(meta.get("force_insert_after_question"))
    if ltype == "paragraph_after":
        offset = int(locator.get("offset", 1) or 1)
        if force_after_question:
            target = insert_paragraph_after(anchor, "")
        else:
            target = get_target_paragraph_after_anchor(
                blocks,
                block_to_para,
                paragraphs,
                anchor,
                offset,
            )
    else:
        target = anchor

    apply_to_paragraph(target, answer, mode=mode)
    dbg("  -> wrote into paragraph")
    return "applied"


def _apply_slot_to_doc(
    slot: Dict[str, object],
    answer: object,
    *,
    doc: docx.document.Document,
    blocks: List[Union[Paragraph, Table]],
    paragraphs: List[Paragraph],
    block_to_para: Dict[int, int],
    mode: str,
) -> str:
    answer_type = slot.get("answer_type")
    meta = slot.get("meta") or {}
    locator = slot.get("answer_locator") or {}
    ltype = str(locator.get("type", ""))

    if (
        answer_type == "multiple_choice"
        and isinstance(answer, dict)
        and "choice_index" in answer
    ):
        return _apply_multiple_choice_slot(
            doc=doc,
            blocks=blocks,
            slot=slot,
            answer=answer,
        )

    dbg(f"Applying answer for slot {slot.get('id', '')} (type={ltype})")

    if ltype == "table_cell":
        return _apply_table_cell_slot(
            doc=doc,
            locator=locator,
            answer=answer,
            mode=mode,
        )
    if ltype in ("paragraph_after", "paragraph"):
        return _apply_paragraph_slot(
            doc=doc,
            blocks=blocks,
            paragraphs=paragraphs,
            block_to_para=block_to_para,
            locator=locator,
            meta=meta,
            answer=answer,
            mode=mode,
            ltype=ltype,
        )
    dbg(f"  -> unsupported locator type: {ltype}")
    return "bad_locator"


# ---------------------------- Main application flow ----------------------------
def apply_answers_to_docx(
    docx_path: str,
    slots_json_path: str,
    answers_json_path: str,
    out_path: str,
    mode: str = "fill",
    generator: Optional[Callable[..., object]] = None,
    gen_name: str = ""
) -> Dict[str, int]:
    with open(slots_json_path, "r", encoding="utf-8") as f:
        slots_payload = json.load(f)

    by_id, by_q = ({}, {})
    if answers_json_path and answers_json_path != "-" and os.path.isfile(answers_json_path):
        by_id, by_q = load_answers(answers_json_path)
        dbg(f"Answers loaded: by_id={len(by_id)}, by_question={len(by_q)}")
    else:
        dbg("No answers file provided; relying solely on generator (if any)")

    doc = docx.Document(docx_path)
    blocks, paragraphs, block_to_para, block_to_table = build_indexes(doc)

    applied = 0
    skipped_no_answer = 0
    skipped_bad_locator = 0
    skipped_table_oob = 0
    generated = 0

    slots = (slots_payload or {}).get("slots", [])

    answers, generated = _build_answers_map(
        slots,
        by_id=by_id,
        by_q=by_q,
        generator=generator,
        gen_name=gen_name,
    )

    for s in slots:
        sid = s.get("id", "")
        question_text = (s.get("question_text") or "").strip()
        answer = answers.get(sid)
        if answer is None:
            dbg(f"NO ANSWER for slot {sid!r} / question '{question_text}' — skipping")
            skipped_no_answer += 1
            continue
        try:
            status = _apply_slot_to_doc(
                s,
                answer,
                doc=doc,
                blocks=blocks,
                paragraphs=paragraphs,
                block_to_para=block_to_para,
                mode=mode,
            )
        except Exception as exc:
            dbg(f"  -> error while applying slot {sid}: {exc}")
            status = "bad_locator"

        if status == "applied":
            applied += 1
        elif status == "table_oob":
            skipped_table_oob += 1
        else:
            skipped_bad_locator += 1

    doc.save(out_path)
    print(f"Wrote {out_path}")

    return {
        "applied": applied,
        "skipped_no_answer": skipped_no_answer,
        "skipped_bad_locator": skipped_bad_locator,
        "skipped_table_oob": skipped_table_oob,
        "total_slots": len(slots),
        "generated": generated,
    }


def _parse_arguments(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Apply answers into a DOCX using slots.json")
    ap.add_argument("docx_path", help="Path to the original .docx")
    ap.add_argument("slots_json", help="Path to slots.json produced by the detector")
    ap.add_argument(
        "answers_json",
        nargs="?",
        default="",
        help="Path to answers.json (optional if using --generate)",
    )
    ap.add_argument("-o", "--out", required=True, help="Path to write updated .docx")
    ap.add_argument(
        "--mode",
        choices=["replace", "append", "fill"],
        default="fill",
        help="Write mode for paragraphs/cells (default: fill)",
    )
    ap.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        default=True,
        help="Verbose debug logging (default on)",
    )
    ap.add_argument(
        "--no-debug",
        dest="debug",
        action="store_false",
        help="Disable debug logging",
    )
    ap.add_argument(
        "--generate",
        metavar="MODULE:FUNC",
        help=(
            "Dynamically generate answers by calling given function for each question"
            " (e.g. backend.application.generation.my_module:gen_answer)"
        ),
    )
    if len(argv) == 1:
        ap.print_help()
        sys.exit(1)
    return ap.parse_args(argv[1:])


def _validate_input_paths(docx_path: str, slots_json: str, debug: bool) -> None:
    required_paths = [docx_path, slots_json]
    for path in required_paths:
        if not os.path.isfile(path):
            print(f"Error: '{path}' does not exist.", file=sys.stderr)
            sys.exit(1)
    if debug:
        print("[apply_answers] validated input paths")


def _maybe_load_generator(spec: Optional[str], debug: bool) -> Tuple[Optional[Callable[..., object]], str]:
    if not spec:
        return None, ""
    if ":" not in spec:
        print("Error: --generate requires MODULE:FUNC", file=sys.stderr)
        sys.exit(1)
    module_name, func_name = spec.split(":", 1)
    try:
        module: ModuleType = importlib.import_module(module_name)
        func = getattr(module, func_name)
        if not callable(func):
            raise AttributeError
        if debug:
            print(f"Loaded generator function {spec}")
        return func, spec
    except Exception as exc:
        print(f"Error: failed to load generator function {spec}: {exc}", file=sys.stderr)
        sys.exit(1)


def _validate_answers_path(path: str, generate_spec: Optional[str]) -> None:
    if not path or path == "-":
        return
    if os.path.isfile(path):
        return
    if generate_spec:
        return
    print(
        f"Error: answers file '{path}' does not exist and no --generate specified.",
        file=sys.stderr,
    )
    sys.exit(1)


def _print_debug_header(args: argparse.Namespace) -> None:
    print("### APPLY DEBUG MODE ON ###")
    print(f"[apply_answers] source={args.docx_path} slots={args.slots_json}")


def _print_summary(summary: Dict[str, int]) -> None:
    print("--- APPLY SUMMARY ---")
    for key, value in summary.items():
        print(f"{key}: {value}")

# ---------------------------- CLI ----------------------------

def main():
    args = _parse_arguments(sys.argv)

    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        _print_debug_header(args)

    _validate_input_paths(args.docx_path, args.slots_json, DEBUG)
    _validate_answers_path(args.answers_json, args.generate)
    generator, gen_name = _maybe_load_generator(args.generate, DEBUG)

    try:
        if DEBUG:
            print("[apply_answers] applying answers to document")
        summary = apply_answers_to_docx(
            args.docx_path,
            args.slots_json,
            args.answers_json,
            args.out,
            mode=args.mode,
            generator=generator,
            gen_name=gen_name,
        )
    except Exception as exc:
        print(f"Error: failed to apply answers: {exc}", file=sys.stderr)
        sys.exit(1)

    if DEBUG:
        _print_summary(summary)
if __name__ == "__main__":
    main()
