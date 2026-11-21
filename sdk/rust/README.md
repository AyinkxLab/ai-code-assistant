# Rust SDK (Phase 12)

A small, dependency-conscious Rust client skeleton for the AI Code Assistant
public API.

- **Request building**: validate the base URL, bearer token, paths, headers, and
  JSON bodies, then produce a `PreparedRequest` for any HTTP/TLS transport.
- **Structured errors**: `Error::from_status` maps 400/401/403/404/409/422/429
  and 5xx to typed variants with retry guidance.
- **Test server**: `TestServer` is a deterministic loopback HTTP server so tests
  never touch the network.
- **Webhook signatures**: `compute_signature` / `verify_signature` implement the
  shared `v1=<hex HMAC-SHA256>` contract over `<timestamp>.<raw body>`.

## Minimum supported Rust version

Rust **1.75.0**. The MSRV is declared in `Cargo.toml` (`rust-version`).

## Example

```rust
use ai_code_assistant_sdk::{Client, Method};

let client = Client::new("https://api.example.com")?.with_token("token")?;
let request = client
    .request(Method::Get, "/api/projects")
    .build()?;
// Hand `request` to your HTTP transport.
# Ok::<(), ai_code_assistant_sdk::Error>(())
```

## Webhook verification

```rust
use ai_code_assistant_sdk::verify_signature;

let valid = verify_signature(secret, body, signature, timestamp, 300, now)?;
assert!(valid);
```

The signature covers the exact bytes `<timestamp>.<raw body>`; do not re-encode
the JSON before verifying.

## Tests

```bash
cargo test
```
