//! Request building and client configuration.
//!
//! The skeleton validates configuration and produces a [`PreparedRequest`]; it
//! does not perform network I/O so a caller can plug in any HTTP transport
//! (including one with TLS) without coupling the SDK to a runtime.

use std::collections::BTreeMap;

use crate::errors::{Error, Result};

/// An HTTP method supported by the SDK.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    /// `GET`
    Get,
    /// `POST`
    Post,
    /// `PUT`
    Put,
    /// `PATCH`
    Patch,
    /// `DELETE`
    Delete,
}

impl Method {
    /// The wire representation.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Method::Get => "GET",
            Method::Post => "POST",
            Method::Put => "PUT",
            Method::Patch => "PATCH",
            Method::Delete => "DELETE",
        }
    }

    /// Whether the method is safe to retry automatically.
    #[must_use]
    pub fn is_idempotent(self) -> bool {
        matches!(self, Method::Get | Method::Put | Method::Delete)
    }
}

/// A fully built request, ready for a transport.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreparedRequest {
    /// HTTP method.
    pub method: Method,
    /// Absolute request URL.
    pub url: String,
    /// Headers, keyed by name.
    pub headers: BTreeMap<String, String>,
    /// Optional request body.
    pub body: Option<Vec<u8>>,
}

impl PreparedRequest {
    /// Reads a header by case-insensitive name.
    #[must_use]
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// The body decoded as UTF-8, when present and valid.
    #[must_use]
    pub fn body_text(&self) -> Option<&str> {
        self.body
            .as_deref()
            .and_then(|body| std::str::from_utf8(body).ok())
    }
}

/// A small client that validates configuration and builds requests.
#[derive(Debug, Clone)]
pub struct Client {
    base_url: String,
    api_token: Option<String>,
}

impl Client {
    /// Creates a client for an absolute `http(s)` base URL.
    pub fn new(base_url: impl Into<String>) -> Result<Self> {
        let base_url = base_url.into();
        let trimmed = base_url.trim();
        if trimmed.is_empty() {
            return Err(Error::Validation("base_url must not be empty".into()));
        }
        if !(trimmed.starts_with("http://") || trimmed.starts_with("https://")) {
            return Err(Error::Validation(
                "base_url must be an absolute http(s) URL".into(),
            ));
        }
        if trimmed.chars().any(|c| c.is_whitespace() || c.is_control()) {
            return Err(Error::Validation(
                "base_url must not contain whitespace".into(),
            ));
        }
        Ok(Client {
            base_url: trimmed.trim_end_matches('/').to_string(),
            api_token: None,
        })
    }

    /// Sets the bearer token sent with every request.
    pub fn with_token(mut self, token: impl Into<String>) -> Result<Self> {
        let token = token.into();
        if token.trim().is_empty() {
            return Err(Error::Validation("api token must not be empty".into()));
        }
        if token.chars().any(|c| c == '\r' || c == '\n') {
            return Err(Error::Validation(
                "api token must not contain newlines".into(),
            ));
        }
        self.api_token = Some(token);
        Ok(self)
    }

    /// The configured base URL.
    #[must_use]
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// Starts building a request for `path`.
    #[must_use]
    pub fn request(&self, method: Method, path: &str) -> RequestBuilder<'_> {
        RequestBuilder {
            client: self,
            method,
            path: path.to_string(),
            headers: BTreeMap::new(),
            body: None,
        }
    }

    /// Convenience for `GET`.
    #[must_use]
    pub fn get(&self, path: &str) -> RequestBuilder<'_> {
        self.request(Method::Get, path)
    }

    /// Convenience for `POST`.
    #[must_use]
    pub fn post(&self, path: &str) -> RequestBuilder<'_> {
        self.request(Method::Post, path)
    }
}

/// Builds a [`PreparedRequest`] with validation.
pub struct RequestBuilder<'a> {
    client: &'a Client,
    method: Method,
    path: String,
    headers: BTreeMap<String, String>,
    body: Option<Vec<u8>>,
}

