"""Tests for the per-user LLM response cache (issue #18)."""

from app.models import ApiKey
from app.services import llm_cache
from app.services.providers.base import ProviderResponse


class _FakeProvider:
    """Counts chat() calls so cache hits are observable."""

    name = "fake"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, model=None, params=None):
        self.calls += 1
        return ProviderResponse(content=f"reply {self.calls}", model=model or "fake-1")


class _User:
    def __init__(self, uid):
        self.id = uid


def _msgs(text="hello"):
    return [{"role": "user", "content": text}]


class TestSignature:
    def test_same_request_same_signature(self):
        kwargs = {
            "user_id": 1,
            "provider": "openai",
            "model": "gpt",
            "params": {"temperature": 0},
            "messages": _msgs(),
        }
        assert llm_cache.request_signature(**kwargs) == llm_cache.request_signature(**kwargs)

    def test_different_user_changes_signature(self):
        base = {"provider": "p", "model": None, "params": None, "messages": _msgs()}
        assert llm_cache.request_signature(user_id=1, **base) != llm_cache.request_signature(
            user_id=2, **base
        )

    def test_model_params_and_prompt_change_signature(self):
        base = {
            "user_id": 1,
            "provider": "p",
            "model": "m",
            "params": {"temperature": 0},
            "messages": _msgs("a"),
        }
        signature = llm_cache.request_signature(**base)
        assert llm_cache.request_signature(**{**base, "model": "m2"}) != signature
        assert llm_cache.request_signature(**{**base, "params": {"temperature": 1}}) != signature
        assert llm_cache.request_signature(**{**base, "messages": _msgs("b")}) != signature


class TestCacheClass:
    def test_hit_and_miss_counters(self):
        cache = llm_cache.LLMResponseCache(ttl=60, max_entries=4, clock=lambda: 1000.0)
        assert cache.get("k") is None
        cache.set("k", ProviderResponse(content="x"))
        assert cache.get("k").content == "x"
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["sets"] == 1

    def test_ttl_expiry(self):
        now = {"t": 0.0}
        cache = llm_cache.LLMResponseCache(ttl=10, clock=lambda: now["t"])
        cache.set("k", ProviderResponse(content="x"))
        now["t"] = 5
        assert cache.get("k") is not None
        now["t"] = 11
        assert cache.get("k") is None
        assert cache.stats()["expirations"] == 1

    def test_size_cap_evicts_lru(self):
        cache = llm_cache.LLMResponseCache(ttl=60, max_entries=2, clock=lambda: 0.0)
        cache.set("a", ProviderResponse(content="a"))
        cache.set("b", ProviderResponse(content="b"))
        cache.get("a")  # mark "a" recently used
        cache.set("c", ProviderResponse(content="c"))
        assert cache.get("b") is None
        assert cache.get("a") is not None
        assert cache.stats()["evictions"] == 1

    def test_disabled_never_stores(self):
        cache = llm_cache.LLMResponseCache(enabled=False)
        cache.set("k", ProviderResponse(content="x"))
        assert cache.get("k") is None


class TestCachedChat:
    def test_identical_requests_hit_cache(self, app):
        provider = _FakeProvider()
        user = _User(1)
        first = llm_cache.cached_chat(user, _msgs(), provider=provider)
        second = llm_cache.cached_chat(user, _msgs(), provider=provider)
        assert provider.calls == 1
        assert first.content == second.content

    def test_never_leaks_across_users(self, app):
        provider = _FakeProvider()
        llm_cache.cached_chat(_User(1), _msgs(), provider=provider)
        llm_cache.cached_chat(_User(2), _msgs(), provider=provider)
        assert provider.calls == 2

    def test_no_cache_bypass(self, app):
        provider = _FakeProvider()
        user = _User(1)
        llm_cache.cached_chat(user, _msgs(), provider=provider)
        llm_cache.cached_chat(user, _msgs(), provider=provider, no_cache=True)
        assert provider.calls == 2

    def test_key_rotation_invalidates(self, app, db, make_user):
        user = make_user(username="cacheuser", email="cache@example.com")
        key = ApiKey(user_id=user.id, provider="fake", encrypted_value="v1")
        db.session.add(key)
        db.session.commit()

        provider = _FakeProvider()
        llm_cache.cached_chat(user, _msgs(), provider=provider)
        llm_cache.cached_chat(user, _msgs(), provider=provider)
        assert provider.calls == 1

        key.updated_at = key.updated_at.replace(year=key.updated_at.year + 1)
        db.session.commit()

        llm_cache.cached_chat(user, _msgs(), provider=provider)
        assert provider.calls == 2
