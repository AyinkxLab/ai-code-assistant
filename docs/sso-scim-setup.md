# SSO and SCIM setup guide

This guide is provider-agnostic: it works the same whether your identity
provider is Okta, Entra ID, Google Workspace, Keycloak, OneLogin, or anything
else. It separates what this repository **actually implements today** from the
OIDC/SAML/SCIM protocol features that are **not implemented yet**, so you do not
configure an identity provider against endpoints that do not exist.

Related: [domain verification and SSO enforcement](sso-domain-verification.md)
and the [API error catalog](api-errors.md).

## Implementation status

| Capability | Status | Where it lives |
| --- | --- | --- |
| DNS proof of email-domain ownership | Supported | `app/services/domain_verification.py`, `workspace_domains` |
| SSO enforcement (block password sign-in for a verified domain) | Supported | `app/auth/routes.py` (`login`) |
| Account deactivation revokes an existing browser session | Supported | `app/auth/routes.py` (`_revoke_deprovisioned_sessions`) |
| Workspace provisioning and role mapping (manual/API) | Supported | `/workspaces/api/workspaces/<id>/members` |
| Workspace deprovisioning (soft removal) | Supported | `DELETE /workspaces/api/workspaces/<id>/members/<uid>` |
| OIDC relying party (authorization-code login, ID token validation) | **Not implemented** | — |
| SAML 2.0 service provider (ACS, metadata, assertion validation) | **Not implemented** | — |
| SCIM 2.0 Groups (`/scim/v2/Groups`) | Supported (minimal) | `app/scim/routes.py`, `app/services/scim_groups.py` |
| SCIM 2.0 Users (`/scim/v2/Users`) | **Not implemented** | — |
| Automatic JIT user provisioning on first SSO login | **Not implemented** | — |
| IdP-group-to-role sync for existing users | Supported (safe roles only) | `/api/group-roles/<workspace_id>` |

The practical consequence is important: this application does not receive an
identity-provider callback and does not issue a dedicated SCIM secret. It
records that a domain is owned by a workspace and uses that fact to refuse
password sign-in for accounts on that domain. The minimal Groups endpoint
below accepts the existing hashed API tokens described in
[API tokens](api-tokens.md).

## Protocol setup: what to configure (and what not to)

The following values are intentionally "none" because the corresponding code
does not exist in this repository. Do not point a live provider at guessed
paths.

### OIDC

| Setting | Required value for this application |
| --- | --- |
| Redirect/callback URI | **None.** There is no `/auth/sso/callback`, `/auth/oidc/callback`, or equivalent route. |
| Scopes | **None consumed.** The application does not request `openid`, `profile`, `email`, or refresh scopes. |
| Discovery document | Not fetched. |
| Client ID / client secret | Not read from the application environment. Do not store one for this app yet. |
| ID token / access token / refresh token | Not received, validated, or stored. |

If your deployment terminates OIDC at a reverse proxy or identity-aware proxy,
that proxy issues the normal Flask session cookie used by this app. The proxy
is outside this repository; keep its client secret in the proxy's secret store,
not in this application's configuration.

### SAML 2.0

| Setting | Required value for this application |
| --- | --- |
| ACS (assertion consumer service) URL | **None.** There is no SAML endpoint. |
| SP entity ID | Not used. |
| SP metadata URL | Not published. |
| Signing/encryption certificates | Not used by this application. |

Do not configure a production IdP to post assertions to this app; the
assertion would 404 and no XML signature validation exists.

### SCIM 2.0

| Setting | Required value for this application |
| --- | --- |
| SCIM Groups base URL | `/scim/v2/Groups?workspace_id=<id>` (minimal implementation only) |
| Bearer token | An existing [API token](api-tokens.md) with `read` for list/get, `write` for create/update, or `admin` for delete. Only its SHA-256 hash is stored. |
| Supported resources | `Groups` only. There is no `/scim/v2/Users` endpoint and no JIT user creation. |

