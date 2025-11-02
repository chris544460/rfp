import os
import re
import json
from typing import Any, Dict, List, Optional, Union

import pandas as pd
import docx
from docx.text.paragraph import Paragraph
from docx.table import Table


def _apply_tag_override(records: List[Dict[str, Any]], override_tags: Optional[List[str]]) -> None:
    """Override the ``tags`` key for every record when tags are provided."""

    if not override_tags:
        return

    normalized = [tag.strip() for tag in override_tags if tag and tag.strip()]
    if not normalized:
        return

    for record in records:
        if isinstance(record, dict):
            record["tags"] = list(normalized)


def iter_block_items(parent):
    """
    Generator yielding paragraphs and tables in reading order from a DOCX document or element.
    """
    if hasattr(parent, "element"):
        elm = parent.element.body if hasattr(parent.element, "body") else parent.element
    else:
        elm = parent
    for child in elm:
        if child.tag.endswith("}p"):
            yield Paragraph(child, parent)
        elif child.tag.endswith("}tbl"):
            yield Table(child, parent)


class ExcelQuestionnaireParser:
    """
    Parses a two-column questionnaire Excel file into JSON-friendly records.
    Column 0: question key (ignored), Column 1: question text and subsequent answer lines.
    """
    def __init__(self,
                 file_path: str,
                 sheet_name: Optional[Union[str, int]] = 0,
                 section: Optional[str] = None):
        self.file_path = file_path
        self.sheet_name = sheet_name
        self.section = section or "Document"
        self.records: List[Dict[str, Optional[str]]] = []

    def parse(self) -> List[Dict[str, Optional[str]]]:
        df = pd.read_excel(self.file_path, sheet_name=self.sheet_name, header=None)
        i, n_rows = 0, len(df)
        while i < n_rows:
            key_cell = df.iat[i, 0]
            text_cell = df.iat[i, 1]
            if pd.notna(key_cell) and pd.notna(text_cell):
                question = str(text_cell).strip()
                answer_parts: List[str] = []
                i += 1
                # collect answer lines
                while i < n_rows and pd.isna(df.iat[i, 0]):
                    ans = df.iat[i, 1]
                    if pd.notna(ans):
                        answer_parts.append(str(ans).strip())
                    i += 1
                self.records.append({
                    "source": self.file_path,
                    "section": self.section,
                    "field": question,
                    "value": "\n".join(answer_parts)
                })
            else:
                i += 1
        return self.records

    def to_json(
        self,
        output_path: str,
        override_tags: Optional[List[str]] = None,
    ) -> None:
        if not self.records:
            self.parse()
        _apply_tag_override(self.records, override_tags)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.records, f, indent=2, ensure_ascii=False)


class ExcelAnswerLibraryParser:
    """
    Parses an Answer Library Excel file with columns:
      - 'ID'
      - 'Question'
      - 'Answer_Response*' columns
      - optional 'Alternate Questions', 'Answer_No/Yes', 'Section Name', 'Tags'
    """
    def __init__(self,
                 file_path: str,
                 sheet_name: Optional[Union[str, int]] = 0):
        self.file_path = file_path
        self.sheet_name = sheet_name
        self.records: List[Dict[str, Union[str, List[str]]]] = []

    def parse(self) -> List[Dict[str, Union[str, List[str]]]]:
        df = pd.read_excel(self.file_path, sheet_name=self.sheet_name)
        answer_cols = [c for c in df.columns if str(c).startswith("Answer_Response")]
        for _, row in df.iterrows():
            rec: Dict[str, Union[str, List[str]]] = {
                "source": self.file_path,
                "id": str(row.get("ID", "")).strip(),
                "question": str(row.get("Question", "")).strip(),
                "alternate_questions": [],
                "answers": []
            }
            # alternate questions
            alt_q = row.get("Alternate Questions")
            if pd.notna(alt_q):
                rec["alternate_questions"] = [
                    a.strip() for a in str(alt_q).split(";") if a.strip()
                ]
            # answer responses
            for col in answer_cols:
                val = row.get(col)
                if pd.notna(val) and str(val).strip():
                    rec["answers"].append(str(val).strip())
            # yes/no
            yn = row.get("Answer_No/Yes")
            if pd.notna(yn):
                rec["yes_no"] = str(yn).strip()
            # section name
            sec = row.get("Section Name")
            if pd.notna(sec):
                rec["section"] = str(sec).strip()
            # tags
            tags = row.get("Tags")
            if pd.notna(tags):
                rec["tags"] = [t.strip() for t in str(tags).split(";") if t.strip()]
            self.records.append(rec)
        return self.records

    def to_json(
        self,
        output_path: str,
        override_tags: Optional[List[str]] = None,
    ) -> None:
        if not self.records:
            self.parse()
        _apply_tag_override(self.records, override_tags)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.records, f, indent=2, ensure_ascii=False)


