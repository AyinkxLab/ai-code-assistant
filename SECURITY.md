# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities privately and promptly. Do **not** open a
public issue for a vulnerability, and never post secrets, credentials, or
working exploit code in issues, pull requests, or commit messages.

To report:

1. Open a **GitHub Security Advisory** for this repository
   (`Security` tab → `Report a vulnerability`) if it is available, or
2. Otherwise open a private issue with the `security` label describing the
   problem, the affected area, and (if known) a minimal reproduction.

Include:

- the affected module/endpoint and version/commit,
- the impact (what an attacker could do),
- any suggested fix (do not include working exploit payloads in public places).

## What happens next

- Reports are acknowledged and triaged as soon as possible.
- We will work with you to confirm impact and scope, and to land a fix before
  public disclosure where warranted.
- We ask that you give us reasonable time to fix and release before public
  disclosure.

## Security expectations in this repository

- Never commit real secrets: `.env`, API keys, OAuth client secrets, or
  database passwords (`.env.example` values are placeholders).
- Preserve the existing safety behavior when touching untrusted input
  (imported projects, uploaded files, review context): path-traversal checks,
  size/file-count caps, secret-file skipping, and treating repository content
  as untrusted data in prompts.
- Keep the Stellar/RPC surface read-only and SSRF-safe, and keep plugin
  capability enforcement fail-closed (see `docs/security.md`).

## Prompt injection and untrusted content

The assistant hands user-controlled text to an LLM. That text — uploaded files,
pasted code, repository content, issue/PR bodies — is **data**, never
instructions.

### Threat model

- **Instruction override.** Content tries to cancel the task ("ignore all
  previous instructions", "you are now…") or issue new ones.
- **Instruction exfiltration.** Content asks the model to reveal its system
  prompt, rules, or any secret in context.
- **Credential exfiltration.** Content asks for API keys, tokens, or other
  secrets, or tries to get them echoed back.
- **Output injection.** Model output contains active content (script tags,
  event handlers, `javascript:` URLs) that could execute in the browser.

### Mitigations

- **Structured boundary.** Task instructions live in a **system** message;
  untrusted content lives in a **user** message, fenced in explicit delimiters,
  with a boundary guard and a reminder re-asserted *after* the content
  (`app/services/prompt_safety.py`). Applied to `/tools/analyze`,
  `/tools/code`, and `/tools/generate`.
- **Marker neutralization + reporting.** Instruction-like markers are redacted
  before the model sees them and reported to the caller (for example
  `injection_warnings` on `/tools/analyze`); they are never executed.
- **No secrets in prompts.** User content is never merged into the system
  prompt, and provider API keys are injected in the provider layer only — they
  are never part of any message.
- **Defense-in-depth output sanitization.** The client Markdown renderer escapes
  HTML and strips script/event-handler/`javascript:` payloads
  (`app/static/js/chat_markdown.js`, issue #35).
- **Authorization is never derived from model output** or from requested OAuth
  scopes.

### Documented test cases

Common patterns are exercised in `tests/test_analyze_prompt_injection.py` and
`tests/test_code_prompt_injection.py`: "ignore previous instructions", "reveal
your system prompt", role switches ("you are now…"), and credential-request
phrasing.
