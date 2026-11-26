//! The ~60fps frame loop: layout following, input region, animations, chat visibility.

use std::f64::consts::TAU;
use std::time::{Duration, Instant};

use gtk4::glib;
use gtk4::prelude::*;

use crate::app::App;
use crate::chat::format_live_dots;
use crate::input::set_class;
use crate::layout::{self, Rect};
use crate::protocol::{AppyState, Expression};
use crate::state::SharedState;
use crate::weather::CARD_SIZE;

const FRAME: Duration = Duration::from_millis(16);
/// Live status pills cycle "." -> ".." -> "..." at this pace.
const DOTS_STEP_MS: u128 = 400;
/// Per-frame easing of the weather card toward its spot (and back into Appy).
const CARD_EASE: f64 = 0.2;
const CARD_EASE_LEAVING: f64 = 0.3;

pub fn start(app: &'static App) {
    glib::timeout_add_local(FRAME, move || {
        app.tick();
        glib::ControlFlow::Continue
    });
}

impl App {
    fn tick(&self) {
        let now = Instant::now();
        let mut s = self.state.borrow_mut();

        self.layout_shell(&mut s);
        let card_gone = self.step_card(&mut s, now);
        self.sync_input_region(&mut s);

        let wobbling = s.wobble.step();
        self.apply_wobble_css(&mut s, wobbling);
        self.ui.chat.step_scroll();
        self.animate_status_pill(&s);

        let blink_enabled = !s.is_busy() && s.state != AppyState::Listening;
        let mut redraw = wobbling;
        redraw |= s.blink.step(now, blink_enabled);
        redraw |= self.update_chat_visibility(&mut s, now);
        redraw |= self.advance_face(&mut s, now);

        if redraw {
            self.ui.mascot.queue_draw();
        }

        // Hiding a widget under the pointer emits `leave` synchronously, so only do it
        // once the state is released.
        drop(s);
        if card_gone {
            self.ui.card.hide();
        }
    }

    /// Position the shell around the mascot: home it on first layout, keep the mascot
    /// on screen, and put the chat on whichever side has room. Called every frame and
    /// on every drag update.
    pub fn layout_shell(&self, s: &mut SharedState) {
        let ui = &self.ui;
        let win = ui.window_size();
        if win.0 <= 0.0 || win.1 <= 0.0 {
            return;
        }
        let Some(shell) = ui.measure_shell(s.placement.side) else {
            return;
        };
        let body = ui.body_in_mascot();
        if !s.placement.is_initialized() {
            s.placement.home(win, &shell, body);
        }
        // Also catches a screen resize leaving Appy stranded off-screen
        s.placement.anchor = layout::clamp_mascot(s.placement.anchor, win, body);

        // Pick the chat's side freely while it's hidden; while it's open, keep it put
        // (so new messages can't make it jump) unless Appy is being dragged.
        if (!s.chat_open || s.drag.active) && s.placement.choose_side(win, &shell, s.chat_open) {
            ui.apply_side(s.placement.side, s.chat_open);
        }
        ui.move_shell(s.placement.shell_origin(&shell));
    }

    /// The mascot body in window coordinates.
    pub fn body_on_screen(&self, s: &SharedState) -> Rect {
        let body = self.ui.body_in_mascot();
        let (ax, ay) = s.placement.anchor;
        Rect {
            x: ax + body.x,
            y: ay + body.y,
            ..body
        }
    }

    /// Where the weather card belongs right now: beside Appy, or beside the whole glass
    /// panel in glass mode (so it never sits on top of it).
    pub fn card_slot(&self, s: &SharedState) -> layout::Point {
        let body = self.body_on_screen(s);
        let clear_of = if s.locked {
            self.ui.shell_bounds().unwrap_or(body)
        } else {
            body
        };
        layout::card_slot(
            body,
            clear_of,
            (CARD_SIZE, CARD_SIZE),
            self.ui.window_size(),
            s.placement.side,
            s.chat_open,
        )
    }

    /// Ease the weather card toward its spot beside Appy, or back into him when it's
    /// leaving. Returns true once the exit animation is done and the card should be
    /// hidden (by the caller, after releasing the state).
    fn step_card(&self, s: &mut SharedState, now: Instant) -> bool {
        let body = self.body_on_screen(s);
        let slot = self.card_slot(s);
        let Some(card) = s.card.as_mut() else {
            return false;
        };

        card.expire(now);
        if card.is_gone(now) {
            s.card = None;
            s.last_input_region = None;
            return true;
        }

        let size = (CARD_SIZE, CARD_SIZE);
        let (target, rate) = if card.leaving_since.is_some() {
            if self.ui.card.is_present() {
                self.ui.card.pop_out();
            }
            let (cx, cy) = body.center();
            ((cx - size.0 * 0.5, cy - size.1 * 0.5), CARD_EASE_LEAVING)
        } else {
            (slot, CARD_EASE)
        };
        let (pos, _) = layout::ease_point(card.pos, target, rate);
        card.pos = pos;
        self.ui.move_card(pos);
        false
    }

