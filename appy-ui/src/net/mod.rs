//! Background networking on its own Tokio runtime thread. Results reach GTK through
//! `UiEvent`s on an async channel; outgoing messages arrive on `ws_out_rx`.

mod pill_bridge;
mod ws;

use std::sync::{Arc, Mutex};

use tokio::sync::mpsc::UnboundedReceiver;

use crate::protocol::{ClientEvent, PillSyncState, UiEvent};

pub fn spawn(
    ui_tx: async_channel::Sender<UiEvent>,
    ws_out_rx: UnboundedReceiver<ClientEvent>,
    pill_sync: Arc<Mutex<PillSyncState>>,
) {
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("failed to start tokio runtime");
        rt.block_on(async move {
            tokio::spawn(pill_bridge::serve(ui_tx.clone(), pill_sync));
            ws::run(ui_tx, ws_out_rx).await;
        });
    });
}
