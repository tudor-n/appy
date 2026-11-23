use futures_util::{SinkExt, Stream, StreamExt};
use iced::futures::channel::mpsc as iced_mpsc;
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio_tungstenite::{connect_async, tungstenite::Message as WsMsg};

use crate::{BackendEvent, WS_URL};

#[derive(Debug, Clone)]
pub enum WsEvent {
    Ready(UnboundedSender<ClientEvent>),
    Connected,
    Disconnected,
    Backend(BackendEvent),
}

#[derive(Debug, serde::Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientEvent {
    UserText { text: String },
}

pub fn subscription() -> impl Stream<Item = WsEvent> {
    iced::stream::channel(64, |mut output: iced_mpsc::Sender<WsEvent>| async move {
        let (tx, mut outgoing_rx) = mpsc::unbounded_channel();
        let _ = output.send(WsEvent::Ready(tx)).await;

        loop {
            match connect_async(WS_URL).await {
                Ok((ws_stream, _)) => {
                    let _ = output.send(WsEvent::Connected).await;
                    let (mut write, mut read) = ws_stream.split();

                    loop {
                        tokio::select! {
                            Some(Ok(msg)) = read.next() => {
                                if let WsMsg::Text(txt) = msg {
                                    if let Ok(ev) = serde_json::from_str::<BackendEvent>(&txt) {
                                        let _ = output.send(WsEvent::Backend(ev)).await;
                                    }
                                }
                            }
                            Some(client_ev) = outgoing_rx.recv() => {
                                if let Ok(json) = serde_json::to_string(&client_ev) {
                                    if write.send(WsMsg::Text(json.into())).await.is_err() {
                                        break;
                                    }
                                }
                            }
                            else => break,
                        }
                    }

                    let _ = output.send(WsEvent::Disconnected).await;
                }
                Err(_) => {
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                }
            }
        }
    })
}