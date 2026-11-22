# OpenAPI document

The public JSON API is described by an OpenAPI 3.1 document generated from the
Flask routing table. Generating from the live routes keeps the document in sync
with the handlers instead of maintaining a second, hand-written route list.

## Generate

```bash
python scripts/generate_openapi.py --output openapi.json
```

The document is deterministic: routes are sorted, JSON keys are sorted, and the
file ends with a newline, so regeneration produces byte-identical output for
the same code.

## Check for drift

```bash
# After generating, verify the file matches the current routes:
python scripts/generate_openapi.py --output openapi.json --check
```

`--check` fails with exit code `1` when the generated document is invalid or
when the output file differs from the current routes. CI runs the generator,
then the check, then uploads the document as the `openapi` artifact.

## What is included

- Only paths containing `/api/` are documented; HTML pages are out of scope.
- Path parameters such as `<int:workspace_id>` become `{workspace_id}` with an
  integer schema.
- Every operation lists the stable error responses from the
  [API error catalog](api-errors.md), including the quota `429` described in
  [API rate limits](api-rate-limits.md).

## Adding an endpoint

Add the Flask route as usual. Regenerate the document in the same pull request;
CI will fail if the generated file is stale.
