"""The provider service layer decrypts and uses the user's stored key (#30).

Encryption at rest already lives in ``app/services/crypto.py`` + ``ApiKey``.
These tests pin the remaining guarantee: the provider layer is the only place a
stored key is decrypted, and a stored key is used when the environment does not
supply one - while plaintext is never persisted.
"""

from app.extensions import db
from app.models import ApiKey
from app.services.api_keys import create_key
from app.services.providers.registry import get_provider


def _force_provider(monkeypatch, name="openai", env_key=None):
    monkeypatch.setenv("LLM_PROVIDER", name)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if env_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", env_key)


class TestProviderUsesStoredKey:
    def test_injects_stored_key_when_env_missing(self, app, make_user, monkeypatch):
        user = make_user()
        _force_provider(monkeypatch, "openai")
        create_key(user, provider="openai", secret="sk-stored-30")

        provider = get_provider("openai", user=user)

        assert provider.name == "openai"
        assert provider.api_key == "sk-stored-30"

    def test_environment_key_takes_precedence(self, app, make_user, monkeypatch):
        user = make_user()
        _force_provider(monkeypatch, "openai", env_key="sk-env-30")
        create_key(user, provider="openai", secret="sk-stored-30")

        assert get_provider("openai", user=user).api_key == "sk-env-30"

    def test_unconfigured_when_no_key_anywhere(self, app, make_user, monkeypatch):
        user = make_user()
        _force_provider(monkeypatch, "openai")

        assert get_provider("openai", user=user).api_key == ""

    def test_inactive_key_is_ignored(self, app, make_user, monkeypatch):
        user = make_user()
        _force_provider(monkeypatch, "openai")
        key = create_key(user, provider="openai", secret="sk-inactive-30")
        key.is_active = False
        db.session.commit()

        assert get_provider("openai", user=user).api_key == ""

    def test_key_for_another_provider_is_ignored(self, app, make_user, monkeypatch):
        user = make_user()
        _force_provider(monkeypatch, "openai")
        create_key(user, provider="anthropic", secret="sk-anthropic-30")

        assert get_provider("openai", user=user).api_key == ""

    def test_keyless_provider_ignores_stored_keys(self, app, make_user, monkeypatch):
        user = make_user()
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        create_key(user, provider="mock", secret="irrelevant")

        assert get_provider("mock", user=user).name == "mock"

    def test_plaintext_is_never_persisted(self, app, make_user, monkeypatch):
        user = make_user()
        _force_provider(monkeypatch, "openai")
        create_key(user, provider="openai", secret="sk-never-plain-30")

        row = ApiKey.query.filter_by(user_id=user.id).first()
        assert row is not None
        assert "sk-never-plain-30" not in (row.encrypted_value or "")
        # The plaintext is recoverable only through the provider layer.
        assert get_provider("openai", user=user).api_key == "sk-never-plain-30"
