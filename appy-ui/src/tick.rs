//! The frame loop: layout following, input region, animations, chat visibility. It runs
//! on the window's frame clock, so it's in step with the screen (144 Hz on the laptop)
//! and its changes land in the frame being drawn. Animations are tuned in 60 Hz frames
//! and scaled by the time that actually passed.

use std::cell::Cell;
use std::f64::consts::TAU;
use std::time::Instant;

use gtk4::glib;
use gtk4::prelude::*;

use crate::app::App;
use crate::chat::format_live_dots;
use crate::input::set_class;
use crate::layout::{self, Rect};
use crate::mascot::BUSY_PHASE_WRAP;
use crate::protocol::AppyState;
use crate::state::{FRAME_SECS, SharedState, per_frame};
use crate::card::{CardKind, CARD_SIZE};

/// After a stall (or the window coming back from hidden), catch up at most this many
/// 60 Hz frames, so nothing leaps.
const MAX_FRAMES: f64 = 4.0;
/// Live status pills cycle "." -> ".." -> "..." at this pace.
const DOTS_STEP_MS: u128 = 400;
/// Per-frame easing of the cards toward their spots (and back into Appy).
const CARD_EASE: f64 = 0.2;
const CARD_EASE_LEAVING: f64 = 0.3;
/// Per-frame easing of the memory panel toward a new slot.
const MEMORY_EASE: f64 = 0.2;

pub fn start(app: &'static App) {
    // Ticks only while the window is shown; a hidden Appy costs nothing
    let last_frame = Cell::new(None::<i64>);
    app.ui.window.add_tick_callback(move |_, clock| {
        let t = clock.frame_time(); // µs
        let frames = last_frame.replace(Some(t)).map_or(1.0, |prev| {
            ((t - prev) as f64 / 1e6 / FRAME_SECS).clamp(0.0, MAX_FRAMES)
        });
        app.tick(frames);
        glib::ControlFlow::Continue
    });
}

