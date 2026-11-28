//! Pointer and keyboard handling.
//!
//! - Hover the mascot body to peek at the chat; hovering the chat keeps it open.
//! - Drag the mascot to move the overlay (it wobbles when let go). Let go past the edge
//!   of the screen, over another monitor, and he moves to that monitor.
//! - Double left-click the mascot to hide the overlay (the panel pill brings it back).
//! - Double right-click to toggle glass mode (pinned open, dark backdrop); Esc leaves it.
//! - Enter sends, Shift+Enter inserts a newline.
//! - Click a card (weather, now playing) to send it back; hovering it keeps it up.

use std::time::Instant;

use gtk4::gdk::{Key, ModifierType, ScrollUnit};
use gtk4::prelude::*;
use gtk4::{
    EventControllerKey, EventControllerMotion, EventControllerScroll, EventControllerScrollFlags,
    GestureClick, GestureDrag, PickFlags, PropagationPhase, glib,
};

use crate::app::App;
use crate::input::set_class;
use crate::layout;
use crate::mascot;
use crate::card::CardKind;
use crate::state::Card;

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
    connect_card(app);
}

fn connect_map(app: &'static App) {
    app.ui.window.connect_map(move |_| {
        let mut s = app.state.borrow_mut();
        s.last_input_region = None;
        app.ui
            .update_input_region(s.chat_open, s.locked, s.is_moving());
    });
}

