# Team Collaboration

Phase 7 turns single-user workspaces into shared team workspaces. Owners can
invite teammates, assign roles, transfer ownership, and review an audit trail;
members can comment on projects, watch activity, and manage their
notifications.

This document is the user-facing feature guide **and** the developer guide.
The API is documented separately in [api-collaboration.md](api-collaboration.md).

## Feature summary

- **Workspace member lifecycle** — add members by username, remove (soft
  delete, history preserved), update roles, self-service leave, and atomic
  ownership transfer.
- **Invitations** — email-based invites for registered and unregistered users,
  with one-time hashed tokens, a public landing page, accept/decline, TTL
  expiry, and owner cancellation.
- **Roles & permissions** — `owner` / `contributor` / `viewer` with a central,
  capability-based permission matrix.
- **Activity feed & audit** — every team action is recorded; members see a
  non-audit feed, the owner sees the full audit subset with metadata.
- **Notifications** — a per-user inbox (invitations, mentions, membership,
  role changes, AI events), unread badge, mark-read/read-all, and per-category
  preferences.
- **Project discussion** — threaded comments on projects with `@username`
  mentions that notify active members.
- **Collaboration settings** — per-workspace toggle for invitations and a
  default member role.
- **Permission-aware AI** — every AI prompt is grounded in the current
  workspace/project with an escaped, bounded team roster, and content access
  fails closed.

## Roles

| Role | What they can do |
| ---- | ---------------- |
| `owner` | Manage members, invitations, settings; transfer ownership; read audit; full project access |
| `contributor` | Comment, view activity/members, presence, self-leave |
| `viewer` | Comment, view activity/members, presence, self-leave |

The owner is identified by `Workspace.user_id` (authoritative even if a
membership row is stale); everyone else resolves through an **active**
`WorkspaceMember` row. Non-members resolve to no role and fail closed.