    /// Keep the Wayland click-through input region matched to what's on screen.
    fn sync_input_region(&self, s: &mut SharedState) {
        if !self.ui.window.is_visible() {
            return;
        }
        let desired = self.ui.input_rects(s.chat_open, s.locked, s.drag.active);
        if !desired.is_empty() && s.last_input_region.as_ref() != Some(&desired) {
            self.ui.set_input_region(&desired);
            s.last_input_region = Some(desired);
        }
    }

    /// Wobbly-windows effect: in glass mode the whole shell wobbles, otherwise just the
    /// chat column (the mascot wobbles itself in its draw function).
    fn apply_wobble_css(&self, s: &mut SharedState, wobbling: bool) {
        let css = &self.ui.wobble_css;
        if wobbling {
            let tx = (s.wobble.x * 0.45).clamp(-14.0, 14.0);
            let ty = (s.wobble.y * 0.35).clamp(-10.0, 10.0);
            let skew_x = (s.wobble.x * 0.35).clamp(-8.0, 8.0);
            let rot = (s.wobble.x * 0.12).clamp(-3.5, 3.5);

            if s.locked {
                let g_tx = (tx * 0.75).clamp(-10.0, 10.0);
                let g_ty = (ty * 0.75).clamp(-8.0, 8.0);
                let g_skew = (skew_x * 0.60).clamp(-4.5, 4.5);
                let g_rot = (rot * 0.65).clamp(-2.2, 2.2);
                css.load_from_data(&format!(
                    ".appy-shell.glass-locked {{ transform: translate({g_tx:.2}px, {g_ty:.2}px) skewX({g_skew:.2}deg) rotate({g_rot:.2}deg); }}\n.chat-wobble {{ transform: none; }}"
                ));
            } else {
                css.load_from_data(&format!(
                    ".appy-shell.glass-locked {{ transform: none; }}\n.chat-wobble {{ transform: translate({tx:.2}px, {ty:.2}px) skewX({skew_x:.2}deg) rotate({rot:.2}deg); }}"
                ));
            }
            s.wobble_css_active = true;
        } else if s.wobble_css_active {
            css.load_from_data(".appy-shell.glass-locked, .chat-wobble { transform: none; }");
            s.wobble_css_active = false;
        }
    }

    fn animate_status_pill(&self, s: &SharedState) {
        let Some(active) = &s.active_status else {
            return;
        };
        let Some(word) = active.state.live_status_text() else {
            return;
        };
        let step = active.started.elapsed().as_millis() / DOTS_STEP_MS;
        let markup = format_live_dots(word, step);
        if active.label.label() != markup {
            active.label.set_markup(&markup);
        }
    }

    /// Open/close the chat panel. Returns true if the mascot needs a redraw.
    fn update_chat_visibility(&self, s: &mut SharedState, now: Instant) -> bool {
        let ui = &self.ui;
        let input_selected = ui.window.is_active() && ui.input.focus.contains_focus();
        let show = s.should_show_chat(input_selected, now);
        if s.chat_open == show {
            return false;
        }

        s.chat_open = show;
        ui.chat_panel.set_sensitive(show);
        set_class(&ui.chat_panel, "open", show);
        if show {
            return false;
        }

        s.hover.panel = false;
        ui.input.effort.collapse();
        // Relax back to the neutral face once the conversation is put away
        if s.state == AppyState::Idle && s.expression != Expression::Neutral {
            s.expression = Expression::Neutral;
            self.sync_pill(s);
            return true;
        }
        false
    }

    /// Advance the busy-dots / talking animations. Returns true if the mascot needs a redraw.
    fn advance_face(&self, s: &mut SharedState, now: Instant) -> bool {
        if s.is_busy() {
            s.phase = (s.phase + 0.14) % TAU;
            return true;
        }
        if s.state != AppyState::Speaking {
            return false;
        }
        if s.speaking_until.is_some_and(|until| now < until) {
            s.talk_phase = (s.talk_phase + 0.28) % (TAU * 10.0);
        } else {
            s.state = AppyState::Idle;
            s.speaking_until = None;
            s.talk_phase = 0.0;
            self.sync_pill(s);
        }
        true
    }
}
