import openpyxl
from rfp_xlsx_slot_finder import extract_slots_from_xlsx, extract_schema_from_xlsx
from rfp_xlsx_apply_answers import write_excel_answers


def test_extract_slots_from_xlsx(tmp_path):
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Sheet1"
    c = ws1["A1"]
    c.value = "Question?"
    c.font = openpyxl.styles.Font(color="FFFF0000", bold=True)
    c.fill = openpyxl.styles.PatternFill("solid", fgColor="FFFFFF00")
    ws2 = wb.create_sheet("Data")
    ws2["B2"] = "Answer"

    path = tmp_path / "sample.xlsx"
    wb.save(path)

    result = extract_slots_from_xlsx(str(path))
    assert result["doc_type"] == "xlsx"
    sheets = {s["name"]: s for s in result["sheets"]}
    assert set(sheets.keys()) == {"Sheet1", "Data"}

    sheet1_cells = {cell["address"]: cell for cell in sheets["Sheet1"]["cells"]}
    assert sheet1_cells["A1"]["value"] == "Question?"
    assert sheet1_cells["A1"]["bold"] is True
    assert sheet1_cells["A1"]["font_color"].upper().endswith("FF0000")
    data_cells = {cell["address"]: cell for cell in sheets["Data"]["cells"]}
    assert data_cells["B2"]["value"] == "Answer"


def test_question_without_answer_slot(monkeypatch, tmp_path, capsys):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "What is your name?"
    ws["B1"] = "n/a"  # not blank
    in_path = tmp_path / "in.xlsx"
    wb.save(in_path)

    # Provide fake API key and stub the LLM pipeline to return a schema
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    import rfp_xlsx_slot_finder as finder

    def fake_macro(profile, *, model):
        return [{"sheet": "Sheet1"}]

    def fake_zone(profile, regions, *, model):
        return [{"sheet": "Sheet1"}]

    def fake_extract(profile, zones, *, model):
        return [
            {
                "sheet": "Sheet1",
                "question_cell": "A1",
                "question_text": "What is your name?",
                "answer_cell": None,
                "question_id": "A1",
            }
        ]

    def fake_score(candidates, *, model):
        return candidates

    monkeypatch.setattr(finder, "_llm_macro_regions", fake_macro)
    monkeypatch.setattr(finder, "_llm_zone_refinement", fake_zone)
    monkeypatch.setattr(finder, "_llm_extract_candidates", fake_extract)
    monkeypatch.setattr(finder, "_llm_score_and_assign", fake_score)

    schema = extract_schema_from_xlsx(str(in_path), debug=False)
    assert schema and schema[0]["question_text"].startswith("What is")
    assert schema[0]["answer_cell"] is None

    out_path = tmp_path / "out.xlsx"
    res = write_excel_answers(schema, ["Alice"], str(in_path), str(out_path))
    assert res["applied"] == 0
    assert res["skipped"] == 1
    captured = capsys.readouterr()
    assert "no answer slot" in captured.out.lower()


def test_comment_formats_citations(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "Question?"
    in_path = tmp_path / "in.xlsx"
    wb.save(in_path)

    schema = [
        {
            "sheet": "Sheet1",
            "question_cell": "A1",
            "answer_cell": "B1",
            "question_text": "Question?",
        }
    ]
    answers = [{"text": "Ans", "citations": {1: "First", 2: "Second"}}]
    out_path = tmp_path / "out.xlsx"
    write_excel_answers(schema, answers, str(in_path), str(out_path))

    wb2 = openpyxl.load_workbook(out_path)
    c = wb2["Sheet1"]["B1"]
    assert c.comment is not None
    txt = c.comment.text
    assert "[1] First" in txt
    assert "[2] Second" in txt