The full capability matrix lives in `CAPABILITIES` in
`app/services/permissions.py` (issue #136) and is rendered in the
[API reference](api-collaboration.md#permission-matrix).

Optional organization, team, and workspace custom roles are resolved with the
deterministic [role precedence rules](role-precedence.md) (#599).

## Invitation flow

1. The **owner** creates an invitation for an email
   (`POST /collaboration/api/workspaces/<id>/invitations`). The raw token is
   returned once and emailed (only the SHA-256 hash is stored).
2. The invitee opens the landing page
   (`GET /collaboration/invitations/<token>`) and is offered **Accept** /
   **Decline** (or login/register links with a `next` back to the landing
   page).
3. Accepting is atomic: the membership is created (or a removed member is
   reactivated), the invitation is marked accepted, the inviter is notified,
   and the action is recorded in activity/audit.
4. Invitations expire after `INVITE_TTL_HOURS` (default 168). Declined or
   cancelled invitations cannot be accepted; unknown/expired/cancelled tokens
   return uniform `404`s, and the accept/decline/landing endpoints are IP
   rate-limited.

Invitations are the only token channel — tokens are never placed in
notification payloads or logs.

## Notifications

The header **Notifications** bell shows an unread badge (polled every 60 s).
The inbox supports pagination, mark-read, and mark-all-read. Notification
categories and their preference keys:

| Type | Preference | Purpose |
| ---- | ---------- | ------- |
| `invitation` | `invitations` | You were invited to a workspace |
| `mention` | `mentions` | Someone `@mentioned` you in a project comment |
| `membership` | `membership` | You were added to / removed from a workspace |
| `role_change` | — (always delivered) | Your role or ownership changed |
| `ai_event` | `ai_events` | A long-running AI analysis completed |

All preferences default to on; `role_change` is always delivered.

## FAQ

- **Why can't the owner leave?** Ownership is tied to `Workspace.user_id`. The
  owner must transfer ownership first, which demotes them to contributor.
- **What happens when I remove a member?** The membership is soft-deleted
  (`status = removed`) so history is preserved; pending invitations for them
  are cancelled, and they are notified.
- **What does ownership transfer do?** The target becomes owner, the previous
  owner becomes a contributor, and every project's owner column moves to the
  new owner in a single transaction.
- **Can a viewer see source code?** Not yet — source-content routes
  (tree/file/search/chat/analyze) remain owner-scoped. Members can comment,
  view activity, and see the member list. Member content access is tracked in
  issue #127.
- **Can I turn off emails?** Preferences only gate in-app notifications; emails
  are sent only for invitations when SMTP is configured.

---

## Developer guide

### Where the code lives

| Concern | Location |
| ------- | -------- |
| Blueprint & routes | `app/collaboration/routes.py`, `app/workspaces/routes.py` (members) |
| Models | `app/models/workspace_member.py`, `invitation.py`, `activity_event.py`, `notification.py`, `notification_preference.py`, `project_comment.py`, `workspace_settings.py` |
| Permission engine | `app/services/permissions.py` (issue #142) |
| Activity service | `app/services/activity.py` |
| Notifications | `app/services/notifications.py` |
| Mentions | `app/services/mentions.py` |
| Invitations | `app/services/invitations.py`, `app/services/email.py` |
| Rate limiting | `app/services/ratelimit.py` |
| AI context gating | `app/services/project_analysis.py` (`assert_content_access`, `team_context`) |
| Frontend | `app/templates/workspaces/{members,audit,detail}.html`, `app/templates/collaboration/{invitation,notifications}.html`, `app/static/js/{members,audit,invitation,notifications}.js` |
| Migration | `migrations/versions/a38bfbf71b9e_add_phase_7_collaboration_tables.py` |

### Permission model and how to add a capability

Roles are resolved in `role_for(workspace_id, user)`; capabilities are a plain
dict in `CAPABILITIES`:

```python
CAPABILITIES = {
    "manage_invitations": ((ROLE_OWNER,), "Create, list, and cancel invitations"),
    ...
}
```

`role_can(role, capability)` fails closed (unknown roles/`None` grant
nothing); `can(capability, workspace_id)` resolves the current user's role and
checks it.

**To add a new capability:**

1. Add a `"capability_name": (allowed_roles, "description")` entry to
   `CAPABILITIES`.
2. Require it on a route with the `@require_workspace_capability("name")`
   decorator (resolves `workspace_id`, 404 for non-members, 403 without the
   capability).
3. For member-scoped read access use `@require_workspace_member` plus
   `resolve_workspace(workspace_id)`.
4. Add tests in `tests/test_permissions.py` (matrix + route enforcement).
5. Update `docs/api-collaboration.md` (permission matrix + endpoint table) and
   this document.

### How to add a new endpoint

1. Decide the capability (above). Pages use
   `@login_required` + the capability/member decorator; JSON APIs follow the
   existing conventions (pagination helpers `_page()`/`_per_page()`,
   `{"error": ...}` bodies, `404` over `403` for non-members to avoid an
   existence oracle).
2. Record the action with `record_activity(...)` (feed + audit surface) and
   `notify(...)` when a user should be informed.
3. Add route tests covering the happy path, each error code, and the
   member/owner authorization split.

### How to extend notification types

1. Add a `type` string constant (e.g. `"review_requested"`).
2. Create a `Notification` via `notify(user, type, actor=..., workspace=...,
   payload={...}, link=...)`. Payloads must stay small and safe — never
   secrets, tokens, or file contents.
3. Add a preference key to `PREFERENCE_TYPES` and map the type in
   `TYPE_PREFERENCE_MAP` in `app/models/notification_preference.py` (use
   `None` to always deliver).
4. Surface the type label in the inbox UI if needed, and add preference tests
   in `tests/test_notifications.py`.

### Security invariants

- **Fail closed:** unknown roles, non-members, and stale memberships resolve
  to no permissions; `assert_content_access` raises 403 on any mismatch before
  prompts are assembled.
- **No existence oracle:** workspace/project/invitation lookups return uniform
  `404` for inaccessible resources.
- **One-time tokens:** invitation tokens are stored as SHA-256 hashes, returned
  once, and never persisted in notifications/logs; email is the only channel.
- **Append-only audit:** `activity_events` are created with the action and
  never updated or deleted by application code.
- **Bounded AI context:** prompts carry an escaped project/team header and a
  roster capped by `PROJECT_MAX_MEMBER_CONTEXT`.

## Known limitations & adjacent work

- **Member content access (#127)** — members can collaborate but cannot open
  source-content routes yet; notification links to the project explorer open
  for owners only.
- **WebSockets (#50)** — presence uses a cheap heartbeat, not live
  WebSockets.
- **Secure share links (#51)** — invitations are email-gated, not public share
  links.
- **Inline review comments (#52)** — comments are project-level threads, not
  line-level review comments.
- **Shared team prompt library (#53)** — prompts remain per-user.
- **General audit framework (#34)** — the audit view is workspace-scoped, not
  a platform-wide framework.
