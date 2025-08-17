# rfp_utils/word_comments.py
from datetime import datetime
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

def ensure_comments_part(document):
    try:
        return document.part.comments_part
    except AttributeError:
        return document.part.add_comments_part()

def add_comment_to_run(document, run, comment_text, author="RFPBot"):
    part = ensure_comments_part(document)
    # Compute next comment id
    try:
        existing = part._element.xpath(".//w:comment", namespaces=part._element.nsmap)
        next_id = max([int(el.get(qn("w:id"))) for el in existing] + [0]) + 1
    except Exception:
        next_id = 0

    # Create <w:comment>
    c = OxmlElement("w:comment")
    c.set(qn("w:id"), str(next_id))
    c.set(qn("w:author"), author)
    c.set(qn("w:date"), datetime.utcnow().isoformat() + "Z")

    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = str(comment_text or "")
    r.append(t)
    p.append(r)
    c.append(p)
    part._element.append(c)

    # Anchor the comment to the run containing "[n]"
    start = OxmlElement("w:commentRangeStart"); start.set(qn("w:id"), str(next_id))
    end   = OxmlElement("w:commentRangeEnd");   end.set(qn("w:id"), str(next_id))
    ref_r = OxmlElement("w:r")
    ref   = OxmlElement("w:commentReference");  ref.set(qn("w:id"), str(next_id))
    ref_r.append(ref)

    parent = run._r.getparent()
    idx = parent.index(run._r)
    parent.insert(idx, start)          # before the run
    parent.insert(idx + 2, end)        # after the run
    parent.insert(idx + 3, ref_r)      # comment reference
