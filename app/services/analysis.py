"""AI-powered analysis of GitHub repositories, issues, and pull requests.

These helpers build focused prompts from a bounded slice of repository context
and delegate to the configured LLM provider. The prompts ask the model to
clearly label uncertainty so the UI can distinguish confirmed defects from
suggestions.

When Stellar/Soroban signals are detected in the repository or PR diff, a
Stellar-aware section is appended to the prompt so the model can surface
contract-specific concerns (authorization, storage keys, ``extend_ttl``,
``panic!``/``unwrap`` in contract paths, cross-contract calls). When no
signals are found, the generic prompt is unchanged.
"""

from __future__ import annotations

from app.services.llm import LLMProviderError, get_provider
from app.services.stellar_detection import StellarSignals, detect_stellar_project

# Hard cap on the amount of repository text fed to the model for any single
# analysis, so a request never uploads the whole repository.
MAX_CONTEXT_CHARS = 40_000

_SYSTEM = (
    "You are an expert software engineering analyst. Be concrete, cite the "
    "specific code you refer to, and be honest about uncertainty. Clearly "
    "label every finding: prefix confirmed defects or facts with "
    "'[CONFIRMED]' and anything that is a hypothesis, trade-off, or suggestion "
    "with '[SUGGESTION]'."
)

# -- Stellar-aware prompt sections -------------------------------------------

_STELLAR_PR_SECTION = """
Stellar/Soroban review (detected):
This repository shows Stellar/Soroban signals. In addition to the generic
review, pay attention to Soroban-specific concerns in the changed files:
- Contract structure: #[contractimpl]/#[contract] usage, contract types.
- Authorization: require_auth / address checks before privileged operations.
- Storage: storage key handling, extend_ttl / persist patterns.
- Error handling: panic!/unwrap in contract code paths (should be avoided).
- Cross-contract calls: proper interface usage and error propagation.
- Stellar dependencies: Soroban crates / SDKs touched by the change.
- Stellar config files (e.g. stellar.toml) if changed.
Mark every Stellar finding [CONFIRMED] or [SUGGESTION] as usual.
"""

_STELLAR_ISSUE_SECTION = """
Stellar/Soroban context (detected):
This repository shows Stellar/Soroban signals. Consider Stellar-specific
aspects in your analysis:
- Soroban SDK / XDR concerns relevant to the issue.
- Contract structure and storage patterns.
- Network behaviour (testnet/mainnet/futurenet) if applicable.
- Authorization, error handling, and cross-contract call patterns.
Keep the [CONFIRMED]/[SUGGESTION] labelling and do not fabricate Stellar
claims beyond what the evidence supports.
"""


class _FileAdapter:
    """Adapt a plain dict (``path``, ``content``) into a detection-compatible object.

    :func:`detect_stellar_project` expects objects with ``.path`` and
    ``.content`` attributes. This adapter wraps a dict so the same detection
    logic can be reused for GitHub PR changed-file dicts and raw repo file
    listings without re-implementing detection.
    """

    __slots__ = ("path", "content")

    def __init__(self, path: str, content: str | None) -> None:
        self.path = path
        self.content = content

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_FileAdapter(path={self.path!r})"


def _adapt_pr_files(files: list[dict]) -> list[_FileAdapter]:
    """Convert GitHub PR file dicts into detection-compatible objects.

    Each PR file dict has ``filename`` and ``patch`` keys (the latter may be
    ``None`` for binary files). The patch text is a diff, not full file
    content, but it still contains Soroban attributes, imports, and crate
    names in added/removed lines — sufficient for heuristic detection.
    """
    adapters: list[_FileAdapter] = []
    for file in files:
        filename = file.get("filename") or file.get("path") or ""
        patch = file.get("patch") or file.get("content")
        if filename:
            adapters.append(_FileAdapter(filename, patch))
    return adapters


def _adapt_repo_files(files: list[dict] | list[tuple[str, str | None]]) -> list[_FileAdapter]:
    """Convert repo file dicts/tuples into detection-compatible objects.

    Accepts either ``[{"path": ..., "content": ...}, ...]`` or
    ``[("path", "content"), ...]`` for flexibility.
    """
    adapters: list[_FileAdapter] = []
    for item in files:
        if isinstance(item, dict):
            path = item.get("path") or item.get("filename") or ""
            content = item.get("content") or item.get("patch")
        elif isinstance(item, (list, tuple)) and len(item) >= 1:
            path = item[0]
            content = item[1] if len(item) > 1 else None
        else:
            continue
        if path:
            adapters.append(_FileAdapter(path, content))
    return adapters


def _safe_detect(adapters: list[_FileAdapter]) -> StellarSignals | None:
    """Run detection, returning ``None`` on any unexpected failure."""
    try:
        return detect_stellar_project(adapters)
    except Exception:  # noqa: BLE001 - detection must never crash analysis
        return None


def _run(prompt: str, *, system: str = _SYSTEM) -> str:
    """Run a single completion with the configured provider."""
    try:
        provider = get_provider()
        return provider.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
        )
    except LLMProviderError as exc:
        return f"[analysis unavailable: {exc}]"


