use std::time::{Duration, Instant};

const PILL_TIMEOUT: Duration = Duration::from_secs(8);

struct AppyOverlay {
    visible: bool,
    hovered: bool,
    input_focused: bool,
    last_activity: Option<Instant>,
    state: AppyState,
    phase: f32,
    user_input: String,
    appy_reply: String,
    status_label: String,
}

impl AppyOverlay {
    fn show_pills(&self) -> bool {
        if self.hovered || self.input_focused {
            return true;
        }
        if matches!(self.state, AppyState::Booting | AppyState::Thinking | AppyState::Compacting) {
            return true;
        }
        self.last_activity
            .map(|t| t.elapsed() < PILL_TIMEOUT)
            .unwrap_or(false)
    }

    fn bump_activity(&mut self) {
        self.last_activity = Some(Instant::now());
    }
}