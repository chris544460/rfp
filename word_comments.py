from datetime import datetime
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

def add_comment_to_run(document, run, comment_text, author="RFPBot"):
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

    def _append_run(text: str, bold: bool = False) -> None:
        r = OxmlElement("w:r")
        if bold:
            rPr = OxmlElement("w:rPr")
            b = OxmlElement("w:b")
            rPr.append(b)
            r.append(rPr)
        t = OxmlElement("w:t")
        t.text = text
        r.append(t)
        p.append(r)

    def _append_break() -> None:
        br_r = OxmlElement("w:r")
        br = OxmlElement("w:br")
        br_r.append(br)
        p.append(br_r)

    if isinstance(comment_text, (list, tuple)):
        for chunk in comment_text:
            if isinstance(chunk, tuple):
                text, bold = chunk
            else:
                text, bold = chunk, False
            text = str(text or "")
            parts = text.split("\n")
            for i, part in enumerate(parts):
                if part:
                    _append_run(part, bold)
                if i < len(parts) - 1:
                    _append_break()
    else:
        _append_run(str(comment_text or ""))

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
