import os, json
import sys, pathlib
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from dataclasses import asdict
import docx
from backend.rfp_docx_slot_finder import (
    detect_para_question_with_blank,
    detect_two_col_table_q_blank,
    detect_response_label_then_blank,
    _iter_block_items,
    attach_context,
    dedupe_slots,
    QASlot,
    AnswerLocator
)
from backend.rfp_docx_apply_answers import apply_answers_to_docx

def build_slots(doc_path):
    doc = docx.Document(doc_path)
    blocks = list(_iter_block_items(doc))
    slots = []
    for detector in (
        detect_para_question_with_blank,
        detect_two_col_table_q_blank,
        detect_response_label_then_blank,
    ):
        slots.extend(detector(blocks))
    attach_context(slots, blocks)
    slots = dedupe_slots(slots)
    return {"doc_type": "docx", "file": os.path.basename(doc_path), "slots": [asdict(s) for s in slots]}

def test_pipeline_idempotent(tmp_path):
    answer_text = "Our approach is outstanding."
    doc = docx.Document()
    doc.add_paragraph("Please describe your approach.")
    doc.add_paragraph("")
    src = tmp_path / "orig.docx"
    doc.save(src)
    slots_payload = build_slots(src)
    slots_path = tmp_path / "slots.json"
    with open(slots_path, "w", encoding="utf-8") as f:
        json.dump(slots_payload, f)
    slot_id = slots_payload["slots"][0]["id"]
    answers_path = tmp_path / "answers.json"
    with open(answers_path, "w", encoding="utf-8") as f:
        json.dump({slot_id: answer_text}, f)
    first = tmp_path / "out1.docx"
    apply_answers_to_docx(str(src), str(slots_path), str(answers_path), str(first))
    second = tmp_path / "out2.docx"
    apply_answers_to_docx(str(first), str(slots_path), str(answers_path), str(second))
    final_doc = docx.Document(second)
    texts = [p.text.strip() for p in final_doc.paragraphs]
    assert texts.count(answer_text) == 1

def test_dedupe_overlapping_ranges():
    s1 = QASlot(
        id="1",
        question_text="1. Please describe your approach.",
        answer_locator=AnswerLocator(type="paragraph", paragraph_index=5),
    )
    s2 = QASlot(
        id="2",
        question_text="Please describe your approach.",
        answer_locator=AnswerLocator(type="paragraph_after", paragraph_index=4, offset=2),
    )
    deduped = dedupe_slots([s1, s2])
    assert len(deduped) == 1


def test_blank_search_stops_before_next_question(tmp_path):
    doc = docx.Document()
    doc.add_paragraph("Question one?")
    doc.add_paragraph("Question two?")
    doc.add_paragraph("")
    src = tmp_path / "two_questions.docx"
    doc.save(src)
    loaded = docx.Document(src)
    blocks = list(_iter_block_items(loaded))
    slots = detect_para_question_with_blank(blocks)
    assert len(slots) == 1
    assert slots[0].question_text.strip() == "Question two?"
