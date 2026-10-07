"""Tests for the streaming helpers used by the chat SSE endpoint."""

import pytest

from app.services.streaming import DEFAULT_CHUNK_SIZE, chunk_text


class TestChunkText:
    def test_empty_text_yields_nothing(self):
        assert list(chunk_text("")) == []

    def test_joining_chunks_reproduces_the_reply(self):
        reply = "Hello there, this is a longer assistant reply with several words."
        assert "".join(chunk_text(reply)) == reply

    def test_chunks_do_not_exceed_requested_size(self):
        reply = "The quick brown fox jumps over the lazy dog."
        chunks = list(chunk_text(reply, size=12))
        assert chunks
        assert max(len(chunk) for chunk in chunks) <= 12

    def test_default_chunk_size_is_bounded(self):
        reply = "x" * (DEFAULT_CHUNK_SIZE * 3 + 5)
        chunks = list(chunk_text(reply))
        assert "".join(chunks) == reply
        assert max(len(chunk) for chunk in chunks) <= DEFAULT_CHUNK_SIZE

    def test_splits_long_unbroken_token(self):
        assert list(chunk_text("abcdefghij", size=4)) == ["abcd", "efgh", "ij"]

    def test_prefers_word_boundaries(self):
        assert list(chunk_text("one two three", size=8)) == ["one two ", "three"]

    def test_rejects_non_positive_size(self):
        with pytest.raises(ValueError):
            list(chunk_text("hello", size=0))
