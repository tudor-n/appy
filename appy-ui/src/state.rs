//! Mutable UI state, grouped by concern. Everything except `ActiveStatus` is plain data
//! with time passed in explicitly, so the logic is unit-testable without GTK.

use std::f64::consts::PI;
use std::time::{Duration, Instant};

use gtk4::Label;
use gtk4::prelude::*;

use crate::layout::{Placement, Point};
use crate::protocol::{AppyState, Effort, Expression};

/// How long the chat stays visible after backend activity, so you can read the reply.
const VOICE_COOLDOWN: Duration = Duration::from_secs(8);
/// How long the chat stays open after the pointer leaves the mascot, so you can
/// cross the gap to the messages without them collapsing.
const HOVER_GRACE: Duration = Duration::from_millis(300);
const BLINK_DURATION_MS: f64 = 160.0;
/// How long the weather card stays up (unless hovered).
const CARD_LIFETIME: Duration = Duration::from_secs(14);
/// How long the card's exit animation runs before it's hidden.
pub const CARD_EXIT: Duration = Duration::from_millis(320);

pub struct SharedState {
    pub state: AppyState,
    pub expression: Expression,
    pub effort: Effort,
    /// Phase of the busy dots animation.
    pub phase: f64,
    /// Phase of the talking bounce.
    pub talk_phase: f64,
    pub speaking_until: Option<Instant>,
    /// Glass mode: chat pinned open with a dark backdrop (double right-click).
    pub locked: bool,
    pub voice_cooldown_until: Option<Instant>,
    pub chat_open: bool,
    pub message_count: usize,
    pub last_input_region: Option<Vec<(i32, i32, i32, i32)>>,
    pub active_status: Option<ActiveStatus>,
    /// Whether the wobble CSS provider currently holds a non-identity transform.
    pub wobble_css_active: bool,
    pub hover: Hover,
    pub drag: Drag,
    pub placement: Placement,
    pub wobble: Wobble,
    pub blink: Blink,
    /// The weather card, while it's on screen.
    pub card: Option<Card>,
}

impl SharedState {
    pub fn new(now: Instant) -> Self {
        Self {
            // The overlay comes up before the backend, which replays "booting" (continuing
            // this) or "idle" (ending it) once it's connected
            state: AppyState::Booting,
            expression: Expression::Neutral,
            effort: Effort::Off,
            phase: 0.0,
            talk_phase: 0.0,
            speaking_until: None,
            locked: false,
            voice_cooldown_until: None,
            chat_open: false,
            message_count: 0,
            last_input_region: None,
            active_status: None,
            wobble_css_active: false,
            hover: Hover::default(),
            drag: Drag::default(),
            placement: Placement::default(),
            wobble: Wobble::default(),
            blink: Blink::new(now),
            card: None,
        }
    }

    pub fn is_busy(&self) -> bool {
        self.state.is_busy()
    }

    pub fn trigger_voice_cooldown(&mut self, now: Instant) {
        self.voice_cooldown_until = Some(now + VOICE_COOLDOWN);
    }

    pub fn set_mascot_hover(&mut self, hovered: bool, now: Instant) {
        if self.hover.mascot != hovered {
            self.hover.mascot = hovered;
            self.after_hover_change(now);
        }
    }

    pub fn set_panel_hover(&mut self, hovered: bool, now: Instant) {
        if self.hover.panel != hovered {
            self.hover.panel = hovered;
            self.after_hover_change(now);
        }
    }

    fn after_hover_change(&mut self, now: Instant) {
        if self.hover.mascot || self.hover.panel {
            self.hover.grace_until = None;
        } else {
            // Pointer left everything: start the grace period, and let an explicit
            // mouse-out close the chat even if the voice cooldown was running.
            self.hover.grace_until = Some(now + HOVER_GRACE);
            self.voice_cooldown_until = None;
        }
    }

    pub fn should_show_chat(&self, input_selected: bool, now: Instant) -> bool {
        self.locked
            || self.hover.is_hovered(now)
            || self.drag.active
            || input_selected
            // Busy opens the chat to show its status pill, except booting: at startup
            // only the mascot pops out
            || (self.is_busy() && self.state != AppyState::Booting)
            || self.voice_cooldown_until.is_some_and(|until| now < until)
    }

    /// Turn the live status pill (e.g. "thinking...") into its finished form
    /// ("thought for 3s").
    pub fn finish_active_status(&mut self) {
        if let Some(active) = self.active_status.take() {
            active.finish();
        }
    }
}

/// The status pill currently animating in the chat log.
pub struct ActiveStatus {
    pub state: AppyState,
    pub label: Label,
    pub started: Instant,
}

impl ActiveStatus {
    fn finish(self) {
        let elapsed = format_duration(self.started.elapsed());
        self.label.set_text(&format!(
            "{} for {}",
            self.state.done_status_text(),
            elapsed
        ));
        if let Some(pill) = self.label.parent() {
            pill.remove_css_class("live");
        }
    }
}

