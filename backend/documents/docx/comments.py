"""Compatibility helpers for injecting Word comments via python-docx."""

from datetime import datetime
import uuid
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


# --- Modern (threaded) comments extension part helper ---
def ensure_comments_ext_part(document):
    """
    Return the document's *modern* comments‑extension part, creating it if
    necessary.  Modern (threaded) comments reside in a separate part
    `/word/commentsExt.xml` that contains the `<w15:commentsExt>` root with
    one `<w15:commentEx>` per legacy comment.  The python‑docx API does not
    (yet) expose this part officially, but some versions have a private
    ``_comments_ext_part`` or ``comments_ext_part`` attribute, or a
    ``add_comments_ext_part()`` factory.  This helper mirrors
    ``ensure_comments_part`` for maximum compatibility.
    """
    # Newer private attr
    part = getattr(document.part, "_comments_ext_part", None)
    if part is not None:
        return part

    # Older attr
    part = getattr(document.part, "comments_ext_part", None)
    if part is not None:
        return part

    # Factory, if provided by patched builds
    add_part = getattr(document.part, "add_comments_ext_part", None)
    if callable(add_part):
        return add_part()

    # Fallback: create an empty commentsExt part manually
    from docx.opc.package import OpcPackage
    from docx.opc.part import XmlPart

    package: OpcPackage = document.part.package
    reltype = "http://schemas.microsoft.com/office/2017/10/relationships/commentsExt"
    if not any(rel.reltype == reltype for rel in document.part.rels.values()):
        # create a new xml part with the correct content‑type
        partname = package.partname_for(reltype, "/word/commentsExt.xml")
        content_type = "application/vnd.ms-word.commentsExt+xml"
        part = XmlPart(partname, content_type, b'<w15:commentsExt xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"/>')
        package.relate_to(document.part, reltype, part, is_external=False)
    else:
        part = next(rel._target for rel in document.part.rels.values() if rel.reltype == reltype)

    return part

def add_comment_to_run(
    document,
    run,
    comment_text,
    author="RFPBot",
    bold_prefix=None,
    source_file: Optional[str] = None,
):
    """Create a legacy + modern Word comment anchored to ``run`` with optional metadata."""
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
        # preserve trailing space in Source File text
        t2.set(qn("xml:space"), "preserve")
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

    # ------------------------------------------------------------------
    # Modern (threaded) comment extension record
    # ------------------------------------------------------------------
    try:
        ext_part = ensure_comments_ext_part(document)
        ce = OxmlElement("w15:commentEx")
        ce.set(qn("w15:id"), str(next_id))
        # Each thread needs a stable 8‑char paraId – use random hex
        ce.set(qn("w15:paraId"), uuid.uuid4().hex[:8])
        ce.set(qn("w15:done"), "0")        # not resolved
        ce.set(qn("w15:authorId"), "0")    # default author map
        # Append to the <w15:commentsExt> root (create if missing)
        root = ext_part._element
        if root.tag != qn("w15:commentsExt"):  # empty part?
            root.clear()
            root.tag = qn("w15:commentsExt")
        root.append(ce)
    except AttributeError:
        # Silently ignore if the running python‑docx build lacks support
        pass

    # Anchor comment to the run
    start = OxmlElement("w:commentRangeStart")
    start.set(qn("w:id"), str(next_id))
    end = OxmlElement("w:commentRangeEnd")
    end.set(qn("w:id"), str(next_id))
    ref_r = OxmlElement("w:r")
    ref = OxmlElement("w:commentReference")
    ref.set(qn("w:id"), str(next_id))
    ref_r.append(ref)

    parent = run._r.getparent()
    idx = parent.index(run._r)
    parent.insert(idx, start)          # before the run
    parent.insert(idx + 2, end)        # after the run
    parent.insert(idx + 3, ref_r)      # reference run