fn connect_scrolling(app: &'static App) {
    // Over the message log, capture the scroll before the log's own ScrolledWindow sees
    // it: every scroll goes through ChatLog, so nothing fights its stick-to-bottom
    // easing. Elsewhere on the panel (the input pill), whatever isn't used by the input
    // itself bubbles up and scrolls the log too.
    app.ui
        .chat
        .scrolled
        .add_controller(log_scroller(app, PropagationPhase::Capture));
    app.ui
        .chat_panel
        .add_controller(log_scroller(app, PropagationPhase::Bubble));

    // Keyboard scrolling and Esc-to-leave only in glass mode; outside it, Esc in the
    // message field just lets go of the keyboard
    let keys = EventControllerKey::new();
    keys.connect_key_pressed(move |_, keyval, _, _| {
        if !app.state.borrow().locked {
            // Done typing: let go of the keyboard so the chat can close again. (Not while
            // borrowing the state: moving the focus emits signals right away.)
            if keyval == Key::Escape && app.ui.input.focus.contains_focus() {
                GtkWindowExt::set_focus(&app.ui.window, None::<&gtk4::Widget>);
                return glib::Propagation::Stop;
            }
            return glib::Propagation::Proceed;
        }
        let mut s = app.state.borrow_mut();
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

/// Scrolls the message log while it's open: a touchpad moves it 1:1 and keeps coasting
/// when the fingers lift; a mouse wheel and the keyboard ease in steps.
fn log_scroller(app: &'static App, phase: PropagationPhase) -> EventControllerScroll {
    let scroll = EventControllerScroll::new(
        EventControllerScrollFlags::VERTICAL | EventControllerScrollFlags::KINETIC,
    );
    scroll.set_propagation_phase(phase);
    scroll.connect_scroll(move |controller, _dx, dy| {
        let s = app.state.borrow();
        if !(s.locked || s.chat_open) {
            return glib::Propagation::Proceed;
        }
        if controller.unit() == ScrollUnit::Surface {
            app.ui.chat.scroll_direct(dy);
        } else {
            app.ui.chat.scroll_by(dy * WHEEL_STEP);
        }
        glib::Propagation::Stop
    });
    scroll.connect_decelerate(move |_, _vx, vy| {
        let s = app.state.borrow();
        if s.locked || s.chat_open {
            app.ui.chat.fling(vy);
        }
    });
    scroll
}

/// Only the mascot body pushes the messages out. Once they're out, hovering the message
/// panel keeps them in view. A short grace period covers the gap between the two.
fn connect_hover(app: &'static App) {
    let on_body = move |x: f64, y: f64| {
        let m = &app.ui.mascot;
        mascot::body_contains(m.width() as f64, m.height() as f64, x, y)
    };
    let mascot_hover = move |hovered: bool| {
        app.with_state(move |s| s.set_mascot_hover(hovered, Instant::now()));
    };
    let panel_hover = move |hovered: bool| {
        app.with_state(move |s| s.set_panel_hover(hovered, Instant::now()));
    };

    let mascot_motion = EventControllerMotion::new();
    mascot_motion.connect_enter(move |_, x, y| mascot_hover(on_body(x, y)));
    mascot_motion.connect_motion(move |_, x, y| mascot_hover(on_body(x, y)));
    mascot_motion.connect_leave(move |_| mascot_hover(false));
    app.ui.mascot.add_controller(mascot_motion);

    let panel_motion = EventControllerMotion::new();
    panel_motion.connect_enter(move |_, _, _| panel_hover(true));
    panel_motion.connect_leave(move |_| panel_hover(false));

    // The memory panel floats apart from the chat, so it keeps the chat open the same way
    let memory_motion = EventControllerMotion::new();
    let memory_hover = move |hovered: bool| {
        app.with_state(move |s| s.set_memory_hover(hovered, Instant::now()));
    };
    memory_motion.connect_enter(move |_, _, _| memory_hover(true));
    memory_motion.connect_leave(move |_| memory_hover(false));
    app.ui.memory.root.add_controller(memory_motion);
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
            .stage
            .pick(start_x, start_y, PickFlags::DEFAULT)
            .is_some_and(|w| w == mascot_widget);

        let mut guard = app.state.borrow_mut();
        let s = &mut *guard;
        if !hit_mascot {
            s.drag.active = false;
            return;
        }
        s.drag.begin(s.placement.anchor, (start_x, start_y));
        s.last_input_region = None;
        app.ui.update_input_region(s.chat_open, s.locked, true);
    });

    drag.connect_drag_update(move |gesture, dx, dy| {
        let mut s = app.state.borrow_mut();
        if !s.drag.active {
            return;
        }
        // Past the edge over another monitor: he waits at the edge, and letting go
        // there moves him across (see drag_end)
        let pointer = gesture.start_point().map(|(x, y)| (x + dx, y + dy));
        s.drag.over_other_monitor = pointer.and_then(|p| app.ui.monitor_across(p)).is_some();
        set_class(&app.ui.root, "leaving", s.drag.over_other_monitor);
        // Only the mascot itself has to stay on screen; the shell rearranges around it
        let ui = &app.ui;
        s.placement.anchor =
            layout::clamp_mascot(s.drag.target(dx, dy), ui.window_size(), ui.body_in_mascot());
        app.layout_shell(&mut s);

        let (step_dx, step_dy) = s.drag.update(dx, dy);
        s.wobble.kick(step_dx, step_dy);
    });

    drag.connect_drag_end(move |gesture, dx, dy| {
        let mut s = app.state.borrow_mut();
        if s.drag.active {
            s.drag.active = false;
            s.last_input_region = None;
            app.ui.update_input_region(s.chat_open, s.locked, false);

            // Let go over another monitor: move there once the drag is fully over (doing
            // it mid-drag, while the compositor holds the button for this window, could
            // leave the overlay unmapped)
            let pointer = gesture.start_point().map(|(x, y)| (x + dx, y + dy));
            let target = pointer.and_then(|p| app.ui.monitor_across(p));
            if let Some((monitor, at, size)) = target.filter(|_| s.drag.over_other_monitor) {
                let grab = s.drag.grab;
                glib::idle_add_local_once(move || app.move_to_monitor(&monitor, at, size, grab));
            }
            s.drag.over_other_monitor = false;
            app.ui.root.remove_css_class("leaving");
        }
    });

    app.ui.stage.add_controller(drag);
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

    let compact_click = GestureClick::new();
    compact_click.connect_released(move |_, _, _, _| app.compact());
    app.ui.input.compact.area.add_controller(compact_click);

    for (effort, button) in &app.ui.input.effort.options {
        let effort = *effort;
        button.connect_clicked(move |_| app.set_effort(effort));
    }
}

fn connect_card(app: &'static App) {
    for kind in CardKind::ALL {
        let update_card = move |f: fn(&mut Card)| {
            app.with_state(move |s| {
                if let Some(card) = s.cards[kind.index()].as_mut() {
                    f(card);
                }
            });
        };
        let root = &app.ui.card_shell(kind).root;

        let click = GestureClick::new();
        click.connect_released(move |_, _, _, _| update_card(|card| card.dismiss(Instant::now())));
        root.add_controller(click);

        let motion = EventControllerMotion::new();
        motion.connect_enter(move |_, _, _| update_card(|card| card.hovered = true));
        motion.connect_leave(move |_| update_card(|card| card.hovered = false));
        root.add_controller(motion);
    }
}
