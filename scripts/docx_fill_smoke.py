import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.documents.extraction import QuestionExtractor
from backend.answering.lite_answering import generate_answers
from backend.retrieval import AzureSearchStack
from backend.documents.docx.apply_answers import apply_answers_to_docx

DOCX_PATH = Path(r"C:\Users\r ohernan\Downloads\AIP_ADIC_Sample 1 (1).docx")
OUTPUT_PATH = DOCX_PATH.with_name("filled_document.docx")
FUND_FILTER = ["Rodrigo", "Fund - GMF V"]
USE_LIVE_LLM = True
K_SNIPPETS = 6
MIN_CONFIDENCE = 0.0

extraction_client = QuestionExtractor()
retrieval_client = AzureSearchStack()
llm_kwargs = {"fund_filter": FUND_FILTER, "k": K_SNIPPETS, "min_confidence": MIN_CONFIDENCE}

print(
    retrieval_client.search(
        "How do you decide which investments to take part on?",
        fund_filter=FUND_FILTER,
    )
)

with open(DOCX_PATH, "rb") as f:
    question_slots = extraction_client.extract(f)

all_context = retrieval_client.search_batch(
    [slot.get("question", "").strip() for slot in question_slots],
    fund_filter=FUND_FILTER,
    k=K_SNIPPETS,
)
for slot, context in zip(question_slots, all_context):
    slot["contextSnippets"] = context

answers = generate_answers(
    questions=[slot.get("question", "") for slot in question_slots],
    all_context=[slot.get("contextSnippets", []) for slot in question_slots],
    fund_filter=FUND_FILTER,
    k=K_SNIPPETS,
    min_confidence=MIN_CONFIDENCE,
)
for slot, answer in zip(question_slots, answers):
    slot["answer"] = answer

with open(DOCX_PATH, "rb") as f:
    filled_bytes, summary = apply_answers_to_docx(
        docx_source=f,
        slots=question_slots,
    )

with open(OUTPUT_PATH, "wb") as out_file:
    out_file.write(filled_bytes)

print(summary)
