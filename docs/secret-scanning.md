# Secret scanning in CI

`scripts/secret_scan.py` runs in the `secret-scan` CI job before merge. It is a
dependency-free lint that catches common credential shapes and high-entropy
values assigned to secret-like names. It complements — and does not replace —
GitHub secret scanning, code review, and rotating a credential the moment it is
exposed.

## Running it

```bash
python scripts/secret_scan.py .
python scripts/secret_scan.py app tests --json
python scripts/secret_scan.py . --allowlist .github/secret-scan-allowlist.json
```

Exit codes:

| Status | Meaning |
| ------ | ------- |
| `0` | No findings. |
| `1` | At least one potential credential. |
| `2` | Invalid input, unreadable path, or malformed allowlist. |

Reports redact the matched value (for example `AKIA...XY`) so the log does not
become a second leak. `--json` returns a bounded structured report; at most 1000
findings are returned.

## Patterns

The scanner recognises:

- AWS access key ids (`AKIA...`/`ASIA...`).
- GitHub classic and fine-grained tokens.
- GitLab personal access tokens.
- Slack bot/user/app tokens.
- Stripe live keys.
- OpenAI-style `sk-` keys.
- PEM private-key headers.
- A quoted 20+ character value assigned to a secret-like key
  (`api_key`, `secret`, `client_secret`, `access_token`, `password`, ...) when
  the value is not an obvious placeholder and has Shannon entropy of at least
  3.5.

Binary files and files over 2 MiB are skipped, as are dependency/build
directories. Patterns are documented in the script docstring and `PATTERNS`.

## Allowlist

Reviewed false positives go in `.github/secret-scan-allowlist.json`:

```json
{
  "version": 1,
  "allow": [
    {
      "pattern": "generic_secret_assignment",
      "value": "<exact matched value>",
      "reason": "why this is safe to keep"
    }
  ]
}
```

An entry suppresses a finding only when both the pattern name and the exact
matched value match. Keep the list short, give a reason, and never add a real
credential — rotate it instead. A malformed allowlist makes the scanner exit `2`.

## Tests

`tests/test_secret_scan.py` writes a synthetic, non-live credential to a
temporary file at test time and asserts it is caught, then repeats the scan with
an allowlist entry and asserts it is not reported. No real credential is stored
in the repository or the test source.
