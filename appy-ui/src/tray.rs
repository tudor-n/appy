use futures_util::{SinkExt, Stream};
use iced::futures::channel::mpsc as iced_mpsc;
use ksni::{Tray, TrayMethods};
use tokio::sync::mpsc::{self, UnboundedSender};

#[derive(Debug, Clone)]
pub enum TrayEvent {
    ToggleOverlay,
    Quit,
}

struct AppyTray {
    tx: UnboundedSender<TrayEvent>,
    icon_argb: Vec<u8>,
}

impl Tray for AppyTray {
    fn id(&self) -> String {
        "appy-lab-os".into()
    }

    fn title(&self) -> String {
        "Appy".into()
    }

    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        vec![ksni::Icon {
            width: 32,
            height: 32,
            data: self.icon_argb.clone(),
        }]
    }

    fn activate(&mut self, _x: i32, _y: i32) {
        let _ = self.tx.send(TrayEvent::ToggleOverlay);
    }

    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        use ksni::menu::*;
        vec![
            StandardItem {
                label: "Summon / Hide Appy".into(),
                activate: Box::new(|this: &mut Self| {
                    let _ = this.tx.send(TrayEvent::ToggleOverlay);
                }),
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: "Quit".into(),
                activate: Box::new(|this: &mut Self| {
                    let _ = this.tx.send(TrayEvent::Quit);
                }),
                ..Default::default()
            }
            .into(),
        ]
    }
}

/// Generates a minimal 32x32 ARGB32 squircle icon for the KDE tray
fn build_tray_icon_argb() -> Vec<u8> {
    let mut data = vec![0u8; 32 * 32 * 4];
    for y in 0..32 {
        for x in 0..32 {
            let idx = (y * 32 + x) * 4;
            let in_body = (4..28).contains(&x) && (7..29).contains(&y);
            let is_eye = ((10..13).contains(&x) || (19..22).contains(&x)) && (15..22).contains(&y);
            let is_leaf = (16..22).contains(&x) && (2..7).contains(&y);

            if is_eye {
                data[idx..idx + 4].copy_from_slice(&[255, 20, 20, 24]);
            } else if in_body {
                data[idx..idx + 4].copy_from_slice(&[240, 218, 216, 208]);
            } else if is_leaf {
                data[idx..idx + 4].copy_from_slice(&[250, 251, 242, 242]);
            }
        }
    }
    data
}

pub fn subscription() -> impl Stream<Item = TrayEvent> {
    iced::stream::channel(16, |mut output: iced_mpsc::Sender<TrayEvent>| async move {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let tray = AppyTray {
            tx,
            icon_argb: build_tray_icon_argb(),
        };

        let _handle = tray.spawn().await.expect("failed to register KDE tray item");

        while let Some(ev) = rx.recv().await {
            let _ = output.send(ev).await;
        }
    })
}