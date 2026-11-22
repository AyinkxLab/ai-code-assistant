# Dependency policy and SBOM

The repository generates a bounded software bill of materials for its declared
Python dependencies and fails CI when one carries a license outside the
allowlist. The implementation is `scripts/dependency_policy.py` and uses only
the Python standard library.

## What is checked

`requirements.txt` and `requirements-dev.txt` are parsed, including `-r`
includes. The resulting set is the **declared direct dependency set**; transitive
packages are not enumerated. Versions come from the exact `==` pins already
enforced by `scripts/check_assets.py`.

For each package the license is resolved in this order:

1. The reviewed `FALLBACK_LICENSES` map in the script. These entries are facts
   about the packages this repository pins, kept deterministic across metadata
   formats.
2. Installed package metadata (`License-Expression`, `License`, then
   `License :: OSI Approved ::` classifiers) for a new package not in the map.
3. `UNKNOWN` when neither source yields a usable license.

`UNKNOWN` is **not** allowed. A new dependency with missing or unrecognised
license metadata fails the job until a reviewer adds the license fact to the
map (or the dependency is replaced). Do not add an allowlist entry for a license
without checking the actual package license first.

## Allowlist

`0BSD`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `CC0-1.0`, `ISC`,
`LGPL-2.1-or-later`, `LGPL-3.0-or-later`, `MIT`, `MPL-2.0`, `PSF-2.0`,
`Python-2.0`, `Unlicense`.

An `OR`/`AND` expression is allowed only when every referenced license is in the
allowlist. This is intentionally conservative: a dual-licensed package that
could be used under a permissive option still needs a reviewed entry if the
other option is disallowed.

## SBOM format

The output is **not CycloneDX**. It is a small project-owned schema documented
here so we do not claim a standard we do not fully implement:

```json
{
  "schema": "ai-code-assistant-sbom/1",
  "generator": "scripts/dependency_policy.py",
  "format_note": "Minimal project SBOM; not a CycloneDX document.",
  "scope": "declared direct Python requirements (transitive packages excluded)",
  "component_count": 25,
  "truncated": false,
  "components": [
    {
      "name": "Flask",
      "version": "3.1.3",
      "license": "BSD-3-Clause",
      "pinned": true,
      "source": "requirements.txt"
    }
  ]
}
```

Bounds: at most 500 components (sorted by normalised name, `truncated` is set
when clipped) and at most 200 characters per string field. The document is
deterministic for a given requirement set.

## CI

The `dependency-policy` job in `.github/workflows/ci.yml` installs the pinned
requirements, runs:

```bash
python scripts/dependency_policy.py --sbom sbom-python.json
```

and uploads `sbom-python.json` as a short-retention build artifact. A failing
license exits non-zero and lists every offender, so the job fails before the
artifact step. Release managers can attach that artifact to a release.

## Dependency review process

1. Open the dependency change with its exact `==` pin in one of the requirement
   files.
2. Run `python scripts/dependency_policy.py --sbom /tmp/sbom.json` locally.
3. If the package is new and its license is allowed by policy, add its license
   fact to `FALLBACK_LICENSES` in the script and mention the package in the pull
   request.
4. If the license is not allowed, do not add it. Choose another package or get
   an explicit, documented exception approved by a maintainer first.
5. CI repeats the check and stores the SBOM artifact.

The existing dependency workflows remain in place: `dependency-review.yml`
blocks new high/critical advisories in pull requests, and
`dependency-audit.yml` runs `pip-audit` and `npm audit` on pinned versions.
