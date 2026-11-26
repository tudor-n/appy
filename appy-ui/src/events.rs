//! How each backend event changes the state and the chat log.

use std::time::{Duration, Instant};

use gtk4::prelude::*;

use crate::app::App;
use crate::chat::format_live_dots;
use crate::protocol::{AppyState, BackendEvent};
use crate::state::{ActiveStatus, SharedState};

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
        }

        self.sync_pill(&s);
        self.ui.mascot.queue_draw();
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
