//! Tiny HTTP endpoint on 127.0.0.1:8002 for the KDE Plasma panel pill (appy-pill/).
//!
//! `GET /state` returns a `PillSyncState` snapshot (polled every 200ms by the pill);
//! `POST /toggle`, `POST /quit` and `POST /auto-approve` control the overlay. Localhost only, one request per
//! connection, so a hand-rolled responder is enough.
//!
//! The POST routes need an `X-Appy-Pill: 1` header. A web page can't send one to us: a
//! cross-origin request with a custom header needs a CORS preflight, which this bridge never
//! approves. Without it any page could switch auto-approve on.

use std::sync::{Arc, Mutex};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use crate::protocol::{PillSyncState, UiEvent};

/// Override with APPY_PILL_ADDR, e.g. to run a test copy next to the real one.
const ADDR: &str = "127.0.0.1:8002";

#[derive(Debug, PartialEq, Eq)]
enum Route {
    Toggle,
    Quit,
    AutoApprove,
    State,
    Refused,
}

const PILL_HEADER: &str = "x-appy-pill";

fn from_pill(request: &str) -> bool {
    request.lines().skip(1).take_while(|l| !l.is_empty()).any(|l| {
        l.split_once(':').is_some_and(|(name, value)| {
            name.trim().eq_ignore_ascii_case(PILL_HEADER) && value.trim() == "1"
        })
    })
}

fn route(request: &str) -> Route {
    if request.starts_with("POST ") && !from_pill(request) {
        Route::Refused
    } else if request.starts_with("POST /toggle") {
        Route::Toggle
    } else if request.starts_with("POST /quit") {
        Route::Quit
    } else if request.starts_with("POST /auto-approve") {
        Route::AutoApprove
    } else {
        Route::State
    }
}

fn http_ok_json(body: &str) -> String {
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    )
}

pub async fn serve(ui_tx: async_channel::Sender<UiEvent>, pill_sync: Arc<Mutex<PillSyncState>>) {
    let addr = std::env::var("APPY_PILL_ADDR").unwrap_or_else(|_| ADDR.to_string());
    let listener = match TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[pill] couldn't bind {addr} ({e}); the panel pill won't be able to connect");
            return;
        }
    };
    while let Ok((stream, _)) = listener.accept().await {
        tokio::spawn(handle(stream, ui_tx.clone(), pill_sync.clone()));
    }
}

async fn handle(
    mut stream: TcpStream,
    ui_tx: async_channel::Sender<UiEvent>,
    pill_sync: Arc<Mutex<PillSyncState>>,
) {
    let mut buf = [0u8; 2048];
    let n = stream.read(&mut buf).await.unwrap_or(0);
    let request = String::from_utf8_lossy(&buf[..n]);

    let body = match route(&request) {
        Route::Toggle => {
            let _ = ui_tx.send(UiEvent::ToggleOverlay).await;
            r#"{"ok":true}"#.to_string()
        }
        Route::Quit => {
            let _ = ui_tx.send(UiEvent::Quit).await;
            r#"{"ok":true}"#.to_string()
        }
        Route::AutoApprove => {
            let _ = ui_tx.send(UiEvent::ToggleAutoApprove).await;
            r#"{"ok":true}"#.to_string()
        }
        Route::Refused => r#"{"ok":false}"#.to_string(),
        Route::State => {
            let snapshot = pill_sync.lock().map(|g| g.clone()).unwrap_or_default();
            serde_json::to_string(&snapshot).unwrap_or_else(|_| "{}".into())
        }
    };
    let _ = stream.write_all(http_ok_json(&body).as_bytes()).await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_requests() {
        let pill = "\r\nHost: 127.0.0.1:8002\r\nX-Appy-Pill: 1\r\n\r\n";
        assert_eq!(route(&format!("POST /toggle HTTP/1.1{pill}")), Route::Toggle);
        assert_eq!(route(&format!("POST /quit HTTP/1.1{pill}")), Route::Quit);
        assert_eq!(route("GET /state HTTP/1.1\r\n"), Route::State);
        assert_eq!(route(&format!("POST /auto-approve HTTP/1.1{pill}")), Route::AutoApprove);
        assert_eq!(route("POST /auto-approve HTTP/1.1\r\nx-appy-pill:1\r\n\r\n"), Route::AutoApprove);
        assert_eq!(route(""), Route::State);
    }

    #[test]
    fn posts_without_the_pill_header_are_refused() {
        // e.g. fetch("http://127.0.0.1:8002/auto-approve", {method: "POST", mode: "no-cors"})
        let page = "POST /auto-approve HTTP/1.1\r\nHost: 127.0.0.1:8002\r\nOrigin: https://example.com\r\n\r\n";
        assert_eq!(route(page), Route::Refused);
        assert_eq!(route("POST /toggle HTTP/1.1\r\n\r\n"), Route::Refused);
        assert_eq!(route("POST /quit HTTP/1.1\r\nX-Appy-Pill: 0\r\n\r\n"), Route::Refused);
        // only a header counts, not the same text in the body
        assert_eq!(route("POST /quit HTTP/1.1\r\n\r\nX-Appy-Pill: 1"), Route::Refused);
    }

    #[test]
    fn response_has_correct_length() {
        let resp = http_ok_json(r#"{"ok":true}"#);
        assert!(resp.contains("Content-Length: 11\r\n"));
        assert!(resp.ends_with("\r\n\r\n{\"ok\":true}"));
    }
}