class MixedDocParser:
    """
    Single-pass DOCX parser. Captures:
      - Headings (by style or numeric pattern)
      - Normal paragraphs
      - 2-column Q&A tables
      - Multi-column data tables
    """
    HEADING_PATTERN = re.compile(r"^(\d+(?:\.\d+)+)\s+.*")

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.current_section: str = "Document"
        self.records: List[Dict[str, Union[str, Dict[str, str]]]] = []

    def parse(self) -> List[Dict[str, Union[str, Dict[str, str]]]]:
        doc = docx.Document(self.file_path)
        for block in iter_block_items(doc):
            if isinstance(block, Paragraph):
                self._handle_paragraph(block)
            elif isinstance(block, Table):
                self._handle_table(block)
        return self.records

    def _handle_paragraph(self, paragraph: Paragraph):
        text = paragraph.text.strip()
        if not text:
            return
        style = paragraph.style.name.lower() if paragraph.style else ""
        if style.startswith("heading") or self.HEADING_PATTERN.match(text):
            # treat as heading
            self.current_section = text
            self.records.append({
                "source": self.file_path,
                "type": "heading",
                "section": text
            })
        else:
            # normal paragraph
            self.records.append({
                "source": self.file_path,
                "type": "paragraph",
                "section": self.current_section,
                "text": text
            })

    def _handle_table(self, table: Table):
        n_cols = len(table.columns)
        if n_cols == 2:
            self._parse_2col_qa(table)
        else:
            self._parse_multi_col(table)

    def _parse_2col_qa(self, table: Table):
        current_q: Optional[str] = None
        current_a: List[str] = []

        def flush():
            nonlocal current_q, current_a
            if current_q is not None:
                self.records.append({
                    "source": self.file_path,
                    "type": "table_qa",
                    "section": self.current_section,
                    "field": current_q,
                    "value": "\n".join(current_a).strip()
                })

        for row in table.rows:
            col0 = row.cells[0].text.strip()
            col1 = row.cells[1].text.strip()
            if col0 and col1:
                flush()
                current_q, current_a = col0, [col1]
            elif not col0 and col1 and current_q is not None:
                current_a.append(col1)
            elif col0 and not col1:
                flush()
                current_q, current_a = col0, []
        flush()

    def _parse_multi_col(self, table: Table):
        # header row
        headers = [c.text.strip() for c in table.rows[0].cells]
        for idx, row in enumerate(table.rows[1:], start=1):
            data = {headers[i]: row.cells[i].text.strip() for i in range(len(headers))}
            self.records.append({
                "source": self.file_path,
                "type": "table_data",
                "section": self.current_section,
                "row_index": idx,
                "data": data
            })

    def to_json(
        self,
        output_path: str,
        override_tags: Optional[List[str]] = None,
    ) -> None:
        if not self.records:
            self.parse()
        _apply_tag_override(self.records, override_tags)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.records, f, indent=2, ensure_ascii=False)


