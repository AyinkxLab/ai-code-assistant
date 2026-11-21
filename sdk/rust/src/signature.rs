//! Webhook signature helpers shared with the Python and TypeScript SDKs.
//!
//! Signed content is `<timestamp>.<raw body bytes>` and the signature header is
//! `v1=<lowercase hex HMAC-SHA256>`.

use hmac::{Hmac, Mac};
use sha2::Sha256;

use std::collections::BTreeMap;

use crate::errors::{Error, Result};

/// Header carrying the versioned signature.
pub const SIGNATURE_HEADER: &str = "X-AICA-Signature";
/// Header carrying the Unix timestamp.
pub const TIMESTAMP_HEADER: &str = "X-AICA-Timestamp";
/// Current signature version prefix.
pub const SIGNATURE_VERSION: &str = "v1";
/// Default replay tolerance window, in seconds.
pub const DEFAULT_TOLERANCE_SECONDS: u64 = 300;

fn validate_secret(secret: &str) -> Result<()> {
    if secret.is_empty() {
        return Err(Error::Validation(
            "webhook secret must be a non-empty string".into(),
        ));
    }
    Ok(())
}

/// Returns the exact bytes that are signed.
#[must_use]
pub fn signed_content(timestamp: u64, body: &[u8]) -> Vec<u8> {
    let prefix = format!("{timestamp}.").into_bytes();
    let mut content = Vec::with_capacity(prefix.len() + body.len());
    content.extend_from_slice(&prefix);
    content.extend_from_slice(body);
    content
}

/// Computes the `v1=<hex>` signature for a delivery.
pub fn compute_signature(secret: &str, body: &[u8], timestamp: u64) -> Result<String> {
    validate_secret(secret)?;
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .map_err(|error| Error::Validation(format!("invalid webhook secret: {error}")))?;
    mac.update(&signed_content(timestamp, body));
    Ok(format!(
        "{SIGNATURE_VERSION}={}",
        hex::encode(mac.finalize().into_bytes())
    ))
}

/// Returns the two signing headers for a delivery.
pub fn signature_headers(
    secret: &str,
    body: &[u8],
    timestamp: u64,
) -> Result<BTreeMap<String, String>> {
    let signature = compute_signature(secret, body, timestamp)?;
    let mut headers = BTreeMap::new();
    headers.insert(SIGNATURE_HEADER.to_string(), signature);
    headers.insert(TIMESTAMP_HEADER.to_string(), timestamp.to_string());
    Ok(headers)
}

/// Verifies a signature and rejects timestamps outside the tolerance window.
///
/// Returns `Ok(false)` for a malformed or mismatched signature and for a stale
/// timestamp. Returns `Err` only for invalid configuration (an empty secret).
pub fn verify_signature(
    secret: &str,
    body: &[u8],
    signature: &str,
    timestamp: u64,
    tolerance_seconds: u64,
    now: u64,
) -> Result<bool> {
    validate_secret(secret)?;
    if now.abs_diff(timestamp) > tolerance_seconds {
        return Ok(false);
    }
    let expected = compute_signature(secret, body, timestamp)?;
    Ok(constant_time_eq(expected.as_bytes(), signature.as_bytes()))
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    let length = left.len().max(right.len());
    for index in 0..length {
        let a = left.get(index).copied().unwrap_or(0);
        let b = right.get(index).copied().unwrap_or(0);
        difference |= usize::from(a ^ b);
    }
    difference == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "whsec_known_answer";
    const TIMESTAMP: u64 = 1_700_000_000;
    const BODY: &[u8] = br#"{"event":"ping"}"#;
    const EXPECTED: &str = "v1=ea1980a87d2594bdf099acaf428e26cd5bb851648cde873453733621f66b1d5e";

    #[test]
    fn matches_the_shared_known_answer_vector() {
        assert_eq!(
            compute_signature(SECRET, BODY, TIMESTAMP).unwrap(),
            EXPECTED
        );
        assert_eq!(
            signed_content(TIMESTAMP, BODY),
            br#"1700000000.{"event":"ping"}"#.to_vec()
        );
    }

    #[test]
    fn verifies_and_rejects_tampering_and_replays() {
        assert!(verify_signature(SECRET, BODY, EXPECTED, TIMESTAMP, 300, TIMESTAMP).unwrap());
        assert!(
            !verify_signature(SECRET, BODY, EXPECTED, TIMESTAMP, 300, TIMESTAMP + 301).unwrap()
        );
        assert!(!verify_signature(
            SECRET,
            br#"{"event":"pong"}"#,
            EXPECTED,
            TIMESTAMP,
            300,
            TIMESTAMP
        )
        .unwrap());
        assert!(!verify_signature(SECRET, BODY, "v1=deadbeef", TIMESTAMP, 300, TIMESTAMP).unwrap());
    }

    #[test]
    fn headers_are_stable() {
        let headers = signature_headers(SECRET, BODY, TIMESTAMP).unwrap();
        assert_eq!(headers.get(SIGNATURE_HEADER).unwrap(), EXPECTED);
        assert_eq!(headers.get(TIMESTAMP_HEADER).unwrap(), "1700000000");
        assert!(compute_signature("", BODY, TIMESTAMP).is_err());
    }
}
