from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Iterable

from docx import Document
from PyPDF2 import PdfReader

from answer_composer import CompletionsClient
from prompts import read_prompt


SEARCH_PROMPT = read_prompt(
    "llm_doc_search",
    (
        "You will be given a user question and a chunk of text from an uploaded document. "
        "If the chunk contains information that helps answer the question, "
        "respond with 'YES:' followed by only the relevant excerpt. "
        "Otherwise respond with 'NO'."
    ),
)


def _extract_text_from_doc(path: str) -> str:
    """Extract plain text from a .docx or .pdf file."""
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs)
    if ext == ".pdf":
        reader = PdfReader(path)
        parts = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            parts.append(txt)
        return "\n".join(parts)
    raise ValueError(f"Unsupported file type: {path}")


def _iter_chunks(text: str, chunk_size: int = 500, overlap: int = 50) -> Iterable[str]:
    words = text.split()
    step = max(1, chunk_size - overlap)
    for i in range(0, len(words), step):
        yield " ".join(words[i:i + chunk_size])


def search_uploaded_docs(
    question: str,
    doc_paths: List[str],
    llm: CompletionsClient,
    chunk_size: int = 500,
    overlap: int = 50,
    context_pad: int = 50,
) -> List[Dict]:
    """Return LLM-retrieved snippets from uploaded documents.

    Each hit mirrors the structure returned by the vector search module:
    {"text": snippet, "meta": {"source": path}, "cosine": 1.0}
    """
    hits: List[Dict] = []
    for path in doc_paths:
        try:
            text = _extract_text_from_doc(path)
        except Exception:
            continue
        for chunk in _iter_chunks(text, chunk_size=chunk_size, overlap=overlap):
            prompt = (
                f"{SEARCH_PROMPT}\n\nQuestion: {question}\n\nChunk:\n{chunk}\n"
            )
            raw = llm.get_completion(prompt)
            content = raw[0] if isinstance(raw, tuple) else raw
            if not isinstance(content, str):
                continue
            reply = content.strip()
            if reply.upper().startswith("YES:"):
                snippet = reply[4:].strip()
                lower_chunk = chunk.lower()
                idx = lower_chunk.find(snippet.lower())
                if idx >= 0:
                    start = max(0, idx - context_pad)
                    end = min(len(chunk), idx + len(snippet) + context_pad)
                    snippet = chunk[start:end]
                hits.append({
                    "text": snippet,
                    "meta": {"source": str(path)},
                    "cosine": 1.0,
                })
    return hits
