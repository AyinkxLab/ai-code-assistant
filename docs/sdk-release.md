# Releasing the SDKs

The Python and TypeScript SDKs are published from a tag by the
[`Release SDK`](../.github/workflows/release-sdk.yml) GitHub Actions workflow.
Publishing is **tag-driven** and never runs for pull requests, so fork code
cannot reach the publish credentials.

## Tag format

The tag must match the version in both SDK manifests:

```
sdk-vX.Y.Z
```

The workflow fails early when the tag does not equal `sdk-v<version>` from
`sdk/python/pyproject.toml`.

## Release steps

1. Bump the version in **all** of these places and keep them equal:
   - `sdk/python/pyproject.toml` (`project.version`)
   - `sdk/python/ai_code_assistant/__init__.py` (`__version__`)
   - `sdk/typescript/package.json` (`version`)
2. Update the changelog and SDK docs if the public surface changed.
3. Run the gates locally:

   ```bash
   ruff check .
   black --check .
   pytest tests/test_sdk_python.py tests/test_sdk_quickstart.py -q
   npm --prefix sdk/typescript install --no-audit --no-fund
   npm --prefix sdk/typescript run build
   npm --prefix sdk/typescript run typecheck
   python -m build sdk/python
   ```

4. Commit the version bump, open a pull request, and merge it to `main`.
5. Create and push an annotated tag on the merged commit:

   ```bash
   git tag -a sdk-vX.Y.Z -m "SDK X.Y.Z"
   git push origin sdk-vX.Y.Z
   ```

6. Watch the `Release SDK` workflow. It tests, builds, publishes to PyPI and
   npm, and fails closed if any step fails.

## Credentials

The workflow reads two repository secrets. They are available only to the
tag-triggered job:

| Secret | Used for | Notes |
| ------ | -------- | ----- |
| `PYPI_API_TOKEN` | `twine upload` | PyPI API token scoped to the `ai-code-assistant-sdk` project. |
| `NPM_TOKEN` | `npm publish` | npm automation token with publish access to `@ai-code-assistant/sdk`. |

Security rules:

- Rotate both tokens on a schedule and immediately after any suspected leak.
- Never add these secrets to a pull-request-triggered workflow.
- Keep the workflow's default `permissions: contents: read`; publishing uses
  the registry tokens, not the GitHub token.
- The workflow has no `id-token` or `contents: write` permission, and every
  third-party action is pinned to a full commit SHA.

## Verification

After the workflow succeeds:

```bash
pip index versions ai-code-assistant-sdk
npm view @ai-code-assistant/sdk version
```

## Rollback

- **PyPI**: yank the release on the PyPI project page. A yanked version stays
  installable by exact pin but is hidden from resolution.
- **npm**: deprecate the version (`npm deprecate`) and publish a fixed patch.
  Unpublishing is only allowed within npm's 72-hour window and breaks pinned
  consumers.

Republish a fix as a new patch version; never move or reuse an existing tag.
