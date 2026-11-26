//! `App` ties the widgets, the mutable state and the network channels together.
//!
//! Borrowing rule for `state`: never call GTK functions that can synchronously re-enter
//! one of our handlers while holding a `borrow_mut()`. In practice that means showing
//! the window, which emits `map`, must happen *before* borrowing (see `toggle_overlay`).

use std::cell::RefCell;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use gtk4::prelude::*;
use gtk4::{Application, glib};
use tokio::sync::mpsc::{self, UnboundedSender};

use crate::chat::format_live_dots;
use crate::mascot::{self, Pose};
use crate::net;
use crate::protocol::{AppyState, ClientEvent, Effort, PillSyncState, UiEvent};
use crate::state::{ActiveStatus, SharedState};
use crate::ui::Ui;
use crate::{controls, tick};

pub struct App {
    gtk_app: Application,
    pub ui: Ui,
    pub state: RefCell<SharedState>,
    pill_sync: Arc<Mutex<PillSyncState>>,
    ws_tx: UnboundedSender<ClientEvent>,
}

impl App {
    /// Builds the overlay, starts networking and the frame loop. The window starts
    /// hidden; the panel pill (or `POST /toggle`) shows it.
    ///
    /// The App lives for the whole process, so it is leaked once: every signal handler
    /// can then capture a plain `&'static App` instead of cloning a dozen widgets.
    pub fn build(gtk_app: &Application) -> &'static App {
        let pill_sync = Arc::new(Mutex::new(PillSyncState::default()));
        let (ui_tx, ui_rx) = async_channel::unbounded::<UiEvent>();
        let (ws_tx, ws_rx) = mpsc::unbounded_channel::<ClientEvent>();
        net::spawn(ui_tx, ws_rx, pill_sync.clone());

        let app: &'static App = Box::leak(Box::new(App {
            gtk_app: gtk_app.clone(),
            ui: Ui::build(gtk_app),
            state: RefCell::new(SharedState::new(Instant::now())),
            pill_sync,
            ws_tx,
        }));

        let tx = app.ws_tx.clone();
        app.ui.memory.set_on_send(move |out| {
            let _ = tx.send(ClientEvent::MemoryDecision {
                slug: out.slug,
                decision: out.decision,
                text: out.text,
                before: out.before,
            });
        });
        app.connect_draw();
        controls::connect(app);
        tick::start(app);
        glib::spawn_future_local(async move {
            while let Ok(ev) = ui_rx.recv().await {
                app.handle(ev);
            }
        });

        app.pop_out_booting();
        app
    }

    /// Startup: show the mascot right away with the booting loading bar and a "loading"
    /// chat status, and a little hop. Nothing is connected yet; the backend's replayed
    /// status continues or ends this (`apply_status` keeps a status of the same state).
    fn pop_out_booting(&self) {
        self.ui.window.set_visible(true);
        let mut s = self.state.borrow_mut();
        let word = AppyState::Booting.live_status_text().unwrap_or("loading");
        let label = self.ui.chat.push_status(&format_live_dots(word, 0));
        s.active_status = Some(ActiveStatus {
            state: AppyState::Booting,
            label,
            started: Instant::now(),
        });
        s.wobble.kick(0.0, -9.0);
        s.last_input_region = None;
        self.sync_pill(&s);
    }

    fn connect_draw(&'static self) {
        self.ui.mascot.set_draw_func(move |_, cr, width, height| {
            let s = self.state.borrow();
            // Glass mode wobbles the whole shell instead, so damp the mascot's own wobble
            let wobble_mul = if s.locked { 0.3 } else { 1.0 };
            let pose = Pose {
                state: s.state,
                expression: s.expression,
                phase: s.phase,
                talk_phase: s.talk_phase,
                busy_secs: s
                    .active_status
                    .as_ref()
                    .filter(|a| a.state == s.state)
                    .map_or(0.0, |a| a.started.elapsed().as_secs_f64()),
                wobble_x: s.wobble.x * wobble_mul,
                wobble_y: s.wobble.y * wobble_mul,
                blink_scale: s.blink.scale,
            };
            mascot::draw(cr, width as f64, height as f64, &pose);
        });
    }

    fn handle(&self, ev: UiEvent) {
        match ev {
            UiEvent::ToggleOverlay => self.toggle_overlay(),
            UiEvent::Quit => self.gtk_app.quit(),
            UiEvent::Backend(ev) => self.on_backend(ev),
        }
    }

    /// Update the state now, or on the next idle if it's already borrowed further up
    /// the stack. GTK emits some signals synchronously from inside other calls (hiding
    /// a widget under the pointer emits `leave` right away), so pointer handlers must
    /// never assume the state is free.
    pub fn with_state(&'static self, f: impl FnOnce(&mut SharedState) + 'static) {
        match self.state.try_borrow_mut() {
            Ok(mut s) => f(&mut s),
            Err(_) => {
                glib::idle_add_local_once(move || self.with_state(f));
            }
        }
    }

    /// Publish the current state for the panel pill's `GET /state`.
    pub fn sync_pill(&self, s: &SharedState) {
        if let Ok(mut p) = self.pill_sync.lock() {
            *p = PillSyncState {
                state: s.state,
                expression: s.expression,
                msg_count: s.message_count,
                minimized: !self.ui.window.is_visible(),
            };
        }
    }

    pub fn toggle_overlay(&self) {
        let show = !self.ui.window.is_visible();
        self.ui.window.set_visible(show); // before borrowing: showing emits `map`
        let mut s = self.state.borrow_mut();
        s.last_input_region = None;
        self.sync_pill(&s);
    }

    pub fn minimize(&self) {
        self.ui.window.set_visible(false);
        self.sync_pill(&self.state.borrow());
    }

    /// Send the typed message to the backend.
    pub fn submit(&self) {
        let Some(text) = self.ui.input.text() else {
            return;
        };
        self.ui.chat.push_bubble(&text, true);
        let _ = self.ws_tx.send(ClientEvent::UserText { text });
        self.ui.input.buffer.set_text("");

        let mut s = self.state.borrow_mut();
        s.finish_active_status();
        let label = self.ui.chat.push_status(&format_live_dots("thinking", 0));
        s.active_status = Some(ActiveStatus {
            state: AppyState::Thinking,
            label,
            started: Instant::now(),
        });
        s.message_count += 1;
        s.state = AppyState::Thinking;
        self.sync_pill(&s);
        self.ui.mascot.queue_draw();
    }

    /// A handle for sending to the backend from widget callbacks.
    pub fn sender(&self) -> UnboundedSender<ClientEvent> {
        self.ws_tx.clone()
    }

    /// Ask the backend to compact the conversation now. The UI follows the backend's
    /// "compacting" status, so there's nothing to update here.
    pub fn compact(&self) {
        let _ = self.ws_tx.send(ClientEvent::Compact);
    }

    pub fn set_effort(&self, effort: Effort) {
        self.state.borrow_mut().effort = effort;
        self.ui.input.effort.show(effort);
        let _ = self.ws_tx.send(ClientEvent::SetEffort { effort });
        self.ui.input.effort.collapse();
    }
}
