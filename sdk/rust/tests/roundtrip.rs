//! End-to-end request-building tests against the loopback test server.

use ai_code_assistant_sdk::{Client, Method, TestServer};

#[test]
fn builds_and_sends_a_signed_style_request() {
    let server = TestServer::start(201, r#"{"id":7,"name":"demo"}"#).unwrap();
    let client = Client::new(server.url())
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

    let response = server.send(&request).unwrap();
    assert!(response.starts_with("HTTP/1.1 201"));
    assert!(response.contains(r#"{"id":7,"name":"demo"}"#));

    server.wait_for_requests(1).unwrap();
    let received = server.received();
    assert_eq!(received.len(), 1);
    let raw = &received[0];
    assert!(raw.starts_with("POST /api/projects HTTP/1.1"));
    assert!(raw.contains("Authorization: Bearer secret-token"));
    assert!(raw.contains("Content-Type: application/json"));
    assert!(raw.contains(r#"{"name":"demo"}"#));
}

#[test]
fn multiple_methods_are_sent_with_the_right_verb() {
    let server = TestServer::start(200, "ok").unwrap();
    let client = Client::new(server.url()).unwrap();
    for method in [Method::Get, Method::Put, Method::Delete] {
        let request = client.request(method, "/api/health").build().unwrap();
        server.send(&request).unwrap();
    }
    server.wait_for_requests(3).unwrap();
    let received = server.received();
    assert!(received[0].starts_with("GET /api/health"));
    assert!(received[1].starts_with("PUT /api/health"));
    assert!(received[2].starts_with("DELETE /api/health"));
}
