"""Deterministic, provenance-preserving source chunking."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from services.document_parser import ParsedDocument

MAX_CHUNK_CODEPOINTS = 4000


@dataclass(frozen=True)
class ParsedChunk:
    chunk_index: int
    content: str
    content_hash: str
    page_number: int | None


def chunk_document(document: ParsedDocument, *, maximum_codepoints: int = MAX_CHUNK_CODEPOINTS) -> tuple[ParsedChunk, ...]:
    """Create non-overlapping exact substrings, preferring whitespace boundaries."""

    if not isinstance(maximum_codepoints, int) or isinstance(maximum_codepoints, bool) or maximum_codepoints <= 0:
        raise ValueError("maximum_codepoints must be a positive integer")
    chunks: list[ParsedChunk] = []
    for segment in document.segments:
        for content in _split(segment.text, maximum_codepoints):
            chunks.append(
                ParsedChunk(
                    len(chunks),
                    content,
                    f"sha256:{sha256(content.encode('utf-8')).hexdigest()}",
                    segment.page_number,
                ),
            )
    return tuple(chunks)


def _split(text: str, maximum: int) -> tuple[str, ...]:
    if not text:
        return ()
    result: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + maximum)
        if hard_end == len(text):
            end = hard_end
        else:
            # Include the final whitespace in the preceding exact substring.
            boundary = max(
                (index for index in range(start, hard_end) if text[index].isspace()),
                default=-1,
            )
            end = boundary + 1 if boundary >= start else hard_end
        if end <= start:  # Defensive fallback for an impossible zero-width split.
            end = hard_end
        result.append(text[start:end])
        start = end
    return tuple(result)
