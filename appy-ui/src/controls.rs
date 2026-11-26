//! Pointer and keyboard handling.
//!
//! - Hover the mascot body to peek at the chat; hovering the chat keeps it open.
//! - Drag the mascot to move the overlay (it wobbles when let go).
//! - Double left-click the mascot to hide the overlay (the panel pill brings it back).
//! - Double right-click to toggle glass mode (pinned open, dark backdrop); Esc leaves it.
//! - Enter sends, Shift+Enter inserts a newline.

use std::time::Instant;

use gtk4::gdk::{Key, ModifierType};
use gtk4::prelude::*;
use gtk4::{
    EventControllerKey, EventControllerMotion, EventControllerScroll, EventControllerScrollFlags,
    GestureClick, GestureDrag, PickFlags, PropagationPhase, glib,
};

use crate::app::App;
use crate::input::set_class;
use crate::mascot;

const WHEEL_STEP: f64 = 64.0;
const ARROW_STEP: f64 = 72.0;
const PAGE_STEP: f64 = 220.0;

pub fn connect(app: &'static App) {
    connect_map(app);
    connect_scrolling(app);
    connect_hover(app);
    connect_clicks(app);
    connect_drag(app);
    connect_input(app);
}

fn connect_map(app: &'static App) {
    app.ui.window.connect_map(move |_| {
        let mut s = app.state.borrow_mut();
        s.last_input_bounds = None;
        app.ui
            .update_input_region(s.chat_open, s.locked, s.drag.active);
    });
}

fn connect_scrolling(app: &'static App) {
    let wheel = EventControllerScroll::new(EventControllerScrollFlags::VERTICAL);
    wheel.connect_scroll(move |_, _dx, dy| {
        let s = app.state.borrow();
        if s.locked || s.chat_open {
            app.ui.chat.scroll_by(dy * WHEEL_STEP);
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    app.ui.chat_panel.add_controller(wheel);

    // Keyboard scrolling (and Esc) only in glass mode
    let keys = EventControllerKey::new();
    keys.connect_key_pressed(move |_, keyval, _, _| {
        let mut s = app.state.borrow_mut();
        if !s.locked {
            return glib::Propagation::Proceed;
        }
        let delta = match keyval {
            Key::Up => -ARROW_STEP,
            Key::Down => ARROW_STEP,
            Key::Page_Up => -PAGE_STEP,
            Key::Page_Down => PAGE_STEP,
            Key::Escape => {
                s.locked = false;
                app.ui.root.remove_css_class("glass-locked");
                return glib::Propagation::Stop;
            }
            _ => return glib::Propagation::Proceed,
        };
        app.ui.chat.scroll_by(delta);
        glib::Propagation::Stop
    });
    app.ui.window.add_controller(keys);
}

/// Only the mascot body pushes the messages out. Once they're out, hovering the message
/// panel keeps them in view. A short grace period covers the gap between the two.
fn connect_hover(app: &'static App) {
    let on_body = move |x: f64, y: f64| {
        let m = &app.ui.mascot;
        mascot::body_contains(m.width() as f64, m.height() as f64, x, y)
    };

    let mascot_motion = EventControllerMotion::new();
    mascot_motion.connect_enter(move |_, x, y| {
        app.state
            .borrow_mut()
            .set_mascot_hover(on_body(x, y), Instant::now());
    });
    mascot_motion.connect_motion(move |_, x, y| {
        app.state
            .borrow_mut()
            .set_mascot_hover(on_body(x, y), Instant::now());
    });
    mascot_motion.connect_leave(move |_| {
        app.state
            .borrow_mut()
            .set_mascot_hover(false, Instant::now());
    });
    app.ui.mascot.add_controller(mascot_motion);

    let panel_motion = EventControllerMotion::new();
    panel_motion.connect_enter(move |_, _, _| {
        app.state.borrow_mut().set_panel_hover(true, Instant::now());
    });
    panel_motion.connect_leave(move |_| {
        app.state
            .borrow_mut()
            .set_panel_hover(false, Instant::now());
    });
    app.ui.chat_panel.add_controller(panel_motion);
}

fn connect_clicks(app: &'static App) {
    let left = GestureClick::new();
    left.set_button(1);
    left.connect_pressed(move |_, n_press, _, _| {
        if n_press == 2 {
            app.minimize();
        }
    });
    app.ui.mascot.add_controller(left);

    let right = GestureClick::new();
    right.set_button(3);
    right.connect_pressed(move |_, n_press, _, _| {
        if n_press == 2 {
            let mut s = app.state.borrow_mut();
            s.locked = !s.locked;
            set_class(&app.ui.root, "glass-locked", s.locked);
        }
    });
    app.ui.mascot.add_controller(right);
}

fn connect_drag(app: &'static App) {
    let drag = GestureDrag::new();

    drag.connect_drag_begin(move |_, start_x, start_y| {
        let mascot_widget: gtk4::Widget = app.ui.mascot.clone().upcast();
        let hit_mascot = app
            .ui
            .fixed
            .pick(start_x, start_y, PickFlags::DEFAULT)
            .is_some_and(|w| w == mascot_widget);

        let mut guard = app.state.borrow_mut();
        let s = &mut *guard;
        if !hit_mascot {
            s.drag.active = false;
            return;
        }
        s.drag.begin(&s.placement);
        s.last_input_bounds = None;
        app.ui.update_input_region(s.chat_open, s.locked, true);
    });

    drag.connect_drag_update(move |_, dx, dy| {
        let mut guard = app.state.borrow_mut();
        let s = &mut *guard;
        if !s.drag.active {
            return;
        }
        let ui = &app.ui;
        let max_x = (ui.window.width() as f64 - ui.root.width() as f64).max(0.0);
        let max_y = (ui.window.height() as f64 - ui.root.height() as f64).max(0.0);
        (s.placement.x, s.placement.y) = s.drag.target(dx, dy, max_x, max_y);
        ui.move_shell(s.placement.x, s.placement.y);

        let (step_dx, step_dy) = s.drag.update(dx, dy);
        s.wobble.kick(step_dx, step_dy);
    });

    drag.connect_drag_end(move |_, _, _| {
        let mut s = app.state.borrow_mut();
        if s.drag.active {
            s.drag.active = false;
            s.last_input_bounds = None;
            app.ui.update_input_region(s.chat_open, s.locked, false);
        }
    });

    app.ui.fixed.add_controller(drag);
}

fn connect_input(app: &'static App) {
    // Capture phase so the TextView's own Return handling never sees a plain Enter.
    let send_key = EventControllerKey::new();
    send_key.set_propagation_phase(PropagationPhase::Capture);
    send_key.connect_key_pressed(move |_, keyval, _, modifiers| {
        let is_enter = matches!(keyval, Key::Return | Key::KP_Enter);
        if is_enter && !modifiers.contains(ModifierType::SHIFT_MASK) {
            app.submit();
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    app.ui.input.view.add_controller(send_key);

    for (effort, button) in &app.ui.input.effort.options {
        let effort = *effort;
        button.connect_clicked(move |_| app.set_effort(effort));
    }
}
