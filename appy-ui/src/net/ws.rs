//! Auto-reconnecting WebSocket client for the Python backend.

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc::UnboundedReceiver;
use tokio_tungstenite::{connect_async, tungstenite::Message as WsMsg};

use crate::protocol::{BackendEvent, ClientEvent, UiEvent};

const WS_URL: &str = "ws://127.0.0.1:8001/ws";
const RECONNECT_DELAY: Duration = Duration::from_secs(2);

pub async fn run(
    ui_tx: async_channel::Sender<UiEvent>,
    mut outgoing: UnboundedReceiver<ClientEvent>,
) {
    loop {
        let Ok((stream, _)) = connect_async(WS_URL).await else {
            tokio::time::sleep(RECONNECT_DELAY).await;
            continue;
        };
        let (mut write, mut read) = stream.split();
        loop {
            tokio::select! {
                Some(Ok(msg)) = read.next() => {
                    if let WsMsg::Text(txt) = msg {
                        match serde_json::from_str::<BackendEvent>(&txt) {
                            Ok(ev) => {
                                let _ = ui_tx.send(UiEvent::Backend(ev)).await;
                            }
                            Err(e) => eprintln!("[ws] ignoring unrecognised message ({e}): {txt}"),
                        }
                    }
                }
                Some(ev) = outgoing.recv() => {
                    if let Ok(json) = serde_json::to_string(&ev)
                        && write.send(WsMsg::Text(json.into())).await.is_err() {
                            break;
                        }
                }
                else => break,
            }
        }
    }
}
