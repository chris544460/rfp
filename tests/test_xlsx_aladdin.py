import openpyxl


def test_spacy_question_detection(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "What is your name?"
    # leave B1 blank to simulate answer slot
    path = tmp_path / "in.xlsx"
    wb.save(path)

    import rfp_xlsx_slot_finder as finder

    schema = finder.extract_schema_from_xlsx(str(path), debug=False)
    # The LLM-based pipeline may decline to choose a slot when no model is
    # available.  We simply ensure the question cell was detected.
    assert schema and schema[0]["question_cell"] == "A1"
