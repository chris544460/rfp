import importlib
import openpyxl
import pytest

def test_extract_schema_with_aladdin(monkeypatch, tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "What is your name?"
    path = tmp_path / "in.xlsx"
    wb.save(path)

    monkeypatch.setenv("ANSWER_FRAMEWORK", "aladdin")
    monkeypatch.setenv("aladdin_studio_api_key", "x")
    monkeypatch.setenv("defaultWebServer", "https://example.com")
    monkeypatch.setenv("aladdin_user", "u")
    monkeypatch.setenv("aladdin_passwd", "p")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    import rfp_xlsx_slot_finder as finder
    importlib.reload(finder)

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
                "answer_cell": "B1",
                "question_id": "A1",
            }
        ]

    def fake_score(candidates, *, model):
        return candidates

    monkeypatch.setattr(finder, "_llm_macro_regions", fake_macro)
    monkeypatch.setattr(finder, "_llm_zone_refinement", fake_zone)
    monkeypatch.setattr(finder, "_llm_extract_candidates", fake_extract)
    monkeypatch.setattr(finder, "_llm_score_and_assign", fake_score)

    schema = finder.extract_schema_from_xlsx(str(path), debug=False)
    assert schema and schema[0]["answer_cell"] == "B1"

    monkeypatch.setenv("ANSWER_FRAMEWORK", "openai")
    importlib.reload(finder)