class LoopioExcelParser:
    """
    Parses Loopio-formatted Excel into a JSON library of Q&A entries.
    Expects columns (case-insensitive):
      - 'Library Entry Id'
      - 'Question *'
      - 'Answer *'
      - optional 'Stack', 'Category', 'Sub-Category', 'Tags', 'Alternate Question 1-5'
    """
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.records: List[Dict[str, Union[str, List[str]]]] = []

    def parse(self) -> List[Dict[str, Union[str, List[str]]]]:
        df = pd.read_excel(self.file_path, engine="openpyxl")
        df.columns = [
            c.lower().strip() if isinstance(c, str) else c
            for c in df.columns
        ]
        for _, row in df.iterrows():
            q = str(row.get("question *", "")).strip()
            a = str(row.get("answer *", "")).strip()
            if not q or not a:
                continue
            answers = [ans.strip() for ans in a.split(";") if ans.strip()] or [a]
            tags = [
                t.strip() for t in str(row.get("stack", "")).split(",") if t.strip()
            ]
            cat = str(row.get("category", "")).strip()
            sub = str(row.get("sub-category", "")).strip()
            section = "General"
            if cat and sub:
                section = f"{cat} > {sub}"
            elif cat:
                section = cat
            # alternate questions
            alts: List[str] = []
            for i in range(1, 6):
                col = f"alternate question {i}"
                v = str(row.get(col, "")).strip()
                if v and v.lower() != "nan":
                    alts.append(v)
            entry_id = str(row.get("library entry id", f"loopio_{len(self.records)}")).strip()

            self.records.append({
                "id": entry_id,
                "question": q,
                "answers": answers,
                "section": section,
                "tags": tags,
                "source": os.path.basename(self.file_path),
                "alternate_questions": alts
            })
        return self.records

    def to_json(
        self,
        output_path: str,
        override_tags: Optional[List[str]] = None,
    ) -> None:
        if not self.records:
            self.parse()
        _apply_tag_override(self.records, override_tags)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.records, f, indent=2, ensure_ascii=False)