pub fn format_duration(d: Duration) -> String {
    let total_secs = d.as_secs();
    let mins = total_secs / 60;
    let secs = total_secs % 60;
    if mins > 0 {
        format!("{mins}m{secs}s")
    } else {
        format!("{secs}s")
    }
}

#[derive(Debug, Default)]
pub struct Hover {
    pub mascot: bool,
    pub panel: bool,
    pub grace_until: Option<Instant>,
}

impl Hover {
    pub fn is_hovered(&self, now: Instant) -> bool {
        self.mascot || self.panel || self.grace_until.is_some_and(|until| now < until)
    }
}

/// An in-progress drag of the mascot (offsets relative to where the drag began).
#[derive(Debug, Default)]
pub struct Drag {
    pub active: bool,
    start: Point,
    last_dx: f64,
    last_dy: f64,
}

impl Drag {
    pub fn begin(&mut self, anchor: Point) {
        self.active = true;
        self.start = anchor;
        self.last_dx = 0.0;
        self.last_dy = 0.0;
    }

    /// Returns the movement since the previous update, for the wobble physics.
    pub fn update(&mut self, dx: f64, dy: f64) -> (f64, f64) {
        let step = (dx - self.last_dx, dy - self.last_dy);
        self.last_dx = dx;
        self.last_dy = dy;
        step
    }

    /// Where the drag would put the mascot (before clamping to the screen).
    pub fn target(&self, dx: f64, dy: f64) -> Point {
        (self.start.0 + dx, self.start.1 + dy)
    }
}

/// The weather card Appy pops out next to himself.
#[derive(Debug)]
pub struct Card {
    /// Current top-left, eased toward its slot each frame.
    pub pos: Point,
    pub dismiss_at: Instant,
    /// Set when the card starts flying back into Appy.
    pub leaving_since: Option<Instant>,
    pub hovered: bool,
}

impl Card {
    pub fn new(pos: Point, now: Instant) -> Self {
        Self {
            pos,
            dismiss_at: now + CARD_LIFETIME,
            leaving_since: None,
            hovered: false,
        }
    }

    /// A new weather report while the card is up: stay longer (and come back if leaving).
    pub fn refresh(&mut self, now: Instant) {
        self.dismiss_at = now + CARD_LIFETIME;
        self.leaving_since = None;
    }

    pub fn dismiss(&mut self, now: Instant) {
        self.leaving_since.get_or_insert(now);
    }

    /// Start leaving once its time is up, unless the pointer is resting on it.
    pub fn expire(&mut self, now: Instant) {
        if self.hovered {
            self.dismiss_at = self.dismiss_at.max(now + Duration::from_secs(2));
        } else if now >= self.dismiss_at {
            self.dismiss(now);
        }
    }

    /// True once the exit animation has finished and the card can be hidden.
    pub fn is_gone(&self, now: Instant) -> bool {
        self.leaving_since
            .is_some_and(|since| now.duration_since(since) >= CARD_EXIT)
    }
}

/// Damped spring that makes the shell jiggle after being dragged.
#[derive(Debug, Default)]
pub struct Wobble {
    pub x: f64,
    pub y: f64,
    vx: f64,
    vy: f64,
}

impl Wobble {
    pub fn kick(&mut self, step_dx: f64, step_dy: f64) {
        self.vx = (self.vx - step_dx * 0.55).clamp(-35.0, 35.0);
        self.vy = (self.vy + step_dy * 0.55).clamp(-35.0, 35.0);
    }

    /// Advance one frame. Returns true while still moving; snaps to rest otherwise.
    pub fn step(&mut self) -> bool {
        self.vx = (self.vx - self.x * 0.22) * 0.78;
        self.vy = (self.vy - self.y * 0.22) * 0.78;
        self.x += self.vx;
        self.y += self.vy;

        let moving = [self.x, self.y, self.vx, self.vy]
            .iter()
            .any(|v| v.abs() > 0.05);
        if !moving {
            *self = Self::default();
        }
        moving
    }
}

#[derive(Debug)]
pub struct Blink {
    /// 1.0 = eyes open, 0.0 = closed.
    pub scale: f64,
    start: Option<Instant>,
    next: Instant,
    seed: u64,
}

impl Blink {
    pub fn new(now: Instant) -> Self {
        Self {
            scale: 1.0,
            start: None,
            next: now + Duration::from_millis(3200),
            seed: 0x517cc1b727220a95,
        }
    }

    /// Advance the blink animation. `enabled` is false while the face has no blinkable
    /// eyes (busy dots, listening arcs). Returns true when the mascot needs a redraw.
    pub fn step(&mut self, now: Instant, enabled: bool) -> bool {
        if !enabled {
            let was_blinking = self.start.is_some() || (self.scale - 1.0).abs() > f64::EPSILON;
            self.start = None;
            self.scale = 1.0;
            return was_blinking;
        }

        match self.start {
            Some(start) => {
                let elapsed = now.duration_since(start).as_secs_f64() * 1000.0;
                if elapsed >= BLINK_DURATION_MS {
                    self.scale = 1.0;
                    self.start = None;
                    self.next = now + self.next_delay();
                } else {
                    self.scale = 1.0 - (elapsed / BLINK_DURATION_MS * PI).sin();
                }
                true
            }
            None => {
                if now >= self.next {
                    self.start = Some(now);
                }
                false
            }
        }
    }

