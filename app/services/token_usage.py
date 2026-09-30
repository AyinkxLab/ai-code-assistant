"""Token usage accounting for chat messages and conversations (issue #13).

Provider responses carry real prompt/completion token counts when the vendor
reports them; when they do not — the offline mock provider, or a streamed reply
 — a small, dependency-free estimate is used so every message still records a
usage figure.
"""

from __future__ import annotations

#: Rough characters-per-token ratio for source code and English prose.
CHARS_PER_TOKEN = 4


def estimate_tokens(text) -> int:
    """Estimate the token count of ``text`` (>= 1 for non-empty input)."""
    if text is None:
        return 0
    value = str(text)
    if not value:
        return 0
    return max(1, (len(value) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def messages_text(messages) -> str:
    """Join the content of a provider message list into one string."""
    parts = []
    for message in messages or []:
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        if content:
            parts.append(str(content))
    return "\n".join(parts)


def _totals(prompt: int, completion: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def usage_from_response(response, messages=None) -> dict:
    """Usage for a completed provider response, estimating any missing counts."""
    prompt = getattr(response, "prompt_tokens", None)
    completion = getattr(response, "completion_tokens", None)
    if prompt is None:
        prompt = estimate_tokens(messages_text(messages))
    if completion is None:
        completion = estimate_tokens(getattr(response, "content", ""))
    total = getattr(response, "total_tokens", None)
    if total is None:
        total = (prompt or 0) + (completion or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def usage_from_text(prompt_text, completion_text) -> dict:
    """Usage estimated from raw prompt/completion text (e.g. a streamed reply)."""
    return _totals(estimate_tokens(prompt_text), estimate_tokens(completion_text))


def sum_usage(messages) -> dict:
    """Cumulative usage across ``messages`` (missing counts ignored)."""
    prompt = completion = total = 0
    for message in messages or []:
        if getattr(message, "total_tokens", None) is None:
            continue
        prompt += message.prompt_tokens or 0
        completion += message.completion_tokens or 0
        total += message.total_tokens or 0
    return _totals(prompt, completion)
