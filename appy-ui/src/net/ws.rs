//! Auto-reconnecting WebSocket client for the Python backend.

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc::UnboundedReceiver;
use tokio_tungstenite::{connect_async, tungstenite::Message as WsMsg};

use crate::protocol::{BackendEvent, ClientEvent, UiEvent};

/// Override with APPY_WS_URL, e.g. to run a test copy next to the real one.
const WS_URL: &str = "ws://127.0.0.1:8001/ws";
const RECONNECT_DELAY: Duration = Duration::from_secs(2);

pub async fn run(
    ui_tx: async_channel::Sender<UiEvent>,
    mut outgoing: UnboundedReceiver<ClientEvent>,
) {
    let url = std::env::var("APPY_WS_URL").unwrap_or_else(|_| WS_URL.to_string());
    // A message that found the connection dead goes out first on the next one
    let mut unsent: Option<String> = None;
    loop {
        let Ok((stream, _)) = connect_async(url.as_str()).await else {
            tokio::time::sleep(RECONNECT_DELAY).await;
            continue;
        };
        let (mut write, mut read) = stream.split();
        if let Some(json) = unsent.take()
            && write.send(WsMsg::Text(json.clone().into())).await.is_err()
        {
            unsent = Some(json);
            tokio::time::sleep(RECONNECT_DELAY).await;
            continue;
        }
        loop {
            tokio::select! {
                msg = read.next() => match msg {
                    Some(Ok(WsMsg::Text(txt))) => {
                        match serde_json::from_str::<BackendEvent>(&txt) {
                            Ok(ev) => {
                                let _ = ui_tx.send(UiEvent::Backend(ev)).await;
                            }
                            Err(e) => eprintln!("[ws] ignoring unrecognised message ({e}): {txt}"),
                        }
                    }
                    Some(Ok(WsMsg::Close(_))) | Some(Err(_)) | None => break, // reconnect
                    Some(Ok(_)) => {} // pings are answered by tungstenite
                },
                ev = outgoing.recv() => {
                    let Some(ev) = ev else { return }; // the UI is gone
                    if let Ok(json) = serde_json::to_string(&ev)
                        && write.send(WsMsg::Text(json.clone().into())).await.is_err()
                    {
                        unsent = Some(json);
                        break;
                    }
                }
            }
        }
        tokio::time::sleep(RECONNECT_DELAY).await;
    }
}