impl App {
    /// One frame; `frames` is how many 60 Hz frames' worth of time it covers.
    fn tick(&self, frames: f64) {
        let now = Instant::now();
        let mut s = self.state.borrow_mut();

        self.layout_shell(&mut s);
        self.place_memory(&mut s, now, frames);
        let cards_gone: Vec<CardKind> = CardKind::ALL
            .into_iter()
            .filter(|&kind| self.step_card(&mut s, kind, now, frames))
            .collect();
        self.sync_input_region(&mut s);

        let wobbling = s.wobble.step(frames);
        self.apply_wobble_css(&mut s, wobbling);
        self.ui.input.fit_height();
        self.ui.chat.step_height(frames, self.ui.log_room());
        self.ui.chat.step_scroll(frames);
        self.ui.input.compact.step(frames, now);
        self.animate_status_pill(&s);

        let blink_enabled = !s.is_busy() && s.state != AppyState::Listening;
        let mut redraw = wobbling;
        redraw |= s.blink.step(now, blink_enabled);
        redraw |= self.update_chat_visibility(&mut s, now);
        redraw |= self.advance_face(&mut s, now, frames);

        if redraw {
            self.ui.mascot.queue_draw();
        }

        // Hiding a widget under the pointer emits `leave` synchronously, so only do it
        // once the state is released.
        drop(s);
        for kind in cards_gone {
            self.ui.card_shell(kind).hide();
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
        // Moving to another monitor: hold still until the window is the new one, so the
        // new position isn't clamped to the old screen
        if let Some(mv) = s.monitor_move {
            if !mv.arrived(win, Instant::now()) {
                return;
            }
            s.placement.anchor = mv.anchor;
            s.monitor_move = None;
            s.last_input_region = None;
            s.landing = true;
        }
        let Some(shell) = ui.measure_shell() else {
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
        if (!s.chat_open || s.is_moving()) && s.placement.choose_side(win, &shell, s.chat_open) {
            ui.apply_side(s.placement.side, s.chat_open);
        }
        ui.move_shell(s.placement.shell_origin(&shell));

        if s.landing {
            s.landing = false;
            // Everything that floats beside him starts out in place, not flying in from
            // where it was on the other monitor; then fade in, with a little hop
            s.memory_pos = None;
            for kind in CardKind::ALL {
                let slot = self.card_slot(s, kind);
                if let Some(card) = s.cards[kind.index()].as_mut() {
                    card.pos = slot;
                    ui.move_card(kind, slot);
                }
            }
            s.wobble.kick(0.0, -9.0);
            ui.stage.remove_css_class("moving");
        }
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

    /// Where a card belongs right now: beside Appy, or beside the whole glass panel in
    /// glass mode (so it never sits on top of it). Cards that are up share one column,
    /// music on top of weather.
    pub fn card_slot(&self, s: &SharedState, kind: CardKind) -> layout::Point {
        let (music, weather) = s.stacked();
        let count = (music as usize + weather as usize).max(1);
        let body = self.body_on_screen(s);
        let clear_of = if s.locked {
            self.ui.shell_bounds().unwrap_or(body)
        } else {
            body
        };
        let column = layout::card_slot(
            body,
            clear_of,
            (CARD_SIZE, layout::stack_height(CARD_SIZE, count)),
            self.ui.window_size(),
            s.placement.side,
            s.chat_open,
            self.ui.memory_bounds(),
        );
        let (music_slot, weather_slot) = layout::stack_slots(column, CARD_SIZE, music, weather);
        match kind {
            CardKind::Music => music_slot,
            CardKind::Weather => weather_slot,
        }
    }

    /// Show the memory panel while the chat is open and there's something to review,
    /// and keep it above the chat (or below it, when there's no room above).
    fn place_memory(&self, s: &mut SharedState, now: Instant, frames: f64) {
        let ui = &self.ui;
        let shown = s.chat_open && ui.memory.is_wanted();
        if shown != ui.memory.is_shown() {
            if !shown && ui.memory.root.focus_child().is_some() {
                // Edit was open: don't leave the keyboard in a hidden text box
                GtkWindowExt::set_focus(&ui.window, None::<&gtk4::Widget>);
            }
            ui.memory.set_shown(shown);
            if !shown {
                s.memory_panel_hidden(now);
            }
        }
        let Some(chat) = ui.memory_anchor(s.locked) else {
            return;
        };
        let (slot, side) = layout::memory_slot(
            chat,
            ui.memory_size(),
            ui.window_size(),
            s.placement.side,
            s.memory_side,
            s.is_moving(),
        );
        s.memory_side = side;
        // Glide to a new slot (glass mode on or off, flipping above/below); stick to it
        // while Appy is being moved, and while hidden, so it reappears in place
        let pos = match s.memory_pos {
            Some(current) if shown && !s.is_moving() => {
                layout::ease_point(current, slot, per_frame(MEMORY_EASE, frames)).0
            }
            _ => slot,
        };
        s.memory_pos = shown.then_some(pos);
        ui.move_memory(pos);
    }

    /// Ease a card toward its spot beside Appy, or back into him when it's leaving.
    /// Returns true once the exit animation is done and the card should be hidden (by
    /// the caller, after releasing the state).
    fn step_card(&self, s: &mut SharedState, kind: CardKind, now: Instant, frames: f64) -> bool {
        let body = self.body_on_screen(s);
        let slot = self.card_slot(s, kind);
        let Some(card) = s.cards[kind.index()].as_mut() else {
            return false;
        };

        card.expire(now);
        if card.is_gone(now) {
            s.cards[kind.index()] = None;
            s.last_input_region = None;
            return true;
        }

        let shell = self.ui.card_shell(kind);
        let size = (CARD_SIZE, CARD_SIZE);
        let (target, rate) = if card.leaving_since.is_some() {
            if shell.is_present() {
                shell.pop_out();
            }
            let (cx, cy) = body.center();
            ((cx - size.0 * 0.5, cy - size.1 * 0.5), CARD_EASE_LEAVING)
        } else {
            (slot, CARD_EASE)
        };
        let (pos, _) = layout::ease_point(card.pos, target, per_frame(rate, frames));
        card.pos = pos;
        self.ui.move_card(kind, pos);
        false
    }

    /// Keep the Wayland click-through input region matched to what's on screen.
    fn sync_input_region(&self, s: &mut SharedState) {
        if !self.ui.window.is_visible() {
            return;
        }
        let desired = self.ui.input_rects(s.chat_open, s.locked, s.is_moving());
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
                    ".appy-shell.glass-locked {{ transform: translate({g_tx:.2}px, {g_ty:.2}px) skewX({g_skew:.2}deg) rotate({g_rot:.2}deg); }}\n.chat-wobble, .memory-panel.open {{ transform: none; }}"
                ));
            } else {
                css.load_from_data(&format!(
                    ".appy-shell.glass-locked {{ transform: none; }}\n.chat-wobble, .memory-panel.open {{ transform: translate({tx:.2}px, {ty:.2}px) skewX({skew_x:.2}deg) rotate({rot:.2}deg); }}"
                ));
            }
            s.wobble_css_active = true;
        } else if s.wobble_css_active {
            css.load_from_data(
                ".appy-shell.glass-locked, .chat-wobble, .memory-panel.open { transform: none; }",
            );
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
        // Typing in the memory panel's Edit box counts like typing in the chat
        let input_selected = ui.window.is_active()
            && (ui.input.focus.contains_focus() || ui.memory.contains_focus());
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
        // Put away scrolled up: it comes back showing the newest messages, at its usual size
        ui.chat.pin_to_bottom();
        // Relax back to the resting face (neutral, or focused in focus mode) once the
        // conversation is put away
        let resting = s.mode.resting_expression();
        if s.state == AppyState::Idle && s.expression != resting {
            s.expression = resting;
            self.sync_pill(s);
            return true;
        }
        false
    }

    /// Advance the busy-dots / talking animations. Returns true if the mascot needs a redraw.
    fn advance_face(&self, s: &mut SharedState, now: Instant, frames: f64) -> bool {
        if s.is_busy() {
            s.phase = (s.phase + 0.14 * frames) % BUSY_PHASE_WRAP;
            return true;
        }
        if s.state != AppyState::Speaking {
            return false;
        }
        if s.speaking_until.is_some_and(|until| now < until) {
            s.talk_phase = (s.talk_phase + 0.28 * frames) % (TAU * 10.0);
        } else {
            s.state = AppyState::Idle;
            s.speaking_until = None;
            s.talk_phase = 0.0;
            self.sync_pill(s);
        }
        true
    }
}
