"""AI-generated Soroban contract skeletons (#187).

Turns a natural-language contract description into a minimal, importable
Soroban project (``Cargo.toml`` + ``src/lib.rs``) using the configured LLM
provider, then **normalizes** the result so it is always a structurally valid
Soroban skeleton:

* ``Cargo.toml`` must declare the ``soroban-sdk`` dependency.
* ``src/lib.rs`` must contain ``#[contract]`` / ``#[contractimpl]`` and import
  ``soroban_sdk``, with balanced braces.

When the model output is missing any of that (or the provider fails), the
deterministic scaffold from :mod:`app.services.soroban_scaffold` is substituted
— so the tool never returns broken structure, and the result always round-trips
through Stellar detection as ``likely`` Soroban.

Honesty rules:

* The output is explicitly labeled **AI-generated** and **not compiled or
  verified**. Nothing here executes ``cargo`` or claims the code builds.
* No secrets are requested or emitted. The provider is called in-process; no
  other network operation is performed.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.importing import detect_language
from app.services.llm import LLMProviderError, get_provider
from app.services.soroban_scaffold import (
    DEFAULT_CRATE_NAME,
    ScaffoldError,
    normalize_crate_name,
    soroban_scaffold_rows,
)
from app.services.stellar_detection import detect_stellar_from_dicts

#: Bound on the human description sent to the provider.
MAX_DESCRIPTION_CHARS = 4000
#: Bound on the model output kept in memory / returned.
MAX_OUTPUT_CHARS = 60000

LABEL = (
    "AI-generated Soroban contract skeleton. It has not been compiled or "
    "verified — review it before building or deploying."
)

SYSTEM_PROMPT = (
    "You are a Soroban smart-contract engineer. Given a natural-language "
    "description, produce a MINIMAL Rust Soroban contract skeleton. Output "
    "exactly two fenced code blocks and nothing else: first a ```toml block "
    "for Cargo.toml that depends on soroban-sdk and sets "
    'crate-type = ["cdylib", "rlib"], then a ```rust block for src/lib.rs '
    "that uses the soroban_sdk crate with a #[contract] struct and a "
    "#[contractimpl] impl block. Use #![no_std]. Do not claim the code "
    "compiles. Do not include secrets."
)

_CODE_BLOCK_RE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_CARGO_NAME_RE = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.MULTILINE)


def _extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``(language, body)`` pairs for every fenced code block."""
    blocks = _CODE_BLOCK_RE.findall(text or "")
    return [(lang.strip().lower(), body.strip()) for lang, body in blocks]


def _extract_cargo(blocks: list[tuple[str, str]]) -> str | None:
    for lang, body in blocks:
        if lang == "toml" and ("[dependencies]" in body or "soroban-sdk" in body):
            return body
    for _lang, body in blocks:
        if "[package]" in body or "soroban-sdk" in body:
            return body
    return None


def _extract_lib(blocks: list[tuple[str, str]]) -> str | None:
    for lang, body in blocks:
        if lang in ("rust", "rs") and "#[contract" in body:
            return body
    for _lang, body in blocks:
        if "#[contract" in body or "soroban_sdk" in body:
            return body
    return None


def _crate_name_from_cargo(cargo: str | None) -> str | None:
    if not cargo:
        return None
    match = _CARGO_NAME_RE.search(cargo)
    return match.group(1) if match else None


def _is_balanced(text: str) -> bool:
    return text.count("{") == text.count("}") and text.count("(") == text.count(")")


def _valid_cargo(cargo: str | None) -> bool:
    """A usable manifest must declare the ``soroban-sdk`` dependency."""
    return bool(cargo) and "soroban-sdk" in cargo


def _valid_lib(lib: str | None) -> bool:
    """A usable contract source needs the Soroban structure and a SDK import."""
    if not lib:
        return False
    if "#[contract]" not in lib or "#[contractimpl]" not in lib:
        return False
    if "soroban_sdk" not in lib:
        return False
    return _is_balanced(lib)


def _file_row(path: str, text: str) -> dict[str, Any]:
    """Build an import-ready, plain-text file row for generated code."""
    raw = text.encode("utf-8")
    return {
        "path": path,
        "size": len(raw),
        "is_binary": False,
        "language": detect_language(path),
        "content": text,
    }


def generate_soroban_skeleton(
    description: str,
    *,
    name: str | None = None,
    provider: Any | None = None,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    """Generate a normalized Soroban contract skeleton from ``description``.

    ``provider`` overrides the configured LLM provider (used by tests). The
    return value always contains valid ``Cargo.toml`` + ``src/lib.rs`` rows, a
    ``detection`` block proving the round trip is ``likely`` Soroban, and the
    ``ai_generated`` / ``verified`` / ``label`` markers.
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("A contract description is required.")
    description = description[:MAX_DESCRIPTION_CHARS]

    provider_error: str | None = None
    raw = ""
    try:
        llm = provider or get_provider()
        raw = llm.complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Contract description:\n{description}"},
            ]
        )
    except LLMProviderError as exc:
        provider_error = str(exc)
    raw = (raw or "")[:max_chars]

    blocks = _extract_code_blocks(raw)
    cargo = _extract_cargo(blocks)
    lib = _extract_lib(blocks)

    try:
        crate_name = normalize_crate_name(name or _crate_name_from_cargo(cargo))
    except ScaffoldError:
        crate_name = DEFAULT_CRATE_NAME

    fallback = {row["path"]: row["content"] for row in soroban_scaffold_rows(crate_name)}
    used_fallback = False
    if not _valid_cargo(cargo):
        cargo = fallback["Cargo.toml"]
        used_fallback = True
    if not _valid_lib(lib):
        lib = fallback["src/lib.rs"]
        used_fallback = True

    files = [_file_row("Cargo.toml", cargo), _file_row("src/lib.rs", lib)]
    signals = detect_stellar_from_dicts(files)
    detection = signals.to_dict()

    return {
        "description": description,
        "name": crate_name,
        "files": files,
        "cargo_toml": cargo,
        "lib_rs": lib,
        "ai_generated": True,
        "verified": False,
        "label": LABEL,
        "used_fallback": used_fallback,
        "provider_error": provider_error,
        "detection": {
            "confidence": detection["confidence"],
            "is_soroban": detection["is_soroban"],
            "is_stellar": detection["is_stellar"],
            "evidence": detection["evidence"],
        },
    }
