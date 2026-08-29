"""Config-driven text chunking (chunking.max_chars / chunking.overlap)."""

from __future__ import annotations


def chunk_text(text: str, max_chars: int, overlap: int) -> list:
    """Split text into windows of at most `max_chars` characters.

    Consecutive windows share exactly `overlap` characters
    (`chunks[i+1][:overlap] == chunks[i][-overlap:]`). When the window end
    falls inside a word, the split moves back to the preceding whitespace
    (never earlier than half the window) so chunks start on word
    boundaries when possible.
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars must be positive, got {max_chars}")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError(
            f"overlap ({overlap}) must be in [0, max_chars) for "
            f"max_chars={max_chars}"
        )
    text = text or ""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            window = text[start:end]
            cut = window.rfind(" ", max_chars // 2)
            if cut != -1:
                end = start + cut
        chunks.append(text[start:end])
        if end >= n:
            break
        start = end - overlap
    return chunks