    /// Pseudo-random 2.8-5.4s gap between blinks (LCG, no rand dependency).
    fn next_delay(&mut self) -> Duration {
        self.seed = self
            .seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        Duration::from_millis(2800 + ((self.seed >> 33) % 2600))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ms(n: u64) -> Duration {
        Duration::from_millis(n)
    }

    #[test]
    fn booting_does_not_open_the_chat() {
        // At startup only the mascot pops out; the chat waits for a hover as usual
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.state = AppyState::Booting;
        assert!(!s.should_show_chat(false, t0));
        s.state = AppyState::Thinking;
        assert!(s.should_show_chat(false, t0));
    }

    #[test]
    fn starts_booting() {
        // The overlay is up before the backend: it shows booting until told otherwise
        assert_eq!(SharedState::new(Instant::now()).state, AppyState::Booting);
    }

    #[test]
    fn hover_grace_keeps_chat_open_briefly() {
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.set_mascot_hover(true, t0);
        assert!(s.should_show_chat(false, t0));
        s.set_mascot_hover(false, t0);
        assert!(s.should_show_chat(false, t0 + ms(100)));
        assert!(!s.should_show_chat(false, t0 + ms(400)));
    }

    #[test]
    fn mouse_out_cancels_voice_cooldown() {
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.trigger_voice_cooldown(t0);
        assert!(s.should_show_chat(false, t0 + ms(5000)));
        s.set_panel_hover(true, t0);
        s.set_panel_hover(false, t0);
        assert!(!s.should_show_chat(false, t0 + ms(1000)));
    }

    #[test]
    fn busy_locked_or_typing_shows_chat() {
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        assert!(!s.should_show_chat(false, t0));
        assert!(s.should_show_chat(true, t0));
        s.state = AppyState::Thinking;
        assert!(s.should_show_chat(false, t0));
        s.state = AppyState::Idle;
        s.locked = true;
        assert!(s.should_show_chat(false, t0));
    }

    #[test]
    fn drag_tracks_steps_and_target() {
        let mut drag = Drag::default();
        drag.begin((100.0, 50.0));
        assert_eq!(drag.update(10.0, 5.0), (10.0, 5.0));
        assert_eq!(drag.update(15.0, 5.0), (5.0, 0.0));
        assert_eq!(drag.target(-500.0, 20.0), (-400.0, 70.0));
    }

    #[test]
    fn card_lifecycle() {
        let t0 = Instant::now();
        let mut card = Card::new((0.0, 0.0), t0);
        card.expire(t0 + ms(5_000));
        assert!(card.leaving_since.is_none());
        // Hovering holds it past its lifetime
        card.hovered = true;
        card.expire(t0 + ms(14_500));
        assert!(card.leaving_since.is_none());
        card.hovered = false;
        card.expire(t0 + ms(17_000));
        assert!(card.leaving_since.is_some());
        assert!(!card.is_gone(t0 + ms(17_100)));
        assert!(card.is_gone(t0 + ms(17_400)));
        // A fresh weather report brings it back
        card.refresh(t0 + ms(17_100));
        assert!(card.leaving_since.is_none());
    }

    #[test]
    fn wobble_settles() {
        let mut w = Wobble::default();
        assert!(!w.step());
        w.kick(-20.0, 10.0);
        assert!(w.step());
        let frames = (0..200).take_while(|_| w.step()).count();
        assert!(frames < 200, "wobble never settled");
        assert_eq!((w.x, w.y), (0.0, 0.0));
    }

    #[test]
    fn blink_cycle() {
        let t0 = Instant::now();
        let mut b = Blink::new(t0);
        assert!(!b.step(t0, true));
        // Starts at the scheduled time, then closes and reopens over 160ms
        assert!(!b.step(t0 + ms(3200), true));
        assert!(b.step(t0 + ms(3280), true));
        assert!(
            b.scale < 0.05,
            "eyes should be ~closed mid-blink, got {}",
            b.scale
        );
        assert!(b.step(t0 + ms(3400), true));
        assert_eq!(b.scale, 1.0);
        // Next blink is 2.8-5.4s later
        assert!(b.next >= t0 + ms(3400 + 2800) && b.next <= t0 + ms(3400 + 5400));
    }

    #[test]
    fn blink_resets_when_disabled() {
        let t0 = Instant::now();
        let mut b = Blink::new(t0);
        b.step(t0 + ms(3200), true);
        b.step(t0 + ms(3250), true);
        assert!(b.step(t0 + ms(3260), false));
        assert_eq!(b.scale, 1.0);
        assert!(!b.step(t0 + ms(3270), false));
    }

    #[test]
    fn durations() {
        assert_eq!(format_duration(ms(4_900)), "4s");
        assert_eq!(format_duration(ms(125_000)), "2m5s");
    }
}
