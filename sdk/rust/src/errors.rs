//! Structured errors for the Rust SDK.

use std::fmt;

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, Error>;

/// A structured SDK error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// Invalid input was rejected before any request was sent.
    Validation(String),
    /// The request was not authorized (HTTP 401) or the caller lacks access.
    Permission(String),
    /// The resource does not exist (HTTP 404).
    NotFound(String),
    /// The request conflicts with current state (HTTP 409).
    Conflict(String),
    /// The request was rate limited (HTTP 429).
    RateLimited {
        /// Human-readable message.
        message: String,
        /// Server-provided `Retry-After`, when present.
        retry_after: Option<u64>,
    },
    /// The server returned an unexpected status or failed internally.
    Server {
        /// HTTP status code.
        status: u16,
        /// Stable server code, when present.
        code: Option<String>,
        /// Human-readable message.
        message: String,
    },
    /// The request could not be completed at the transport layer.
    Transport(String),
}

impl Error {
    /// Maps an HTTP status and message to a structured error.
    #[must_use]
    pub fn from_status(
        status: u16,
        message: impl Into<String>,
        code: Option<String>,
        retry_after: Option<u64>,
    ) -> Self {
        let message = message.into();
        match status {
            400 | 422 => Error::Validation(message),
            401 | 403 => Error::Permission(message),
            404 => Error::NotFound(message),
            409 => Error::Conflict(message),
            429 => Error::RateLimited {
                message,
                retry_after,
            },
            status => Error::Server {
                status,
                code,
                message,
            },
        }
    }

    /// The HTTP status associated with this error, when known.
    #[must_use]
    pub fn status(&self) -> Option<u16> {
        match self {
            Error::Server { status, .. } => Some(*status),
            Error::RateLimited { .. } => Some(429),
            Error::Validation(_) => Some(400),
            Error::Permission(_) => Some(403),
            Error::NotFound(_) => Some(404),
            Error::Conflict(_) => Some(409),
            Error::Transport(_) => None,
        }
    }

    /// Whether retrying the request could succeed.
    #[must_use]
    pub fn is_retryable(&self) -> bool {
        match self {
            Error::RateLimited { .. } | Error::Transport(_) => true,
            Error::Server { status, .. } => *status >= 500,
            _ => false,
        }
    }

    /// The stable server code, when the server supplied one.
    #[must_use]
    pub fn code(&self) -> Option<&str> {
        match self {
            Error::Server { code, .. } => code.as_deref(),
            _ => None,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Validation(message) => write!(formatter, "validation error: {message}"),
            Error::Permission(message) => write!(formatter, "permission denied: {message}"),
            Error::NotFound(message) => write!(formatter, "not found: {message}"),
            Error::Conflict(message) => write!(formatter, "conflict: {message}"),
            Error::RateLimited { message, .. } => write!(formatter, "rate limited: {message}"),
            Error::Server {
                status,
                code,
                message,
            } => match code {
                Some(code) => write!(formatter, "server error {status} ({code}): {message}"),
                None => write!(formatter, "server error {status}: {message}"),
            },
            Error::Transport(message) => write!(formatter, "transport error: {message}"),
        }
    }
}

impl std::error::Error for Error {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_statuses_to_variants() {
        assert!(matches!(
            Error::from_status(400, "bad", None, None),
            Error::Validation(_)
        ));
        assert!(matches!(
            Error::from_status(401, "nope", None, None),
            Error::Permission(_)
        ));
        assert!(matches!(
            Error::from_status(403, "nope", None, None),
            Error::Permission(_)
        ));
        assert!(matches!(
            Error::from_status(404, "missing", None, None),
            Error::NotFound(_)
        ));
        assert!(matches!(
            Error::from_status(409, "conflict", None, None),
            Error::Conflict(_)
        ));
        assert!(matches!(
            Error::from_status(429, "slow down", None, Some(3)),
            Error::RateLimited {
                retry_after: Some(3),
                ..
            }
        ));
        assert!(matches!(
            Error::from_status(503, "down", Some("server_error".into()), None),
            Error::Server { status: 503, .. }
        ));
    }

    #[test]
    fn retryability_is_narrow() {
        assert!(Error::from_status(429, "x", None, None).is_retryable());
        assert!(Error::from_status(500, "x", None, None).is_retryable());
        assert!(Error::Transport("x".into()).is_retryable());
        assert!(!Error::from_status(400, "x", None, None).is_retryable());
        assert!(!Error::from_status(404, "x", None, None).is_retryable());
    }
}
