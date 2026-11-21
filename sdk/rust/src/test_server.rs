//! A deterministic loopback HTTP server for SDK tests.
//!
//! The server records the raw request it receives and replies with a fixed
//! status and body. It only listens on `127.0.0.1` and is intended for tests.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::errors::{Error, Result};
use crate::request::PreparedRequest;

/// A tiny loopback HTTP server used by tests.
pub struct TestServer {
    addr: SocketAddr,
    received: Arc<Mutex<Vec<String>>>,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl TestServer {
    /// Starts a server that replies with `status` and `body` to every request.
    pub fn start(status: u16, body: impl Into<String>) -> Result<Self> {
        let listener = TcpListener::bind("127.0.0.1:0")
            .map_err(|error| Error::Transport(format!("cannot bind test server: {error}")))?;
        listener
            .set_nonblocking(true)
            .map_err(|error| Error::Transport(format!("cannot configure test server: {error}")))?;
        let addr = listener.local_addr().map_err(|error| {
            Error::Transport(format!("cannot read test server address: {error}"))
        })?;
        let body = body.into();
        let received = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let thread_received = Arc::clone(&received);
        let thread_stop = Arc::clone(&stop);
        let handle = thread::spawn(move || {
            while !thread_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
                        let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
                        if let Ok(raw) = read_request(&mut stream) {
                            thread_received
                                .lock()
                                .expect("test server record lock")
                                .push(raw);
                            let response = format!(
                                "HTTP/1.1 {status} Test\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                                body.len()
                            );
                            let _ = stream.write_all(response.as_bytes());
                            let _ = stream.flush();
                        }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(TestServer {
            addr,
            received,
            stop,
            handle: Some(handle),
        })
    }

    /// The base URL, for example `http://127.0.0.1:54321`.
    #[must_use]
    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    /// Raw requests received so far, oldest first.
    #[must_use]
    pub fn received(&self) -> Vec<String> {
        self.received
            .lock()
            .expect("test server record lock")
            .clone()
    }

    /// Waits until at least `count` requests have been received.
    pub fn wait_for_requests(&self, count: usize) -> Result<()> {
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if self.received().len() >= count {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(5));
        }
        Err(Error::Transport(format!(
            "timed out waiting for {count} request(s); got {}",
            self.received().len()
        )))
    }

    /// Sends a prepared request and returns the raw response text.
    pub fn send(&self, request: &PreparedRequest) -> Result<String> {
        let (authority, path) = split_url(&request.url)?;
        let mut stream = TcpStream::connect(self.addr)
            .map_err(|error| Error::Transport(format!("cannot connect to test server: {error}")))?;
        let mut raw = format!(
            "{} {} HTTP/1.1\r\nHost: {}\r\n",
            request.method.as_str(),
            path,
            authority
        );
        for (name, value) in &request.headers {
            raw.push_str(&format!("{name}: {value}\r\n"));
        }
        if let Some(body) = &request.body {
            raw.push_str(&format!("Content-Length: {}\r\n", body.len()));
        }
        raw.push_str("Connection: close\r\n\r\n");
        stream
            .write_all(raw.as_bytes())
            .map_err(|error| Error::Transport(format!("cannot write request: {error}")))?;
        if let Some(body) = &request.body {
            stream
                .write_all(body)
                .map_err(|error| Error::Transport(format!("cannot write body: {error}")))?;
        }
        stream
            .flush()
            .map_err(|error| Error::Transport(format!("cannot flush request: {error}")))?;
        let mut response = String::new();
        stream
            .read_to_string(&mut response)
            .map_err(|error| Error::Transport(format!("cannot read response: {error}")))?;
        Ok(response)
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(self.addr);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

fn split_url(url: &str) -> Result<(String, String)> {
    let (rest, path) = url
        .strip_prefix("http://")
        .map(|rest| ("http", rest))
        .or_else(|| url.strip_prefix("https://").map(|rest| ("https", rest)))
        .map(|(scheme, rest)| {
            let (authority, path) = match rest.split_once('/') {
                Some((authority, path)) => (authority, format!("/{path}")),
                None => (rest, "/".to_string()),
            };
            (format!("{scheme}://{authority}"), path)
        })
        .ok_or_else(|| Error::Validation(format!("unsupported url: {url}")))?;
    Ok((rest, path))
}

fn read_request(stream: &mut TcpStream) -> std::io::Result<String> {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 1024];
    loop {
        let read = stream.read(&mut chunk)?;
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(position) = find_header_end(&buffer) {
            let header_text = String::from_utf8_lossy(&buffer[..position]).to_string();
            let content_length = header_text
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    if name.eq_ignore_ascii_case("content-length") {
                        value.trim().parse::<usize>().ok()
                    } else {
                        None
                    }
                })
                .unwrap_or(0);
            let body_start = position + 4;
            while buffer.len() < body_start + content_length {
                let read = stream.read(&mut chunk)?;
                if read == 0 {
                    break;
                }
                buffer.extend_from_slice(&chunk[..read]);
            }
            return Ok(String::from_utf8_lossy(&buffer).to_string());
        }
    }
    Ok(String::from_utf8_lossy(&buffer).to_string())
}

fn find_header_end(buffer: &[u8]) -> Option<usize> {
    buffer
        .windows(4)
        .position(|window| window == b"\r\n\r\n".as_slice())
}
