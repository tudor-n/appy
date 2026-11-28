//! Mutable UI state, grouped by concern. Everything except `ActiveStatus` is plain data
//! with time passed in explicitly, so the logic is unit-testable without GTK.

use crate::card::CardKind;
use std::f64::consts::PI;
use std::time::{Duration, Instant};

use gtk4::Label;
use gtk4::prelude::*;

use crate::layout::{Placement, Point, VSide};
use crate::protocol::{AppyState, Effort, Expression, Mode};

/// How long the chat stays visible after backend activity, so you can read the reply.
const VOICE_COOLDOWN: Duration = Duration::from_secs(8);
/// How long the chat stays open after the pointer leaves the mascot, so you can
/// cross the gap to the messages without them collapsing.
const HOVER_GRACE: Duration = Duration::from_millis(300);
const BLINK_DURATION_MS: f64 = 160.0;
/// How long a pop-out card stays up (unless hovered).
const CARD_LIFETIME: Duration = Duration::from_secs(14);
/// How long the card's exit animation runs before it's hidden.
pub const CARD_EXIT: Duration = Duration::from_millis(320);

pub struct SharedState {
    pub state: AppyState,
    pub expression: Expression,
    pub effort: Effort,
    /// Appy's mode, from the backend; decides the resting face.
    pub mode: Mode,
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
    /// The pop-out cards while they're on screen, by `CardKind::index` (weather, music).
    pub cards: [Option<Card>; 2],
    /// The memory service's auto-approve setting; None until the brain says.
    pub auto_approve: Option<bool>,
    /// Whether the memory panel sits above or below the chat.
    pub memory_side: VSide,
    /// Where the memory panel is drawn, eased toward its slot; None while it's hidden.
    pub memory_pos: Option<Point>,
    /// The overlay is moving to another monitor: where the mascot goes there, applied
    /// once the window has its new size.
    pub monitor_move: Option<MonitorMove>,
    /// Just arrived on another monitor: the next full layout puts everything in place
    /// and fades the overlay back in.
    pub landing: bool,
}

/// See `SharedState::monitor_move`.
#[derive(Debug, Clone, Copy)]
pub struct MonitorMove {
    pub anchor: Point,
    /// The new monitor's size; the window ends up this wide and at most this tall.
    pub size: (f64, f64),
    pub since: Instant,
}

/// Give up waiting for the window's new size after this long.
pub const MONITOR_MOVE_TIMEOUT: Duration = Duration::from_millis(600);

impl MonitorMove {
    /// Whether a `win`-sized window is the one on the new monitor (panels may take
    /// some of its height).
    pub fn arrived(&self, win: (f64, f64), now: Instant) -> bool {
        let fits = (win.0 - self.size.0).abs() < 1.0
            && win.1 <= self.size.1 + 0.5
            && win.1 > self.size.1 - 200.0;
        fits || now.duration_since(self.since) > MONITOR_MOVE_TIMEOUT
    }
}

impl SharedState {
    /// Which cards hold a place in the stack beside Appy: (music, weather). A card that's
    /// flying back keeps its place until it's gone, so the other doesn't slide through it.
    pub fn stacked(&self) -> (bool, bool) {
        let held = |kind: CardKind| self.cards[kind.index()].is_some();
        (held(CardKind::Music), held(CardKind::Weather))
    }

    pub fn new(now: Instant) -> Self {
        Self {
            // The overlay comes up before the backend, which replays "booting" (continuing
            // this) or "idle" (ending it) once it's connected
            state: AppyState::Booting,
            expression: Expression::Neutral,
            effort: Effort::Off,
            mode: Mode::Banter,
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
            cards: [None, None],
            auto_approve: None,
            memory_side: VSide::default(),
            memory_pos: None,
            monitor_move: None,
            landing: false,
        }
    }