Map directory groups to workspace roles with the organization API:

```http
POST /api/group-roles/<workspace_id>
GET /api/group-roles/<workspace_id>
PUT /api/group-roles/<workspace_id>/<mapping_id>
DELETE /api/group-roles/<workspace_id>/<mapping_id>
```

The body is `{"group_external_id": "engineering", "role": "contributor"}`.
Only `viewer` and `contributor` are accepted: a directory group can never grant
the built-in `owner` role, an organization `admin` role, or global admin. A
mapping is applied when group membership changes through SCIM. SCIM-managed
memberships are tracked so removing a group or mapping revokes only roles that
SCIM itself granted; manual memberships are never overwritten downward.

Use the workspace member API or admin UI described below for owners, manual
overrides, and account deactivation. All SCIM and mapping failures use the
[API error catalog](api-errors.md).

## Supported setup: domain verification and SSO enforcement

All examples assume an authenticated workspace owner session. The same
`manage_settings` capability as the web UI is required; non-owners receive
`403 forbidden`, and unauthenticated calls receive `401 unauthorized`.

### 1. Create a DNS challenge

```http
POST /auth/api/domains/challenge
Content-Type: application/json

{"workspace_id": 12, "domain": "example.com"}
```

The `201` response includes:

```json
{
  "id": 7,
  "domain": "example.com",
  "txt_record_name": "_aica-verification.example.com",
  "txt_record_value": "aica-verify=<token>",
  "challenge_expires_at": "2026-01-02T12:00:00+00:00"
}
```

Challenges expire after 24 hours. Creating a new challenge replaces the token
and restarts the window. A domain can be claimed by only one workspace.

### 2. Publish the TXT record

Create a DNS TXT record:

- **Name:** `_aica-verification.example.com`
- **Value:** `aica-verify=<token>` (exactly as returned)

Verification reads DNS with a five-second timeout. DNS propagation can lag;
wait for the record to be visible before verifying.

### 3. Verify the domain

```http
POST /auth/api/domains/verify
Content-Type: application/json

{"workspace_id": 12, "domain": "example.com"}
```

A missing, mistyped, or expired value leaves the domain unverified. List the
workspace's domains with:

```http
GET /auth/api/domains?workspace_id=12
```

### 4. Enable SSO enforcement

Only a verified domain can be enforced:

```http
PATCH /auth/api/domains/7
Content-Type: application/json

{"workspace_id": 12, "enforce_sso": true}
```

From that moment, `/auth/login` refuses a correct password for accounts whose
email domain is enforced:

> Password sign-in is disabled for your organization. Use single sign-on (SSO)
> to continue.

The refusal is audited as a `login_failure` with `reason=sso_enforced`. MFA and
inactive-account checks still apply to any login path that is allowed.

### 5. Provision workspace members and map roles

Provisioning is explicit today; there is no "create user on first SSO login"
step. Add an existing account by username and assign the workspace role that
matches the IdP group:

```http
POST /workspaces/api/workspaces/12/members
Content-Type: application/json

{"username": "alice", "role": "viewer"}
```

Allowed workspace roles in this API are `viewer` and `contributor`; the owner
is fixed by `Workspace.user_id`. Change a mapped role with:

```http
PATCH /workspaces/api/workspaces/12/members/42
Content-Type: application/json

{"role": "contributor"}
```

### 6. Deprovision a member

Removal is a soft delete (`status=removed`) that preserves history and cancels
pending invitations:

```http
DELETE /workspaces/api/workspaces/12/members/42
```

The removed member's next request to the workspace resolves as a non-member
(`404`). To deprovision the account globally as well, deactivate it
(`User.is_active = False`, for example from an operator script or a future SCIM
handler). The existing browser session is logged out on its next request and
future password logins are refused with the disabled-account message.

## Security notes