impl RequestBuilder<'_> {
    /// Adds a header after validating the name and value.
    pub fn header(mut self, name: &str, value: &str) -> Result<Self> {
        if name.is_empty()
            || name
                .chars()
                .any(|c| c.is_whitespace() || c == ':' || c.is_control())
        {
            return Err(Error::Validation(format!("invalid header name: {name:?}")));
        }
        if value.chars().any(|c| c == '\r' || c == '\n') {
            return Err(Error::Validation(format!(
                "invalid header value for {name}"
            )));
        }
        self.headers.insert(name.to_string(), value.to_string());
        Ok(self)
    }

    /// Sets a raw body.
    #[must_use]
    pub fn body(mut self, body: impl Into<Vec<u8>>) -> Self {
        self.body = Some(body.into());
        self
    }

    /// Sets a JSON body and the matching content type.
    pub fn json_body(mut self, json: impl Into<String>) -> Result<Self> {
        let json = json.into();
        if json.trim().is_empty() {
            return Err(Error::Validation("json body must not be empty".into()));
        }
        self.body = Some(json.into_bytes());
        self.headers
            .insert("Content-Type".to_string(), "application/json".to_string());
        Ok(self)
    }

    /// Validates and builds the request.
    pub fn build(self) -> Result<PreparedRequest> {
        if !self.path.starts_with('/') {
            return Err(Error::Validation(
                "path must start with `/`; set the host on the client".into(),
            ));
        }
        if self
            .path
            .chars()
            .any(|c| c.is_whitespace() || c.is_control())
        {
            return Err(Error::Validation("path must not contain whitespace".into()));
        }
        if self.path.starts_with("//") {
            return Err(Error::Validation(
                "path must not be a protocol-relative URL".into(),
            ));
        }
        let mut headers = self.headers;
        headers
            .entry("Accept".to_string())
            .or_insert_with(|| "application/json".to_string());
        if let Some(token) = &self.client.api_token {
            headers.insert("Authorization".to_string(), format!("Bearer {token}"));
        }
        let url = format!("{}{}", self.client.base_url, self.path);
        Ok(PreparedRequest {
            method: self.method,
            url,
            headers,
            body: self.body,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_a_request_with_auth_and_json() {
        let client = Client::new("https://api.example.com/")
            .unwrap()
            .with_token("secret-token")
            .unwrap();
        let request = client
            .post("/api/projects")
            .json_body(r#"{"name":"demo"}"#)
            .unwrap()
            .header("X-Trace", "abc")
            .unwrap()
            .build()
            .unwrap();
        assert_eq!(request.method, Method::Post);
        assert_eq!(request.url, "https://api.example.com/api/projects");
        assert_eq!(request.header("authorization"), Some("Bearer secret-token"));
        assert_eq!(request.header("content-type"), Some("application/json"));
        assert_eq!(request.body_text(), Some(r#"{"name":"demo"}"#));
    }

    #[test]
    fn rejects_invalid_configuration_and_paths() {
        assert!(Client::new("api.example.com").is_err());
        assert!(Client::new("").is_err());
        let client = Client::new("https://api.example.com").unwrap();
        assert!(client.get("api/projects").build().is_err());
        assert!(client.get("//evil.example.com").build().is_err());
        assert!(client.get("/ok").header("Bad Name", "x").is_err());
        assert!(client.get("/ok").header("X-Test", "a\r\nb").is_err());
        assert!(client.get("/ok").json_body("  ").is_err());
        assert!(client.with_token("  ").is_err());
    }

    #[test]
    fn idempotency_is_documented() {
        assert!(Method::Get.is_idempotent());
        assert!(Method::Put.is_idempotent());
        assert!(Method::Delete.is_idempotent());
        assert!(!Method::Post.is_idempotent());
        assert!(!Method::Patch.is_idempotent());
    }
}
