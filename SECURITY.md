# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities privately and promptly. Do **not** open a
public issue for a vulnerability, and never post secrets, credentials, or
working exploit code in issues, pull requests, or commit messages.

This repository is **public**. A regular issue — with the `security` label or
without — is public the moment it is opened, so an issue is *not* a private
channel. Use one of these instead, in order of preference:

1. **GitHub private vulnerability reporting** — open this repository on GitHub
   and go to the `Security` tab (shown as `Security and quality` on some
   layouts), then choose `Report a vulnerability`. This creates a private
   advisory that only the maintainers can see, and it lets GitHub track the
   disclosure for you. If the `Security` tab shows `Report a vulnerability`,
   the feature is enabled and this is the preferred route.
2. **GitHub Security Advisory** — open `Security` → `Advisories` →
   `New draft security advisory` and describe the problem there. Drafts are
   private to the maintainers until they are published.
3. **A private message to the maintainer** on GitHub (`Ayinkx`) if neither of
   the two features above is available.

> If `Report a vulnerability` is missing, private vulnerability reporting is
> turned off for this repository. It can be enabled at `Settings` →
> `Security and quality` → `Advanced Security` → `Private vulnerability
> reporting` → `Enable`. See `docs/security.md`.

Include:

- the affected module/endpoint and version/commit,
- the impact (what an attacker could do),
- a minimal reproduction, and
- any suggested fix (do not include working exploit payloads in public places).

If you have already posted a secret, credential, or exploit in a public
issue, pull request, or commit, tell us as soon as possible so it can be
rotated or removed. Treat a leaked credential as compromised even if it was
removed later, because it may already have been scraped by an automated
mirror.

## What happens next

- Reports are acknowledged and triaged as soon as possible.
- We will work with you to confirm impact and scope, and to land a fix before
  public disclosure where warranted.
- We ask that you give us reasonable time to fix and release before public
  disclosure.

## Reporting a malicious pull request

This repository has been targeted by a supply-chain attack before: a commit
shipped an obfuscated JavaScript payload disguised as a web font together with
a `.vscode/tasks.json` that executed it whenever the folder was opened.

If you open a pull request and you see something like that — an executable
file disguised as an asset, an auto-run editor task, a workflow that reads
`secrets.*` from a pull request, a dependency installed from a URL instead of
a registry — please **stop and report it privately** using one of the routes
above before pushing further changes. Do not "fix" it in a public pull
request, because the diff of that pull request is itself public.

The guard that blocks these shapes is `scripts/check_assets.py`; see
`docs/security.md` for the full list of what it enforces.

## Supported versions

Only the latest commit on the `main` branch is supported. This project is
pre-1.0 and does not publish release artifacts or long-term support branches,
so there are no older versions to patch.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |
| < `main` | :x:                |

Security fixes are applied to `main` by pull request. If you run a fork, pin
to a specific commit and re-apply fixes from `main` when you need them.

## Security expectations in this repository

- Never commit real secrets: `.env`, API keys, OAuth client secrets, or
  database passwords (`.env.example` values are placeholders). The
  credential-shaped file patterns are listed in `.gitignore` so that they
  cannot be committed by accident.
- Preserve the existing safety behavior when touching untrusted input
  (imported projects, uploaded files, review context): path-traversal checks,
  size/file-count caps, secret-file skipping, and treating repository content
  as untrusted data in prompts.
- Keep the Stellar/RPC surface read-only and SSRF-safe, and keep plugin
  capability enforcement fail-closed (see `docs/security.md`).
- Do not weaken or bypass `scripts/check_assets.py` or its test. The guard has
  a tripwire that fails CI if the checks are removed, and removing the tripwire
  is itself a violation.
- Pin new dependencies exactly, and let Dependabot propose the upgrades
  instead of bumping versions by hand.