def _clip(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[context truncated]"


def analyze_issue(
    issue: dict,
    owner: str,
    repo: str,
    *,
    repo_files: list[dict] | list[tuple[str, str | None]] | None = None,
) -> dict:
    """Produce a structured AI analysis of a GitHub issue.

    When *repo_files* is provided, Stellar/Soroban detection is run on the
    file list. If detection is positive, a Stellar-aware section is appended
    to the prompt covering Soroban SDK/XDR concerns, contract structure, and
    network behaviour. When detection is negative, uncertain, or no files are
    provided, the generic issue analysis prompt is used unchanged.
    """
    body = issue.get("body") or "(no description provided)"
    labels = ", ".join(issue.get("labels") or []) or "none"

    stellar_section = ""
    if repo_files:
        signals = _safe_detect(_adapt_repo_files(repo_files))
        if signals is not None and signals.is_stellar:
            stellar_section = _STELLAR_ISSUE_SECTION

    prompt = f"""Repository: {owner}/{repo}
Issue #{issue.get('number')}: {issue.get('title')}
State: {issue.get('state')}
Labels: {labels}

Description:
{_clip(body)}
{stellar_section}
Provide a structured analysis with these sections:
1. Summary - one short paragraph
2. Problem identification - what is actually being asked/fixed
3. Suggested implementation approach - concrete steps
4. Suggested acceptance criteria - bullet list, testable
5. Complexity/difficulty estimation - easy/medium/hard with one-line reasoning
"""
    return {
        "kind": "issue",
        "issue_number": issue.get("number"),
        "title": issue.get("title"),
        "analysis": _run(prompt),
    }


def analyze_pull_request(pr: dict, files: list[dict]) -> dict:
    """Produce a structured AI analysis of a pull request.

    Stellar/Soroban detection is run on the PR changed-file dicts (adapted
    via :func:`_adapt_pr_files`). When detection is positive, a Stellar-aware
    section is appended to the review prompt covering authorization patterns,
    ``panic!``/``unwrap`` in contract paths, storage keys, ``extend_ttl``, and
    cross-contract calls. When not detected, the generic review is unchanged.
    Detection failure is handled safely (falls back to generic review).
    """
    body = pr.get("body") or "(no description provided)"
    changed = []
    for file in files[:40]:
        patch = file.get("patch") or ""
        changed.append(
            f"- {file.get('filename')} ({file.get('status')}, "
            f"+{file.get('additions')}/-{file.get('deletions')})\n"
            f"{_clip(patch, 6000)}"
        )
    files_text = "\n".join(changed) if changed else "(no file-level diff available)"

    # Run Stellar detection on the adapted PR files.
    stellar_section = ""
    signals = _safe_detect(_adapt_pr_files(files))
    if signals is not None and signals.is_stellar:
        stellar_section = _STELLAR_PR_SECTION

    prompt = f"""Pull request #{pr.get('number')}: {pr.get('title')}
State: {pr.get('state')} (merged: {pr.get('merged')})
Author: {pr.get('author')}
Base: {pr.get('base')} -> Head: {pr.get('head')}

Description:
{_clip(body)}

Changed files:
{_clip(files_text, MAX_CONTEXT_CHARS // 2)}
{stellar_section}
Provide a structured review with these sections:
1. Summary - what this PR does, one short paragraph
2. Code-change explanation - what each notable change does
3. Potential bugs - clearly mark [CONFIRMED] vs [SUGGESTION]
4. Suggested tests - concrete test cases for the new behaviour
5. Review checklist - bullet list of things to verify before merge
"""
    return {
        "kind": "pull_request",
        "pr_number": pr.get("number"),
        "title": pr.get("title"),
        "analysis": _run(prompt),
        "stellar_detected": signals is not None and signals.is_stellar,
    }


def analyze_file(filename: str, language: str, code: str, question: str | None = None) -> dict:
    """Analyze a single file's contents (explain, find problems, etc.)."""
    if question:
        prompt = (
            f"File: {filename} (language: {language})\n\n"
            f"Code:\n{_clip(code)}\n\n"
            f"Question: {question}\n"
            "Answer the question directly, referencing specific lines where possible."
        )
    else:
        prompt = (
            f"File: {filename} (language: {language})\n\n"
            f"Code:\n{_clip(code)}\n\n"
            "Review this file: briefly explain its purpose, then identify potential "
            "bugs or problems. Mark [CONFIRMED] for definite defects and [SUGGESTION] "
            "for possible issues or improvements."
        )
    return {"kind": "file", "filename": filename, "analysis": _run(prompt)}


def summarize_repository(owner: str, repo: str, readme: str | None, file_list: list[str]) -> dict:
    """Produce a short overview of a repository from README + file list."""
    prompt = (
        f"Repository: {owner}/{repo}\n\n"
        f"README:\n{_clip(readme or '(no README available)', 20000)}\n\n"
        f"Top-level structure (sample):\n{_clip('\n'.join(file_list[:300]), 15000)}\n\n"
        "Explain in 2-4 sentences what this repository does, its main components, "
        "and the primary technologies used."
    )
    return {"kind": "repository", "full_name": f"{owner}/{repo}", "analysis": _run(prompt)}
