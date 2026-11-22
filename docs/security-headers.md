# Security headers and Content-Security-Policy

Every response passes through an `after_request` hook in `app/__init__.py` that
sets the headers below.

| Header | Value |
| ------ | ----- |
| `Content-Security-Policy` | See the policy below. |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `X-Frame-Options` | `DENY` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` on HTTPS only |

HSTS is deliberately not sent on plain HTTP. Local development runs on HTTP, and
a browser that receives HSTS for `localhost` would force HTTPS and break the dev
server. The header is added only when `request.is_secure` is true (direct TLS or
a trusted proxy that sets the scheme).

## Content-Security-Policy

```
default-src 'self';
base-uri 'self';
object-src 'none';
frame-ancestors 'none';
form-action 'self';
script-src 'self';
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;
font-src 'self' https://fonts.gstatic.com;
img-src 'self' data:;
connect-src 'self' ws: wss:
```

The policy has no `unsafe-eval` and no inline JavaScript. All application
scripts are same-origin files under `app/static/js`; the only inline `<script>`
block is the `application/json` translation catalog, which is not executable.

## Documented exceptions

- **`style-src 'unsafe-inline'`** — several templates set layout values with
  `style="..."` attributes (for example progress-bar widths). Removing the
  exception would require moving every value into a stylesheet, which is a
  larger refactor than this hardening pass. Inline styles cannot execute code.
- **Google Fonts (`fonts.googleapis.com` / `fonts.gstatic.com`)** — the base
  template preconnects to and loads the Inter and JetBrains Mono stylesheets.
- **`connect-src ws: wss:`** — Socket.IO uses the same origin with a WebSocket
  upgrade and polling fallback.
- **Inline event handlers** — there are none. `tests/test_security_headers.py`
  scans every template for `onclick=`, `onload=`, and the other common handler
  attributes and fails if one is added without being recorded as an exception.
  There is no nonce mechanism because the templates have no inline executable
  scripts; a nonce framework would add complexity without removing an existing
  exception.

## Testing

`tests/test_security_headers.py` asserts the headers on a normal response, the
HTTPS-only HSTS behaviour, the absence of `unsafe-eval`, the expected CSP
sources, and the no-inline-handler template rule.

## Residual risk

Workspace admins who can edit templates or static files can change what the
browser loads; CSP is a defense-in-depth control against injected markup, not a
substitute for review of template changes. Third-party origins are limited to
the two Google Fonts hosts above.
