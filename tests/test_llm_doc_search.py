import sys, pathlib
from docx import Document

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from llm_doc_search import search_uploaded_docs


class DummyLLM:
    def get_completion(self, prompt: str):
        if "Project Apollo is top secret." in prompt:
            return "YES: Project Apollo is top secret."
        return "NO"


def test_search_uploaded_docs_docx(tmp_path):
    doc = Document()
    doc.add_paragraph("Project Apollo is top secret.")
    path = tmp_path / "sample.docx"
    doc.save(path)

    llm = DummyLLM()
    hits = search_uploaded_docs("What is Project Apollo?", [str(path)], llm)
    assert hits
    assert "Project Apollo is top secret." in hits[0]["text"]