- **There are no OIDC/SAML secrets to store yet.** Do not invent a client
  secret or signing certificate for this application. SCIM uses existing hashed
  API tokens; do not create or store a separate plaintext SCIM token.
- **Store `SECRET_KEY` in a real secret manager.** It signs Flask session
  cookies; the production configuration refuses to start without it. Rotating
  it invalidates every browser session.
- **Protect session cookies.** Production sets `Secure`, `HttpOnly`, and
  `SameSite=Lax`; serve the app over HTTPS and do not disable these flags.
- **The DNS token is an ownership proof, not an authentication secret.** Anyone
  who can publish the TXT record controls the domain. Never put unrelated
  secrets in the TXT record, and create a fresh challenge if a token leaks.
- **Do not publish challenge tokens or session cookies in logs or tickets.**
  The domain JSON intentionally omits the stored token; only the challenge
  response exposes the value the owner must publish.
- **Enforcement alone is not authentication.** Because no IdP callback exists,
  blocking password login does not create a working SSO path. Only turn on
  `enforce_sso` when you have an approved external access path (for example an
  identity-aware proxy) that establishes the Flask session.
- **API keys are separate from browser sessions.** Deactivating a user revokes
  the browser session but does not automatically deactivate that user's API
  keys; revoke them explicitly with `/keys/api/keys/<id>`.
- **Audit the changes.** Challenge creation, verification, and enforcement
  changes are written to the workspace audit chain as
  `domain_challenge_created`, `domain_verified`, and
  `domain_sso_enforcement_changed`.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `400 invalid_input` on challenge/verify | Missing/non-integer `workspace_id`, missing `domain`, or invalid DNS name | Send a bare domain such as `example.com`; no scheme, path, wildcard, or trailing dot problems. |
| `401 unauthorized` | No authenticated session | Sign in as the workspace owner before calling the API. |
| `403 forbidden` | Caller lacks `manage_settings` (not the owner) | Use the workspace owner account. |
| `409 conflict` creating a challenge | Domain already verified here, or claimed by another workspace | Use the existing row, or release it with the owning organization; a domain can have only one workspace. |
| `410 gone` verifying | Challenge older than 24 hours | Create a new challenge and publish the new TXT value. |
| `400 invalid_input` verifying, with "Add a TXT record ..." | TXT record absent, wrong name, or not yet propagated | Publish exactly `aica-verify=<token>` at `_aica-verification.<domain>` and retry after propagation. |
| `409 conflict` enabling enforcement | Domain is not verified yet | Finish the TXT challenge first; enforcement is refused before verification. |
| `404 not_found` patching a domain | `domain_id` is not in that workspace | Use the `id` returned by `GET /auth/api/domains?workspace_id=...`. |
| Correct password is rejected with "Password sign-in is disabled" | `enforce_sso` is on for the account's domain | Expected. Use the organization's approved SSO path, or turn enforcement off for the domain. |
| Login says the account is disabled | `User.is_active` is false | Reactivate the account through your operator tooling; the session is already revoked. |
| Login demands MFA enrollment | Workspace MFA policy applies and the user has no TOTP secret | Enroll MFA or use the documented break-glass recovery. |
| An OIDC/SAML callback or `/scim/v2/Users` request returns `404` | Those protocol endpoints are not implemented in this repository | Do not configure them; see [Implementation status](#implementation-status). |
| A deprovisioned user still shows as authenticated in an old tab | The session had not made another request yet | The next request logs the session out. If it persists, revoke the API keys and confirm `is_active` is actually false in the database. |

## Tests

`tests/test_sso_scim_e2e.py` exercises the supported lifecycle end to end:
owner login, DNS challenge/verification, enforcement, password-login refusal,
workspace provisioning, role mapping, soft deprovisioning, and session
revocation after account deactivation. It includes a loopback-only mock
identity provider for the OIDC/SAML/SCIM round trip, which is skipped until the
protocol endpoints exist; no live vendor is contacted.
