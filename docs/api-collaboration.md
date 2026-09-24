# Collaboration API Reference (Phase 7)

This document is the consolidated reference for the **Phase 7 — Team
Collaboration** endpoints. It covers invitations, membership, activity/audit,
notifications, comments, settings, and presence.

Every endpoint is served by the `collaboration` blueprint
(`app/collaboration/routes.py`) unless noted, and routes authorization through
the central capability module `app/services/permissions.py` (issue #142). The
permission matrix is defined in `CAPABILITIES` there and rendered in the
[developer guide](team-collaboration.md); the tracking issue is #136.

## Conventions

- **Auth:** every JSON API route requires a logged-in user (`@login_required`).
  Unauthenticated requests get a 302 to `/auth/login`.
- **Roles:** `owner` > `contributor` > `viewer`. The workspace `owner` is
  authoritative via `Workspace.user_id`; everyone else resolves through an
  *active* membership row. Non-members resolve to no role and **fail closed**.
- **Not found vs forbidden:** workspace routes return `404` for non-members and
  non-existent workspaces alike, so callers cannot probe ids (no existence
  oracle). Capability violations by a known member return `403`.
- **Pagination:** list endpoints accept `page` (1-based) and `per_page`
  (1–100, default 20). The activity feed instead uses cursor pagination via
  `before=<cursor>`.
- **Errors:** JSON bodies like `{"error": "<message>"}` with status 400/403/404/409/429.
- **CSRF:** HTML pages are protected by Flask-WTF; JSON API calls use
  `Content-Type: application/json`.

## Permission matrix

| Capability | Allowed roles |
| ---------- | ------------- |
| `view_members` | owner, contributor, viewer |
| `leave_workspace` | contributor, viewer (owner transfers instead) |
| `manage_members` | owner |
| `manage_invitations` | owner |
| `manage_settings` | owner |
| `transfer_ownership` | owner |
| `view_audit` | owner |
| `comment` | owner, contributor, viewer |
| `view_activity` | owner, contributor, viewer |
| `heartbeat` | owner, contributor, viewer |

Roles are hierarchical for UI purposes but each capability lists its exact
roles; an unknown/typo'd role never grants an owner capability.

---

## Invitations

### Create invitation — Phase 7

`POST /collaboration/api/workspaces/<workspace_id>/invitations` — **owner**
(`manage_invitations`)

Request:

```json
{"email": "newguy@example.com", "role": "viewer"}
```

- `email` required, validated, lowercased; max 255 chars.
- `role` optional (`owner` is rejected by validation; `contributor`/`viewer`
  allowed); defaults to the workspace setting `default_member_role`.
- Fails `403` when the workspace has `invitations_enabled = false`.
- Fails `400` for self-invite / invalid role / invalid email.
- Fails `409` when the email already belongs to an active member or has a
  pending invitation.

Response `201` — the invitation plus the **one-time raw token** (the token is
stored hashed as SHA-256 and delivered only via the invite email link):

```json
{
  "id": 12,
  "workspace_id": 3,
  "invited_by": 1,
  "inviter_username": "alice",
  "email": "newguy@example.com",
  "role": "viewer",
  "status": "pending",
  "expires_at": "2026-08-22T10:00:00+00:00",
  "accepted_by": null,
  "accepted_at": null,
  "created_at": "2026-08-15T10:00:00+00:00",
  "token": "3f9c...e2a1"
}
```

### List invitations — Phase 7

`GET /collaboration/api/workspaces/<workspace_id>/invitations` — **owner**
(`manage_invitations`)

Query params: `status` (optional filter: `pending` | `accepted` | `declined` |
`cancelled` | `expired`), `page`, `per_page`.

Response `200`:

```json
{
  "items": [ { "id": 12, "workspace_id": 3, "invited_by": 1,
               "inviter_username": "alice", "email": "newguy@example.com",
               "role": "viewer", "status": "pending",
               "expires_at": "2026-08-22T10:00:00+00:00",
               "accepted_by": null, "accepted_at": null,
               "created_at": "2026-08-15T10:00:00+00:00" } ],
  "total": 1,
  "page": 1,
  "per_page": 20
}
```

No token is ever present in list responses.

### Cancel invitation — Phase 7

`DELETE /collaboration/api/workspaces/<workspace_id>/invitations/<invite_id>` —
**owner** (`manage_invitations`)

- `404` if the invitation is not in this workspace.
- `409` if the invitation is not `pending`.

Response `200` with the updated invitation (`status: "cancelled"`).

### Invitation landing page — Phase 7

`GET /collaboration/invitations/<token>` — **public** (no login required)

Renders an HTML landing page describing the invite state (`pending`, `expired`,
`accepted`, `declined`, `cancelled`, or `invalid`). IP rate-limited.

### Accept invitation — Phase 7

`POST /collaboration/api/invitations/<token>/accept` — **logged-in**

- `429` under IP rate limit.
- `404` for unknown/expired/cancelled tokens (uniform — no existence oracle).
- `409` for previously declined invitations, or when already accepted by a
  different account.
- `403` when the logged-in email does not match the invitation's email.
- Accepting reactivates a previously-removed membership (single row, unique
  `(workspace_id, user_id)` constraint preserved) and is atomic.

Response `201` with the membership:

```json
{
  "id": 7,
  "workspace_id": 3,
  "user_id": 9,
  "role": "viewer",
  "status": "active",
  "username": "newguy",
  "joined_at": "2026-08-15T10:05:00+00:00",
  "removed_at": null,
  "last_active_at": null,
  "created_at": "2026-08-15T10:05:00+00:00"
}
```

When the user is already an active member it returns `200` with
`"idempotent": true`.

### Decline invitation — Phase 7

`POST /collaboration/api/invitations/<token>/decline` — **logged-in**

- `429` under IP rate limit; `404` when the invitation is not pending/valid.

Response `200`: `{"ok": true}`.

---

## Membership

### Members list — Phase 7 (read) / #126 (management)

`GET /workspaces/api/workspaces/<workspace_id>/members` — **any member**
(`view_members`)

Response `200` — array of memberships (see Accept response shape). Only
`status: "active"` members are returned, ordered by join time.

### Add member — #126, Phase 7 rewrite (notifications + activity)

`POST /workspaces/api/workspaces/<workspace_id>/members` — **owner**

Request: `{"username": "bob", "role": "viewer"}`.

- `400` missing username / invalid role / owner already a member.
- `404` no such username; `409` already an active member.
- Reactivating a previously-removed member preserves the row.

Response `201` with the membership.

### Update member role — #126, Phase 7 rewrite (notifications + activity)

`PATCH /workspaces/api/workspaces/<workspace_id>/members/<user_id>` — **owner**

Request: `{"role": "contributor"}`. Response `200` with the membership. A
role change records an activity event and notifies the member.

### Remove member — #126, Phase 7 rewrite (notifications + activity)

`DELETE /workspaces/api/workspaces/<workspace_id>/members/<user_id>` — **owner**

Soft-delete: `status` → `removed`, pending invitations for that user are
cancelled, the member is notified, and history is preserved.
`409` if already removed. Response `200`: `{"ok": true}`.

### Leave workspace — Phase 7

`DELETE /collaboration/api/workspaces/<workspace_id>/membership` — **any
member** (`leave_workspace`)

- `400` for the owner — the owner must transfer ownership first.

Response `200`: `{"ok": true}`.

### Transfer ownership — Phase 7

`POST /collaboration/api/workspaces/<workspace_id>/transfer` — **owner**
(`transfer_ownership`)

Request: `{"user_id": 5}` (must be an active member).

Atomic: the target becomes owner (`Workspace.user_id` + membership role), the
previous owner becomes a `contributor` member, and every project's
denormalized `user_id` moves to the new owner. Both parties get a
`role_change` notification.

Response `200`: `{"ok": true, "workspace": { ...workspace.to_dict() }}`.

---

## Activity & Audit

### Activity feed — Phase 7

`GET /collaboration/api/workspaces/<workspace_id>/activity` — **any member**
(`view_activity`)

Query params: `event_type`, `actor` (username), `before` (opaque cursor
returned as `next_cursor`), `per_page` (1–100, default 20).

Members never see the audit-sensitive subset; the owner sees everything. The
member feed never includes event `metadata`.

Response `200`:

```json
{
  "items": [
    { "id": 5, "workspace_id": 3, "actor_id": 1, "actor_username": "alice",
      "event_type": "comment.added", "label": "commented on a project",
      "target_type": "project_comment", "target_id": 8,
      "created_at": "2026-08-15T09:30:00+00:00" }
  ],
  "next_cursor": "2026-08-15T09:30:00+00:00|5"
}
```

`next_cursor` is `null` on the last page. An invalid cursor returns `400`.

### Audit log — Phase 7

`GET /collaboration/api/workspaces/<workspace_id>/audit` — **owner**
(`view_audit`)

Query params: `event_type`, `actor`, `page`, `per_page`. Returns exactly the
audit event subset (`AUDIT_EVENT_TYPES`) and includes the safe `metadata`.

Response `200`:

```json
{
  "items": [
    { "id": 4, "workspace_id": 3, "actor_id": 1, "actor_username": "alice",
      "event_type": "invitation.created", "label": "invited someone",
      "target_type": "invitation", "target_id": 12,
      "created_at": "2026-08-15T10:00:00+00:00",
      "metadata": {"email": "newguy@example.com", "role": "viewer"} }
  ],
  "total": 1,
  "page": 1,
  "per_page": 20
}
```

Audit rows are append-only — there is no update/delete route.

---

## Notifications

All notification endpoints are **strictly current-user scoped**: accessing
another user's notification returns `404`.

### Inbox — Phase 7

`GET /collaboration/api/notifications` — **logged-in**

Query params: `unread=1` (filter), `page`, `per_page`.

Response `200`:

```json
{
  "items": [
    { "id": 21, "type": "invitation", "actor_id": 1,
      "actor_username": "alice", "workspace_id": 3, "project_id": null,
      "payload": {"title": "You've been invited to join Team X",
                  "workspace": "Team X", "role": "viewer"},
      "link": "/collaboration/invitations/3f9c...e2a1",
      "is_read": false,
      "created_at": "2026-08-15T10:00:00+00:00" }
  ],
  "total": 1,
  "unread_count": 1,
  "page": 1,
  "per_page": 20
}
```

### Unread count — Phase 7

`GET /collaboration/api/notifications/count` — **logged-in**

Response `200`: `{"unread": 1}` (drives the header badge; polled by the UI).

### Mark read — Phase 7

`POST /collaboration/api/notifications/<notification_id>/read` — **logged-in**

Idempotent. `404` when the notification belongs to another user or does not
exist. Response `200` with the notification.

### Mark all read — Phase 7

`POST /collaboration/api/notifications/read-all` — **logged-in**

Response `200`: `{"ok": true}`.

### Get preferences — Phase 7

`GET /collaboration/api/notifications/preferences` — **logged-in**

Response `200` (defaults materialized on first access):

```json
{"invitations": true, "mentions": true, "membership": true, "ai_events": true}
```

### Update preferences — Phase 7

`PUT /collaboration/api/notifications/preferences` — **logged-in**

Request (any subset): `{"ai_events": false}`. Response `200` with the full set.
Preferences gate delivery per notification type; unknown types are ignored.

---

## Comments

Project discussion is available to the owner and active members of the
project's workspace (`resolve_project_collab`). Source-content routes remain
owner-scoped until #127.

### List comments — Phase 7

`GET /collaboration/api/projects/<project_id>/comments` — **owner or active
member of the workspace**

Query params: `page`, `per_page`. Response `200` with `items` (oldest first),
`total`, `page`, `per_page`.

### Create comment — Phase 7

`POST /collaboration/api/projects/<project_id>/comments` — **owner or active
member** (`comment`)

Request:

```json
{"content": "Great work on the search module. @bob please review.",
 "parent_id": null}
```

- `content` required (stripped), max `COMMENT_MAX_LENGTH` chars.
- `parent_id` (optional) must reference a comment in the same project.
- `@username` mentions notify the mentioned active members (never the author).
- A `comment.added` activity event is recorded.

Response `201` with the comment:

```json
{
  "id": 8,
  "project_id": 45,
  "author_id": 1,
  "author_username": "alice",
  "parent_id": null,
  "content": "Great work on the search module. @bob please review.",
  "created_at": "2026-08-15T09:30:00+00:00",
  "updated_at": null
}
```

### Delete comment — Phase 7

`DELETE /collaboration/api/projects/<project_id>/comments/<comment_id>` —
**the author or the workspace owner**

- `403` for anyone else; `404` if the comment is not in this project.

Response `200`: `{"ok": true}`.

---

## Inline review comments (#52)

Inline review threads attach to a specific assistant message in a project chat,
optionally anchored to a fenced code block (0-based `block_index`) and/or a
`line_start`/`line_end` range within the message. Anchors store only positions —
never raw source content. Threads allow one level of replies and support a
resolve/unresolve toggle. All routes use `resolve_project_collab` (owner or
active member), and content is scoped to the message's project.

Permissions: any member with the `comment` capability may create comments and
replies; a thread root may be **resolved** by its author or the workspace owner;
a comment may be **deleted** by its author or the workspace owner.

### List threads for a message — #52

`GET /workspaces/api/projects/<project_id>/messages/<message_id>/review-comments`
— **owner or active member**

Response `200`: `{"message_id": 12, "items": [<thread>, ...]}` where each thread
is a comment object plus a `replies` array (oldest first).

### Create thread / reply — #52

`POST /workspaces/api/projects/<project_id>/messages/<message_id>/review-comments`
— **owner or active member** (`comment`)

Request:

```json
{"body": "This block drops the error. @bob please confirm.",
 "block_index": 0, "line_start": 3, "line_end": 5,
 "parent_id": null}
```

- `body` required (stripped), max `REVIEW_COMMENT_MAX_LENGTH` chars.
- The message must be an assistant message (`400` otherwise).
- `block_index` must reference an existing fenced block in the message; line
  bounds must be positive and ordered (`400` otherwise).
- `parent_id` (optional) must be a root comment on the same message; replies are
  one level deep only.
- `@username` mentions notify the mentioned active members (never the author).

Response `201` with the comment (see ``ReviewComment.to_dict`` fields:
`id`, `project_id`, `message_id`, `author_id`, `author_username`, `parent_id`,
`body`, `block_index`, `line_start`, `line_end`, `resolved`, `resolved_by`,
`resolved_by_username`, `resolved_at`, `created_at`).

### Resolve / unresolve — #52

`PATCH /workspaces/api/projects/<project_id>/messages/<message_id>/review-comments/<comment_id>`
— **thread author or workspace owner**

Request: `{"resolved": true}`. Only a thread root can be resolved (`400` for a
reply); anyone else gets `403`. Resolving records `resolved_by`/`resolved_at`.

### Delete comment — #52

`DELETE /workspaces/api/projects/<project_id>/messages/<message_id>/review-comments/<comment_id>`
— **the author or the workspace owner** (`403` otherwise; deleting a root
cascades to its replies)

Response `200`: `{"ok": true}`.

### Review summary — #52

`GET /workspaces/api/projects/<project_id>/review-summary` — **owner or active
member**

Response `200`: `{"open": 2, "resolved": 1, "total": 3, "threads": [...]}` where
each thread carries its anchor, author, `resolved` state, and `reply_count`. This
drives the review summary panel on the chat tab.

---

## Collaboration settings

### Get settings — Phase 7

`GET /collaboration/api/workspaces/<workspace_id>/settings` — **any member**

Response `200`:

```json
{"workspace_id": 3, "invitations_enabled": true,
 "default_member_role": "viewer",
 "created_at": "2026-08-01T09:00:00+00:00", "updated_at": null}
```

### Update settings — Phase 7

`PUT /collaboration/api/workspaces/<workspace_id>/settings` — **owner**
(`manage_settings`)

Request (any subset): `{"invitations_enabled": false,
"default_member_role": "contributor"}`.

- `400` if `default_member_role` is not `viewer` or `contributor`.

Response `200` with the full settings row. A `settings.changed` audit event is
recorded. `invitations_enabled = false` blocks new invitation creation
(`403`).

---

## Presence

### Heartbeat — Phase 7

`POST /collaboration/api/workspaces/<workspace_id>/heartbeat` — **any member**
(`heartbeat`)

Updates the member's `last_seen_at`. Per-user rate limited (`429` on excess).
Response `200`: `{"ok": true}`.

---

## HTML pages

| Path | Role | Description |
| ---- | ---- | ----------- |
| `GET /collaboration/notifications` | logged-in | Notification inbox UI |
| `GET /collaboration/<workspace_id>/members` | member | Team/member management UI (owner manages, members read + leave) |
| `GET /collaboration/<workspace_id>/audit` | owner | Owner-only audit UI |
| `GET /collaboration/invitations/<token>` | public | Invitation landing page |

---

## Route-map verification

Every route above exists in the app; the checklist is maintained alongside the
issue (#160). Verify with:

```bash
flask --app wsgi routes | findstr /i "collaboration"
```
