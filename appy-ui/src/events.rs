//! How each backend event changes the state and the chat log.

use std::rc::Rc;
use std::time::{Duration, Instant};

use gtk4::glib;
use gtk4::prelude::*;

use crate::app::App;
use crate::chat::format_live_dots;
use crate::protocol::{AppyState, BackendEvent, ClientEvent, Widget};
use crate::state::{ActiveStatus, Card, SharedState};
use crate::weather::CARD_SIZE;

/// Lets the card render scaled-down for a couple of frames so its CSS pop-in runs.
const CARD_POP_DELAY: Duration = Duration::from_millis(30);

/// Safety net: stop the talking bounce if the backend never sends the final "idle".
const MAX_SPEAKING: Duration = Duration::from_secs(30);

/// Rough on-screen talking time for a reply, used when the backend doesn't report TTS
/// playback itself (e.g. dev/mock_backend.py). The real backend sends speaking/idle.
fn estimated_speaking_time(text: &str) -> Duration {
    Duration::from_millis((1600 + text.len() as u64 * 35).min(5500))
}

impl App {
    pub fn on_backend(&self, ev: BackendEvent) {
        let now = Instant::now();
        let mut s = self.state.borrow_mut();

        // Activity keeps the chat open for a bit; a silent config sync shouldn't
        if !matches!(ev, BackendEvent::Effort { .. }) {
            s.trigger_voice_cooldown(now);
        }

        match ev {
            BackendEvent::Wake => {
                s.finish_active_status();
                s.state = AppyState::Listening;
            }
            BackendEvent::Transcript { text, is_final } => {
                s.state = AppyState::Listening;
                if is_final {
                    self.ui.input.buffer.set_text("");
                    self.ui.chat.push_bubble(&text, true);
                    s.message_count += 1;
                } else {
                    self.ui.input.buffer.set_text(&text);
                }
            }
            BackendEvent::Status { state, label } => self.apply_status(&mut s, state, &label, now),
            BackendEvent::Response { text, expression } => {
                s.finish_active_status();
                s.expression = expression;
                if s.state != AppyState::Speaking {
                    s.state = AppyState::Speaking;
                    s.talk_phase = 0.0;
                    s.speaking_until = Some(now + estimated_speaking_time(&text));
                }
                self.ui.chat.push_bubble(&text, false);
                s.message_count += 1;
            }
            BackendEvent::Effort { value } => {
                s.effort = value;
                self.ui.input.effort.show(value);
            }
            BackendEvent::Widget(Widget::Weather(report)) => {
                self.ui.card.set(&report);
                self.show_card(&mut s, now);
            }
            BackendEvent::MemoryProposals { items } => self.ui.memory.set_items(&items),
            BackendEvent::MemoryCommitted {
                kind,
                summary,
                facts,
                dropped,
                commit,
            } => {
                let tx = self.sender();
                let undo: Rc<dyn Fn(Vec<String>)> = Rc::new(move |commits| {
                    let _ = tx.send(ClientEvent::MemoryUndo { commits });
                });
                self.ui.chat.push_memory(kind, &summary, facts, dropped, &commit, undo);
            }
            BackendEvent::MemoryError { text } => self.ui.chat.push_notice(&text),
            BackendEvent::MemorySettings { auto_approve } => s.auto_approve = Some(auto_approve),
        }

        self.sync_pill(&s);
        self.ui.mascot.queue_draw();
    }

    /// Pop the weather card out of Appy (or keep the current one up longer).
    fn show_card(&self, s: &mut SharedState, now: Instant) {
        if let Some(card) = s.card.as_mut() {
            card.refresh(now);
        } else {
            // Start inside the mascot and fly out to its slot (see tick.rs)
            let body = self.body_on_screen(s);
            let (cx, cy) = body.center();
            let start = (cx - CARD_SIZE * 0.5, cy - CARD_SIZE * 0.5);
            s.card = Some(Card::new(start, now));
            self.ui.move_card(start);
            self.ui.card.appear();

            // A little recoil, as if Appy tossed it
            let toward_left = self.card_slot(s).0 < body.x;
            s.wobble.kick(if toward_left { -7.0 } else { 7.0 }, 5.0);
        }

        let card = self.ui.card.root.clone();
        glib::timeout_add_local_once(CARD_POP_DELAY, move || card.add_css_class("shown"));
        s.last_input_region = None;
    }

    fn apply_status(&self, s: &mut SharedState, next: AppyState, label: &str, now: Instant) {
        if next.is_busy() {
            // Repeated "thinking" keeps the existing pill (and its timer) running
            let same_pill = s.active_status.as_ref().is_some_and(|a| a.state == next);
            if !same_pill {
                s.finish_active_status();
                let word = next.live_status_text().unwrap_or(label);
                let pill = self.ui.chat.push_status(&format_live_dots(word, 0));
                s.active_status = Some(ActiveStatus {
                    state: next,
                    label: pill,
                    started: now,
                });
            }
        } else {
            s.finish_active_status();
        }

        match next {
            AppyState::Speaking => {
                if s.state != AppyState::Speaking {
                    s.talk_phase = 0.0;
                }
                s.speaking_until = Some(now + MAX_SPEAKING);
            }
            AppyState::Idle => {
                s.speaking_until = None;
                s.talk_phase = 0.0;
            }
            _ => {}
        }
        s.state = next;
    }
}
