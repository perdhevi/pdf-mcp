"""Split page text into overlapping chunks. Chunks never cross page boundaries,
so every hit can be cited with an exact page number."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extract import PageContent

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")


@dataclass
class Chunk:
    page: int
    index: int  # global index within the document
    text: str
    source: str


def normalize(text: str) -> str:
    text = _WS.sub(" ", text.replace("\r", ""))
    return _BLANKS.sub("\n\n", text).strip()


def _split(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            # Prefer to break on a paragraph, then sentence, then whitespace.
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(sep)
                if cut > size * 0.5:
                    end = start + cut + len(sep)
                    break
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in pieces if p]


def chunk_pages(pages: list[PageContent], size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for p in pages:
        text = normalize(p.full_text)
        if not text:
            continue
        for piece in _split(text, size, overlap):
            chunks.append(Chunk(page=p.page, index=len(chunks), text=piece, source=p.source))
    return chunks
