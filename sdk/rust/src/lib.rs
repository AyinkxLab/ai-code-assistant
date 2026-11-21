//! Rust client skeleton for the AI Code Assistant public API (Phase 12, #660).
//!
//! The crate is intentionally small and focused:
//!
//! - [`Client`] validates configuration and builds [`PreparedRequest`] values.
//! - [`Error`] maps HTTP statuses and transport problems to structured variants.
//! - [`TestServer`] is a tiny, deterministic HTTP server for request/response
//!   tests that never touch the network beyond loopback.
//! - [`compute_signature`] and [`verify_signature`] implement the shared
//!   webhook signing contract.
//!
//! Transport is deliberately pluggable: this skeleton builds requests and
//! models errors, while a caller supplies the HTTP transport (including TLS).
//!
//! # Minimum supported Rust version
//!
//! The MSRV is **1.75.0**, documented in `Cargo.toml` (`rust-version`).

#![forbid(unsafe_code)]

pub mod errors;
pub mod request;
pub mod signature;
pub mod test_server;

pub use errors::{Error, Result};
pub use request::{Client, Method, PreparedRequest, RequestBuilder};
pub use signature::{
    compute_signature, signature_headers, signed_content, verify_signature,
    DEFAULT_TOLERANCE_SECONDS, SIGNATURE_HEADER, SIGNATURE_VERSION, TIMESTAMP_HEADER,
};
pub use test_server::TestServer;
