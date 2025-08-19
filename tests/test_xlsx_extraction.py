import openpyxl
from rfp_xlsx_slot_finder import extract_slots_from_xlsx


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
