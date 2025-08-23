from datetime import datetime
from typing import Optional

from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def ensure_comments_part(document):
    """Return the document's comments part, creating it if necessary.

    The `python-docx` API has changed over versions. Newer versions expose a
    ``_comments_part`` property that creates the part on access, while older
    versions provided ``comments_part`` or an ``add_comments_part()`` factory
    method. This helper tries the available options to stay compatible across
    versions.
    """

    # Preferred modern API: private ``_comments_part`` property
    part = getattr(document.part, "_comments_part", None)
    if part is not None:
        return part

    # Older API: direct attribute
    part = getattr(document.part, "comments_part", None)
    if part is not None:
        return part

    # Fallback: explicit factory method
    add_part = getattr(document.part, "add_comments_part", None)
    if callable(add_part):
        return add_part()

    raise AttributeError("This version of python-docx does not support comments")

def add_comment_to_run(
    document,
    run,
    comment_text,
    author="RFPBot",
    bold_prefix=None,
    source_file: Optional[str] = None,
):
    part = ensure_comments_part(document)

    # Next id
    try:
        existing = part._element.xpath(".//w:comment", namespaces=part._element.nsmap)
        next_id = max([int(el.get(qn("w:id"))) for el in existing] + [0]) + 1
    except Exception:
        next_id = 0

    # <w:comment>
    c = OxmlElement("w:comment")
    c.set(qn("w:id"), str(next_id))
    c.set(qn("w:author"), author)
    c.set(qn("w:date"), datetime.utcnow().isoformat() + "Z")
    p = OxmlElement("w:p")
    if source_file:
        r2 = OxmlElement("w:r")
        r2_pr = OxmlElement("w:rPr")
        b2 = OxmlElement("w:b")
        r2_pr.append(b2)
        r2.append(r2_pr)
        t2 = OxmlElement("w:t")
        t2.text = "Source File: "
        r2.append(t2)
        p.append(r2)
        r3 = OxmlElement("w:r")
        t3 = OxmlElement("w:t")
        t3.text = str(source_file)
        r3.append(t3)
        p.append(r3)
        br = OxmlElement("w:br")
        p.append(br)
    if bold_prefix:
        r1 = OxmlElement("w:r")
        r1_pr = OxmlElement("w:rPr")
        b = OxmlElement("w:b")
        r1_pr.append(b)
        r1.append(r1_pr)
        t1 = OxmlElement("w:t")
        t1.text = str(bold_prefix)
        r1.append(t1)
        p.append(r1)
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = str(comment_text or "")
    r.append(t)
    p.append(r)
    c.append(p)
    part._element.append(c)

    # Anchor comment to the run
    start = OxmlElement("w:commentRangeStart"); start.set(qn("w:id"), str(next_id))
    end   = OxmlElement("w:commentRangeEnd");   end.set(qn("w:id"), str(next_id))
    ref_r = OxmlElement("w:r")
    ref   = OxmlElement("w:commentReference");  ref.set(qn("w:id"), str(next_id))
    ref_r.append(ref)

    parent = run._r.getparent()
    idx = parent.index(run._r)
    parent.insert(idx, start)          # before the run
    parent.insert(idx + 2, end)        # after the run
    parent.insert(idx + 3, ref_r)      # reference run