def detect_and_parse_excel_file(
    file_path: str,
    output_dir: str = "./parsed_json_outputs",
    override_tags: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Detects which Excel parser to use based on worksheet headers/content.

    Returns the detected parser name ("loopio", "answer_library", or "questionnaire")
    after writing JSON output. If no parser can be confidently chosen, returns None
    so the caller can fall back to heuristic parsing.
    """
    os.makedirs(output_dir, exist_ok=True)

    def _serialize(parser, output_path: str, success_message: str) -> bool:
        records = parser.parse()
        if not records:
            return False
        parser.to_json(output_path, override_tags=override_tags)
        print(success_message.format(count=len(records), path=output_path))
        if override_tags:
            printable = ", ".join([tag.strip() for tag in override_tags if tag.strip()])
            if printable:
                print(f"Applied override tags [{printable}] to {len(records)} records.")
        return True

    try:
        sample = pd.read_excel(file_path, engine="openpyxl", nrows=5)
    except Exception as exc:
        print(f"Error processing {file_path}: {exc}")
        return None

    cols = [str(c).lower().strip() for c in sample.columns if pd.notna(c)]
    basename = os.path.basename(file_path)
    stem = os.path.splitext(basename)[0]

    # Loopio export detection
    loopio_indicators = {"library entry id", "question *", "answer *"}
    if loopio_indicators.issubset(set(cols)):
        print(f"Using Loopio export parser for {file_path}")
        parser = LoopioExcelParser(file_path)
        out_path = os.path.join(output_dir, f"{stem}.json")
        if _serialize(
            parser,
            out_path,
            "Detected Loopio format: created {count} records at {path}"
        ):
            return "loopio"
        print(f"Loopio indicators found but no records parsed for {file_path}")
        return None

    # Answer library export detection
    has_answer_cols = any(col.startswith("answer_response") for col in cols)
    if "question" in cols and has_answer_cols:
        print(f"Using responsive export parser for {file_path}")
        parser = ExcelAnswerLibraryParser(file_path)
        out_path = os.path.join(output_dir, f"answers_{basename}.json")
        if _serialize(
            parser,
            out_path,
            "Answer library parsed: {count} records at {path}"
        ):
            return "answer_library"
        print(f"Responsive export indicators found but no records parsed for {file_path}")
        return None

    # Questionnaire detection – look for exactly two populated columns when
    # ignoring header rows. This format typically stores keys in column 0 and
    # question/answer text in column 1.
    try:
        sample_no_header = pd.read_excel(
            file_path, engine="openpyxl", header=None, nrows=10
        )
    except Exception:
        sample_no_header = None

    if sample_no_header is not None:
        populated_columns = []
        for col in sample_no_header.columns:
            col_values = [
                str(val).strip()
                for val in sample_no_header[col].dropna()
                if str(val).strip()
            ]
            if col_values:
                populated_columns.append(col)

        if len(populated_columns) == 2:
            print(f"Using questionnaire export parser for {file_path}")
            parser = ExcelQuestionnaireParser(file_path)
            out_path = os.path.join(output_dir, f"questionnaire_{basename}.json")
            if _serialize(
                parser,
                out_path,
                "Questionnaire parsed: {count} records at {path}"
            ):
                return "questionnaire"
            print(f"Questionnaire indicators found but no records parsed for {file_path}")

    print(f"Could not auto-detect parser for {file_path}")
    return None


def process_standard_excel_file(
    file_path: str,
    output_dir: str = "./parsed_json_outputs",
    override_tags: Optional[List[str]] = None,
) -> None:
    """
    Fallback to process questionnaire or answer library Excel.
    """
    # Try questionnaire first
    q_parser = ExcelQuestionnaireParser(file_path)
    q_recs = q_parser.parse()
    if q_recs:
        print(f"Using questionnaire export parser for {file_path}")
        q_out = os.path.join(output_dir, f"questionnaire_{os.path.basename(file_path)}.json")
        q_parser.to_json(q_out, override_tags=override_tags)
        print(f"Questionnaire parsed: {len(q_recs)} records at {q_out}")
        if override_tags:
            printable = ", ".join([tag.strip() for tag in override_tags if tag.strip()])
            if printable:
                print(f"Applied override tags [{printable}] to {len(q_recs)} records.")
        return

    # Else try answer library
    a_parser = ExcelAnswerLibraryParser(file_path)
    a_recs = a_parser.parse()
    if a_recs:
        print(f"Using responsive export parser for {file_path}")
        a_out = os.path.join(output_dir, f"answers_{os.path.basename(file_path)}.json")
        a_parser.to_json(a_out, override_tags=override_tags)
        print(f"Answer library parsed: {len(a_recs)} records at {a_out}")
        if override_tags:
            printable = ", ".join([tag.strip() for tag in override_tags if tag.strip()])
            if printable:
                print(f"Applied override tags [{printable}] to {len(a_recs)} records.")
    else:
        print(f"No records parsed from {file_path}")


def process_excel_file_with_detection(
    file_path: str,
    output_dir: str = "./parsed_json_outputs",
    override_tags: Optional[List[str]] = None,
) -> None:
    """
    Attempts Loopio detection, else falls back to standard Excel parsers.
    """
    detected = detect_and_parse_excel_file(file_path, output_dir, override_tags=override_tags)
    if detected is None:
        process_standard_excel_file(file_path, output_dir, override_tags=override_tags)


if __name__ == "__main__":
    # Example usage:
    # process_excel_file_with_detection("YourFile.xlsx")
    # parser = MixedDocParser("YourDoc.docx")
    # records = parser.parse()
    # parser.to_json("out_doc.json")
    pass
