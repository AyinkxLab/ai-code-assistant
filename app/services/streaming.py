"""Helpers for streaming assistant replies to the client.

The chat SSE endpoint sends an assistant reply as a sequence of
``message_delta`` frames so the UI can render a typing cursor while the text
arrives. Chunking is presentational only: concatenating the pieces always
reproduces the original reply exactly.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

#: Target size (in characters) of a single streamed chunk.
DEFAULT_CHUNK_SIZE = 24

#: Non-space run plus any trailing spaces, or a run of whitespace.
_TOKEN_RE = re.compile(r"\S+\s*|\s+")


def chunk_text(text: str, size: int = DEFAULT_CHUNK_SIZE) -> Iterator[str]:
    """Split ``text`` into chunks of roughly ``size`` characters.

    Word boundaries are preferred so the client's typing cursor lands between
    words rather than mid-word. Tokens longer than ``size`` (a URL, a long
    identifier, or a run of whitespace) are hard-split so no single frame
    grows unbounded.
    """
    if size < 1:
        raise ValueError("size must be a positive integer")
    if not text:
        return
    buffer = ""
    for token in _TOKEN_RE.findall(text):
        while len(token) > size:
            if buffer:
                yield buffer
                buffer = ""
            yield token[:size]
            token = token[size:]
        if buffer and len(buffer) + len(token) > size:
            yield buffer
            buffer = token
        else:
            buffer += token
    if buffer:
        yield buffer


__all__ = ["DEFAULT_CHUNK_SIZE", "chunk_text"]
