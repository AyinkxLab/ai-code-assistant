## Summary

Adds a bounded per-user response cache for identical LLM requests. Repeated
requests reuse the stored completion while model, provider, prompt, and key
configuration changes produce a different cache key.

## Linked issue

Closes #38

## Changes

- Hash normalized, model-visible messages and request/provider configuration
	into a deterministic SHA-256 cache key.
- Serve cache hits without calling the provider; cache misses store the
	provider response for later reuse.
- Preserve bounded TTL and LRU eviction, with per-user isolation and key
	rotation invalidation.
- Normalize away non-model metadata such as message timestamps.

## Validation

- [x] `python3 -m pytest -q tests/test_llm_cache.py tests/test_providers.py` passes
- [ ] `ruff check .` passes (not available in the local environment)
- [ ] `black --check .` passes (not run)
- [x] Added/updated tests for the change
- [ ] Docs updated if user-visible (not applicable)

## Notes for reviewers

The cache is process-local and intentionally does not cache streaming responses.
Entries are scoped by user and provider-key fingerprint so responses are not
shared across accounts or retained across credential rotation. The existing
TTL/LRU bounds limit memory growth; cache misses continue to use the provider
normally.
