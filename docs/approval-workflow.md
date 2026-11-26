# Sensitive-action approval workflow

The approval workflow (issue #601) adds a second-person check to sensitive
operations such as **role changes** and **data deletion**. It is additive: the
existing workspace capability matrix is unchanged, and a global `is_admin`
flag never grants membership in an organization.

## Two-person rule

1. An organization owner or admin opens a request for one sensitive action and
   target. The request carries a small JSON payload describing the intended
   change; credential-like keys (`password`, `token`, `secret`, `api_key`,
   `session`, and similar) are rejected before storage.
2. A **different** organization owner or admin approves or rejects it. The
   requester cannot decide their own request.
3. The requester releases the action only after an approval exists. Rejected,
   expired, and undecided requests are refused by the release check.

Requests expire automatically. `GET /api/approvals` and
`GET /api/approvals/<request_id>` expire stale rows on read; operators can also
run `flask approvals expire` (or `flask approvals expire --dry-run`) from a
scheduled job. Every submission, decision, and expiry is appended to the
organization's audit log.

## API

| Method | Route | Purpose |
| ------ | ----- | ------- |
| `POST` | `/api/approvals` | Create a pending request |
| `GET` | `/api/approvals?workspace_id=<id>` | List requests for one organization |
| `GET` | `/api/approvals/<request_id>` | Read one request |
| `POST` | `/api/approvals/<request_id>/approve` | Second approver approves |
| `POST` | `/api/approvals/<request_id>/reject` | Second approver rejects |
| `POST` | `/api/approvals/<request_id>/authorize` | Requester releases the action |

`action` is either `role_change` or `data_deletion`. `target_type` and optional
`target_id` bind the decision to the exact object. `ttl_minutes` defaults to 60
and is bounded to one week. `payload` is bounded and must contain plain JSON
values only.

All failures use the [API error catalog](api-errors.md): `400 invalid_input`,
`401 unauthorized`, `403 forbidden`, `404 not_found`, `409 conflict`, and
`410 gone` for an expired request.

Related: [enterprise admin guide](enterprise-admin-guide.md),
[audit and compliance guide](audit-compliance-guide.md).
