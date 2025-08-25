import streamlit as st
import os
import tempfile
import json
import re
from pathlib import Path
from typing import List, Optional

from cli_app import (
    load_input_text,
    extract_questions,
    build_docx,
)
from qa_core import answer_question
from answer.answer_composer import CompletionsClient
from input_file_reader.interpreter_sheet import collect_non_empty_cells
from rfp_xlsx_slot_finder import ask_sheet_schema
from rfp_xlsx_apply_answers import write_excel_answers
from rfp_docx_slot_finder import extract_slots_from_docx
from rfp_docx_apply_answers import apply_answers_to_docx


def save_uploaded_file(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded_file.read())
    tmp.flush()
    return tmp.name


def build_generator(
    search_mode: str,
    fund: Optional[str],
    k: int,
    length: Optional[str],
    approx_words: Optional[int],
    min_confidence: float,
    include_citations: bool,
    llm: CompletionsClient,
    extra_docs: Optional[List[str]] = None,
):
    def gen(question: str):
        ans, cmts = answer_question(
            question,
            search_mode,
            fund,
            k,
            length,
            approx_words,
            min_confidence,
            llm,
            extra_docs=extra_docs,
        )
        if not include_citations:
            ans = re.sub(r"\[\d+\]", "", ans)
            return ans
        citations = {
            lbl: {"text": snippet, "source_file": src}
            for lbl, src, snippet, score, date in cmts
        }
        return {"text": ans, "citations": citations}

    return gen


def main():
    st.title("RFP Responder")

    uploaded = st.file_uploader(
        "Upload RFP file", type=["pdf", "docx", "doc", "txt", "xlsx", "xls"]
    )

    fund = st.text_input("Fund tag filter") or None
    search_mode = st.selectbox(
        "Search mode", ["answer", "question", "blend", "dual", "both"], index=3
    )
    llm_model = st.selectbox(
        "LLM model", ["gpt-3.5-turbo", "gpt-4", "gpt-4o"], index=2
    )
    length_mode = st.radio("Answer length mode", ["Preset", "Custom word count"])
    if length_mode == "Preset":
        length_opt = st.selectbox(
            "Preset length", ["short", "medium", "long"], index=1
        )
        approx_words = None
    else:
        length_opt = None
        approx_words = st.number_input(
            "Approximate word count", min_value=1, value=150
        )
    k_max_hits = st.number_input("Hits per question", min_value=1, value=6)
    min_confidence = st.number_input("Min confidence", value=0.0)
    include_citations = st.checkbox("Include citations with comments", value=True)
    docx_as_text = st.checkbox("Treat DOCX as text", value=False)
    docx_write_mode = st.selectbox(
        "DOCX write mode", ["fill", "replace", "append"], index=0
    )

    extra_uploads = st.file_uploader(
        "Additional documents", type=["pdf", "docx", "txt"], accept_multiple_files=True
    )

    if st.button("Run") and uploaded is not None:
        input_path = save_uploaded_file(uploaded)
        extra_docs = [save_uploaded_file(f) for f in extra_uploads] if extra_uploads else None
        llm = CompletionsClient(model=llm_model)
        suffix = Path(uploaded.name).suffix.lower()

        if suffix in (".xlsx", ".xls"):
            cells = collect_non_empty_cells(input_path)
            schema = ask_sheet_schema(input_path)
            gen = build_generator(
                search_mode,
                fund,
                int(k_max_hits),
                length_opt,
                int(approx_words) if approx_words else None,
                float(min_confidence),
                include_citations,
                llm,
                extra_docs,
            )
            answers = [gen((entry.get("question_text") or "").strip()) for entry in schema]
            out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
            write_excel_answers(
                schema,
                answers,
                input_path,
                out_tmp.name,
                include_comments=include_citations,
            )
            with open(out_tmp.name, "rb") as f:
                st.download_button(
                    "Download answered workbook",
                    f,
                    file_name=Path(uploaded.name).stem + "_answered.xlsx",
                )
        elif suffix == ".docx" and not docx_as_text:
            slots = extract_slots_from_docx(input_path)
            slots_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
            json.dump(slots, slots_tmp)
            slots_tmp.flush()
            gen = build_generator(
                search_mode,
                fund,
                int(k_max_hits),
                length_opt,
                int(approx_words) if approx_words else None,
                float(min_confidence),
                include_citations,
                llm,
                extra_docs,
            )
            out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
            apply_answers_to_docx(
                docx_path=input_path,
                slots_json_path=slots_tmp.name,
                answers_json_path="",
                out_path=out_tmp.name,
                mode=docx_write_mode,
                generator=gen,
                gen_name="streamlit_app:rag_gen",
            )
            with open(out_tmp.name, "rb") as f:
                st.download_button(
                    "Download answered DOCX",
                    f,
                    file_name=Path(uploaded.name).stem + "_answered.docx",
                )
        else:
            raw = load_input_text(input_path)
            questions = extract_questions(raw, llm)
            answers = []
            comments = []
            for q in questions:
                ans, cmts = answer_question(
                    q,
                    search_mode,
                    fund,
                    int(k_max_hits),
                    length_opt,
                    int(approx_words) if approx_words else None,
                    float(min_confidence),
                    llm,
                )
                if not include_citations:
                    ans = re.sub(r"\[\d+\]", "", ans)
                    cmts = []
                answers.append(ans)
                comments.append(cmts)
            qa_doc = build_docx(
                questions,
                answers,
                comments,
                include_comments=include_citations,
            )
            out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
            out_tmp.write(qa_doc)
            out_tmp.flush()
            with open(out_tmp.name, "rb") as f:
                st.download_button(
                    "Download Q/A report",
                    f,
                    file_name=Path(uploaded.name).stem + "_answered.docx",
                )


if __name__ == "__main__":
    main()
