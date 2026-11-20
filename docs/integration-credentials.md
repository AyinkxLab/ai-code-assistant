# Encrypted integration credentials

Workspace integration secrets are stored by
`app/services/integration_credentials.py` and the
`integration_credentials` table.

## Encryption

Secrets use the same authenticated encryption as user-owned provider API keys:
AES-256-GCM with a random 96-bit nonce per write and a `v1` version tag
(`app/services/crypto.py::encrypt_api_key`). Only the base64 ciphertext is
persisted. Decryption happens in memory through `decrypt_for_use` immediately
before an integration call.

The key is derived from `SECRET_KEY`. Rotating `SECRET_KEY` invalidates stored
ciphertext; rotate credentials first (see below) while the old secret is still
configured, or re-enter them after the switch.

## Storage

| Column | Meaning |
| --- | --- |
| `workspace_id` | Owner workspace; cascades on delete |
| `integration` | Integration slug, e.g. `github` |
| `name` | Credential name, e.g. `access_token` |
| `encrypted_value` | AES-256-GCM ciphertext (never plaintext) |
| `last_rotated_at` | Last rotation timestamp |

`(workspace_id, integration, name)` is unique, so `set_credential` updates and
re-encrypts an existing secret rather than creating duplicates.

## Permissions and errors

Only workspace owners (the `manage_integrations` capability) may manage
credentials. Errors are structured:

| Exception | `code` |
| --- | --- |
| `IntegrationCredentialError` | `invalid_input` |
| `IntegrationCredentialNotFound` | `not_found` |
| `IntegrationCredentialPermissionError` | `permission_denied` |
| `IntegrationCredentialNotConfigured` | `not_configured` |

## Redaction

`IntegrationCredential.to_dict()` returns metadata only (`integration`, `name`,
`has_secret`, timestamps). It never includes the plaintext or the ciphertext,
and neither does `__repr__`. Log ciphertext-free identifiers, never a secret.

## Rotation

```python
from app.services.integration_credentials import rotate_workspace_credentials

rotate_workspace_credentials(workspace_id, user=owner)
```

Rotation decrypts each secret in memory and re-encrypts it with a fresh nonce.
The plaintext is unchanged, but the stored ciphertext differs every time.

## Migration

`migrations/versions/e3f4a5b6c7d8_add_integration_credentials.py` creates the
table. Apply it with `flask --app wsgi db upgrade`.

## Tests

`tests/test_integration_credentials.py` covers encrypt/decrypt round trips,
different ciphertext across writes, redacted serialization, rotation of one and
many credentials, and permission/input/not-found errors.
