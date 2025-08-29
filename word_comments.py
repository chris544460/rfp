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
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.package import OpcPackage
    from docx.opc.part import XmlPart

    package: OpcPackage = document.part.package
    reltype = "http://schemas.microsoft.com/office/2017/10/relationships/commentsExt"
    if not any(rel.reltype == reltype for rel in document.part.rels.values()):
        # create a new xml part with the correct content‑type
        if hasattr(package, "partname_for"):
            partname = package.partname_for(reltype, "/word/commentsExt.xml")
        else:
            from docx.opc.packuri import PackURI
            partname = PackURI("/word/commentsExt.xml")
        content_type = "application/vnd.ms-word.commentsExt+xml"
        part = XmlPart(partname, content_type, b'<w15:commentsExt xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"/>', package)
        try:
            # Newer python-docx signature
            package.relate_to(document.part, reltype, part, is_external=False)
        except TypeError:
            # Older signature without the keyword argument
            package.relate_to(document.part, reltype, part)
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
    ext_part = ensure_comments_ext_part(document)

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
