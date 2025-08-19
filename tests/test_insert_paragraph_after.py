import pytest
import docx
from rfp_docx_apply_answers import insert_paragraph_after


def test_insert_paragraph_after_requires_paragraph():
    doc = docx.Document()
    p = doc.add_paragraph("first")
    new_p = insert_paragraph_after(p, "second")
    assert new_p.text == "second"
    with pytest.raises(TypeError):
        insert_paragraph_after("not a paragraph", "text")