    /// Appy is being dragged.
    pub fn is_moving(&self) -> bool {
        self.drag.active
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

    /// The pointer is over the memory panel: keeps the chat open like the chat itself.
    pub fn set_memory_hover(&mut self, hovered: bool, now: Instant) {
        if self.hover.memory != hovered {
            self.hover.memory = hovered;
            self.after_hover_change(now);
        }
    }

    /// The memory panel went away (last proposal decided, or Later) while it may have been
    /// under the pointer, which then never gets a `leave`: let go of its hover with the
    /// usual grace, so the chat (and the chip that just landed) doesn't snap shut.
    pub fn memory_panel_hidden(&mut self, now: Instant) {
        self.set_memory_hover(false, now);
    }

    fn after_hover_change(&mut self, now: Instant) {
        if self.hover.mascot || self.hover.panel || self.hover.memory {
            self.hover.grace_until = None;
        } else {
            // Pointer left everything: start the grace period, and let an explicit
            // mouse-out close the chat even if the voice cooldown was running.
            self.hover.grace_until = Some(now + HOVER_GRACE);
            self.voice_cooldown_until = None;
        }
    }

    pub fn should_show_chat(&self, input_selected: bool, now: Instant) -> bool {
        // While booting there's nothing to read or type into yet: only the mascot shows
        if self.state == AppyState::Booting {
            return false;
        }
        self.locked
            || self.hover.is_hovered(now)
            || self.is_moving()
            || input_selected
            // Busy opens the chat to show its status pill
            || self.is_busy()
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
        let took = self.started.elapsed();
        // The overlay always starts on "loading"; when the brain turns out to be up
        // already, there was no loading to report ("loaded for 0s")
        if self.state == AppyState::Booting && took < Duration::from_secs(1) {
            if let Some(pill) = self.label.parent()
                && let Some(log) = pill.parent().and_downcast::<gtk4::Box>()
            {
                log.remove(&pill);
            }
            return;
        }
        let elapsed = format_duration(took);
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

/// Animations are tuned in 60 Hz frames. The frame loop runs at the screen's own rate
/// and passes how many of those frames went by (e.g. 0.42 on a 144 Hz screen).
pub const FRAME_SECS: f64 = 1.0 / 60.0;

/// An exponential ease's per-frame `rate`, adjusted for `frames` elapsed: easing by the
/// result once covers the same ground as easing by `rate` `frames` times.
pub fn per_frame(rate: f64, frames: f64) -> f64 {
    1.0 - (1.0 - rate).powf(frames)
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
    pub memory: bool,
    pub grace_until: Option<Instant>,
}

impl Hover {
    pub fn is_hovered(&self, now: Instant) -> bool {
        self.mascot
            || self.panel
            || self.memory
            || self.grace_until.is_some_and(|until| now < until)
    }
}

/// An in-progress drag of the mascot (offsets relative to where the drag began).
#[derive(Debug, Default)]
pub struct Drag {
    pub active: bool,
    /// The pointer's position on the mascot's drawing area when the drag began.
    pub grab: Point,
    /// The pointer is past the screen edge, over another monitor: letting go there
    /// moves Appy to it.
    pub over_other_monitor: bool,
    start: Point,
    last_dx: f64,
    last_dy: f64,
}

impl Drag {
    /// `pointer` is where the drag began, in window coordinates.
    pub fn begin(&mut self, anchor: Point, pointer: Point) {
        self.active = true;
        self.over_other_monitor = false;
        self.grab = (pointer.0 - anchor.0, pointer.1 - anchor.1);
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

/// A pop-out card (weather, now playing) beside Appy.
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

    /// New content while the card is up: stay longer (and come back if leaving).
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

    /// Advance by `frames` 60 Hz frames (fractional on faster screens). Returns true
    /// while still moving; snaps to rest otherwise.
    pub fn step(&mut self, frames: f64) -> bool {
        // Whole-frame substeps at most, so the spring behaves the same at any refresh rate
        let mut left = frames;
        while left > 0.0 {
            let f = left.min(1.0);
            let damping = 0.78f64.powf(f);
            self.vx = (self.vx - self.x * 0.22 * f) * damping;
            self.vy = (self.vy - self.y * 0.22 * f) * damping;
            self.x += self.vx * f;
            self.y += self.vy * f;
            left -= f;
        }

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
    fn chat_stays_hidden_while_booting_even_on_hover() {
        // Nothing to read or type into until Appy is up
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.state = AppyState::Booting;
        s.hover.mascot = true;
        assert!(!s.should_show_chat(false, t0));
        assert!(!s.should_show_chat(true, t0));
        s.state = AppyState::Idle;
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
        s.state = AppyState::Idle; // booted
        s.set_mascot_hover(true, t0);
        assert!(s.should_show_chat(false, t0));
        s.set_mascot_hover(false, t0);
        assert!(s.should_show_chat(false, t0 + ms(100)));
        assert!(!s.should_show_chat(false, t0 + ms(400)));
    }

    #[test]
    fn moving_from_the_chat_to_the_memory_panel_keeps_the_chat_open() {
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.state = AppyState::Idle; // booted
        s.set_panel_hover(true, t0);
        s.set_panel_hover(false, t0); // crossing the click-through gap
        s.set_memory_hover(true, t0 + ms(80));
        assert!(s.should_show_chat(false, t0 + ms(5000)));
        s.set_memory_hover(false, t0 + ms(5000));
        assert!(!s.should_show_chat(false, t0 + ms(5400)));
    }

    #[test]
    fn deciding_the_last_proposal_keeps_the_chat_open_briefly() {
        // The panel goes away under the pointer: the usual grace, not an instant close
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.state = AppyState::Idle; // booted
        s.set_memory_hover(true, t0);
        s.memory_panel_hidden(t0);
        assert!(!s.hover.memory);
        assert!(s.should_show_chat(false, t0 + ms(100)));
        assert!(!s.should_show_chat(false, t0 + ms(400)));
    }

    #[test]
    fn mouse_out_cancels_voice_cooldown() {
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.state = AppyState::Idle; // booted
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
        s.state = AppyState::Idle; // booted
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
        drag.begin((100.0, 50.0), (130.0, 90.0));
        assert_eq!(drag.grab, (30.0, 40.0));
        assert_eq!(drag.update(10.0, 5.0), (10.0, 5.0));
        assert_eq!(drag.update(15.0, 5.0), (5.0, 0.0));
        assert_eq!(drag.target(-500.0, 20.0), (-400.0, 70.0));
    }

    #[test]
    fn a_leaving_card_keeps_its_place_in_the_stack() {
        // Otherwise the music card slides down through the weather card flying back
        let t0 = Instant::now();
        let mut s = SharedState::new(t0);
        s.cards[CardKind::Weather.index()] = Some(Card::new((0.0, 0.0), t0));
        s.cards[CardKind::Music.index()] = Some(Card::new((0.0, 0.0), t0));
        s.cards[CardKind::Weather.index()].as_mut().unwrap().dismiss(t0);
        assert_eq!(s.stacked(), (true, true));
        s.cards[CardKind::Weather.index()] = None; // gone
        assert_eq!(s.stacked(), (true, false));
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
    fn monitor_move_waits_for_the_new_window_size() {
        let t0 = Instant::now();
        let mv = MonitorMove {
            anchor: (10.0, 10.0),
            size: (1920.0, 1080.0),
            since: t0,
        };
        assert!(!mv.arrived((1536.0, 864.0), t0)); // still the laptop-sized window
        assert!(mv.arrived((1920.0, 1032.0), t0)); // new screen, minus a panel
        assert!(mv.arrived((1536.0, 864.0), t0 + ms(700))); // gave up waiting
    }

    #[test]
    fn wobble_settles() {
        let mut w = Wobble::default();
        assert!(!w.step(1.0));
        w.kick(-20.0, 10.0);
        assert!(w.step(1.0));
        let frames = (0..200).take_while(|_| w.step(1.0)).count();
        assert!(frames < 200, "wobble never settled");
        assert_eq!((w.x, w.y), (0.0, 0.0));
    }

    #[test]
    fn wobble_is_the_same_at_any_refresh_rate() {
        // 60 frames at 60 Hz vs 144 frames at 144 Hz: about the same motion
        let (mut slow, mut fast) = (Wobble::default(), Wobble::default());
        slow.kick(-20.0, 10.0);
        fast.kick(-20.0, 10.0);
        for i in 0..144 {
            if i % 12 == 0 {
                // compare at the 5 common instants per 12 fast frames
                assert!((slow.x - fast.x).abs() < 1.5, "x {} vs {}", slow.x, fast.x);
            }
            fast.step(60.0 / 144.0);
            if i % 12 == 11 {
                for _ in 0..5 {
                    slow.step(1.0);
                }
            }
        }
    }

    #[test]
    fn per_frame_rate_composes() {
        // Two half frames cover what one whole frame does
        let half = per_frame(0.22, 0.5);
        let after_two_halves = 1.0 - (1.0 - half) * (1.0 - half);
        assert!((after_two_halves - 0.22).abs() < 1e-12);
        assert!((per_frame(0.22, 1.0) - 0.22).abs() < 1e-12);
        assert_eq!(per_frame(0.22, 0.0), 0.0);
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
