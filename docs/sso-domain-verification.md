# Domain verification and SSO enforcement

For the provider-agnostic OIDC/SAML/SCIM setup status and troubleshooting, see
the [SSO and SCIM setup guide](sso-scim-setup.md).

Organizations can prove they own an email domain and then block password
sign-in for accounts on that domain. The implementation is
`app/services/domain_verification.py`; the data lives in `workspace_domains`
(`app/models/workspace_domain.py`).

## Verification flow

1. A workspace owner requests a challenge:

   ```http
   POST /auth/api/domains/challenge
   {"workspace_id": 12, "domain": "example.com"}
   ```

   The response includes `txt_record_name` and `txt_record_value`
   (`_aica-verification.example.com` and `aica-verify=<token>`). Challenges
   expire after **24 hours**; requesting another challenge replaces the token
   and restarts the window.
2. The owner publishes that value as a DNS TXT record.
3. The owner verifies:

   ```http
   POST /auth/api/domains/verify
   {"workspace_id": 12, "domain": "example.com"}
   ```

   Verification reads the TXT record with a five-second DNS timeout. When the
   exact `aica-verify=<token>` value is present before expiry, the domain is
   marked verified. A missing, mistyped, or stale record is rejected without
   changing the stored state.

## SSO enforcement

Only a verified domain can be enforced:

```http
PATCH /auth/api/domains/7
{"workspace_id": 12, "enforce_sso": true}
```

`GET /auth/api/domains?workspace_id=12` lists pending and verified domains for
the owner.

When `enforce_sso` is true, the password login form accepts the credentials but
refuses the session with:

> Password sign-in is disabled for your organization. Use single sign-on (SSO)
> to continue.

The failed attempt is audited with `reason=sso_enforced`.

## Permissions and errors

Every domain operation requires the same workspace `manage_settings`
capability as the web UI (currently the owner). Non-owners receive `403`
`forbidden`; unauthenticated callers receive `401` `unauthorized`. All
responses use the [API error catalog](api-errors.md):

| Situation | HTTP | Code |
| --- | --- | --- |
| Missing/malformed workspace id or domain | 400 | `invalid_input` |
| Non-owner or missing capability | 403 | `forbidden` |
| Domain claimed by another workspace, already verified | 409 | `conflict` |
| Expired challenge | 410 | `gone` |
| TXT record absent or wrong | 400 | `invalid_input` |
| Enforce requested before verification | 409 | `conflict` |
| Domain id not in the workspace | 404 | `not_found` |

## Scope notes

- Verification proves DNS control only. It does not configure or run an SSO
  identity provider; it makes the login policy ready for one.
- A domain can belong to only one workspace, so two organizations cannot both
  enforce SSO for the same email domain.
- The migration is `migrations/versions/590a1b2c3d4e_add_workspace_domains.py`.
  It sits on a merge revision that re-joins the two Phase 11 migration heads,
  so `flask db heads` reports exactly one head.
