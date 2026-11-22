# Linear issues from review findings

`app/services/linear_issues.py` maps a review finding to a Linear issue.

- **Severity → priority:** critical=1, high=2, medium=3, low/info=4.
- **Backlink:** pass the finding URL as `backlink`; it is included in the issue
  description with the file/line when available.
- **Idempotency:** the key is `sha256("linear:<project_id>:<finding_id>")[:40]`.
  The service looks the key up first, so retries return the existing issue with
  `created=False` instead of duplicating it.
- **Errors:** invalid input and transport failures raise `LinearError` with a
  stable `code` (`invalid_severity`, `invalid_finding`, `invalid_response`,
  `unavailable`).

The transport is a small protocol (`find_issue`, `create_issue`) so callers can
plug in their HTTP client and tests can use a fake without network access.