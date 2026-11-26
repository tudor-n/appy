//! The ~60fps frame loop: layout following, input region, animations, chat visibility.

use std::f64::consts::TAU;
use std::time::{Duration, Instant};

use gtk4::glib;
use gtk4::prelude::*;

use crate::app::App;
use crate::chat::format_live_dots;
use crate::input::set_class;
use crate::protocol::{AppyState, Expression};
use crate::state::SharedState;

const FRAME: Duration = Duration::from_millis(16);
/// Live status pills cycle "." -> ".." -> "..." at this pace.
const DOTS_STEP_MS: u128 = 400;

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

        self.follow_layout(&mut s);
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
    }

    /// Place the shell on first layout, and keep its bottom edge pinned afterwards.
    fn follow_layout(&self, s: &mut SharedState) {
        let ui = &self.ui;
        let win = (ui.window.width() as f64, ui.window.height() as f64);
        let root = (ui.root.width() as f64, ui.root.height() as f64);
        let dragging = s.drag.active;
        if s.placement.follow_layout(win, root, dragging) {
            ui.move_shell(s.placement.x, s.placement.y);
        }
    }

    /// Keep the Wayland click-through input region matched to what's on screen.
    fn sync_input_region(&self, s: &mut SharedState) {
        if !self.ui.window.is_visible() {
            return;
        }
        let target = self.ui.input_target(s.chat_open, s.locked);
        let desired = self.ui.input_bounds(&target, s.drag.active);
        if let Some(bounds) = desired
            && desired != s.last_input_bounds
        {
            self.ui.set_input_region(bounds);
            s.last_input_bounds = desired;
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
