use ksni::{Tray, TrayMethods};
use tokio::sync::mpsc::UnboundedSender;

#[derive(Debug, Clone)]
pub enum TrayEvent {
    ToggleOverlay,
    Quit,
}

struct AppyTray {
    tx: UnboundedSender<TrayEvent>,
    icon_argb: Vec<u8>, // 32x32 ARGB32 bytes of Appy's squircle icon
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

    // Left-click on the tray mascot pops Appy out / hides him
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

pub fn spawn_tray(tx: UnboundedSender<TrayEvent>, icon_argb: Vec<u8>) {
    tokio::spawn(async move {
        let tray = AppyTray { tx, icon_argb };
        let _handle = tray.spawn().await.expect("failed to register KDE tray item");
        std::future::pending::<()>().await;
    });
}