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
