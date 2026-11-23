use std::cell::RefCell;
use std::f64::consts::{PI, TAU};
use std::rc::Rc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use gtk4::gdk::Display;
use gtk4::glib;
use gtk4::prelude::*;
use gtk4::{
    Align, Application, ApplicationWindow, Box as GtkBox, CssProvider, DrawingArea, Entry,
    EventControllerFocus, EventControllerMotion, GestureClick, GestureDrag, Label, Orientation,
    PolicyType, ScrolledWindow, STYLE_PROVIDER_PRIORITY_APPLICATION,
};
use gtk4_layer_shell::{Edge, KeyboardMode, Layer, LayerShell};
use ksni::{Tray, TrayMethods};
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio_tungstenite::{connect_async, tungstenite::Message as WsMsg};

const WS_URL: &str = "ws://127.0.0.1:8001/ws";
const VOICE_COOLDOWN: Duration = Duration::from_secs(8);
const MAX_MESSAGES: usize = 20;

// Adjust this to position the minimized pill in the gap between your taskbar apps and tray
const PILL_MARGIN_RIGHT: i32 = 550;
const PILL_MARGIN_BOTTOM: i32 = 4;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum AppyState {
    Idle,
    Listening,
    Booting,
    Thinking,
    Compacting,
    Speaking,
}

#[allow(dead_code)]
#[derive(Debug, Clone, serde::Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum BackendEvent {
    Wake,
    Transcript { text: String, is_final: bool },
    Status { state: AppyState, label: String },
    Response { text: String },
    OpenTool { tool: String, payload: serde_json::Value },
}

#[derive(Debug, serde::Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ClientEvent {
    UserText { text: String },
}

#[derive(Debug)]
enum UiEvent {
    ToggleOverlay,
    Quit,
    Backend(BackendEvent),
}

struct SharedState {
    state: AppyState,
    phase: f64,
    hovered: bool,
    dragging: bool,
    voice_cooldown_until: Option<Instant>,
    chat_open: bool,
    message_count: usize,
    margin_right: i32,
    margin_bottom: i32,
    pending_drag: Option<(f64, f64)>,
    drag_cooldown: u8,
}

impl SharedState {
    fn is_busy(&self) -> bool {
        matches!(
            self.state,
            AppyState::Booting | AppyState::Thinking | AppyState::Compacting
        )
    }

    fn trigger_voice_cooldown(&mut self) {
        self.voice_cooldown_until = Some(Instant::now() + VOICE_COOLDOWN);
    }

    fn should_show_chat(&self, input_selected: bool) -> bool {
        if self.hovered || self.dragging || input_selected || self.is_busy() {
            return true;
        }
        self.voice_cooldown_until
            .map(|until| Instant::now() < until)
            .unwrap_or(false)
    }
}

struct AppyTray {
    tx: async_channel::Sender<UiEvent>,
    icon_argb: Vec<u8>,
}

impl Tray for AppyTray {
    fn id(&self) -> String {
        "appy-lab-os".into()
    }
    fn title(&self) -> String {
        "Appy".into()
    }
    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        vec![ksni::Icon {
            width: 32,
            height: 32,
            data: self.icon_argb.clone(),
        }]
    }
    fn activate(&mut self, _x: i32, _y: i32) {
        let _ = self.tx.send_blocking(UiEvent::ToggleOverlay);
    }
    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        use ksni::menu::*;
        vec![
            StandardItem {
                label: "Summon / Hide Appy".into(),
                activate: Box::new(|this: &mut Self| {
                    let _ = this.tx.send_blocking(UiEvent::ToggleOverlay);
                }),
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: "Quit".into(),
                activate: Box::new(|this: &mut Self| {
                    let _ = this.tx.send_blocking(UiEvent::Quit);
                }),
                ..Default::default()
            }
            .into(),
        ]
    }
}

fn build_tray_icon_argb() -> Vec<u8> {
    let mut data = vec![0u8; 32 * 32 * 4];
    for y in 0..32 {
        for x in 0..32 {
            let idx = (y * 32 + x) * 4;
            let in_body = (4..28).contains(&x) && (7..29).contains(&y);
            let is_eye = ((10..13).contains(&x) || (19..22).contains(&x)) && (15..22).contains(&y);
            let is_leaf = (16..22).contains(&x) && (2..7).contains(&y);
            if is_eye {
                data[idx..idx + 4].copy_from_slice(&[255, 20, 20, 24]);
            } else if in_body {
                data[idx..idx + 4].copy_from_slice(&[240, 218, 216, 208]);
            } else if is_leaf {
                data[idx..idx + 4].copy_from_slice(&[250, 251, 242, 242]);
            }
        }
    }
    data
}

fn spawn_background_services(
    ui_tx: async_channel::Sender<UiEvent>,
    mut ws_out_rx: mpsc::UnboundedReceiver<ClientEvent>,
) {
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
        rt.block_on(async move {
            let tray = AppyTray {
                tx: ui_tx.clone(),
                icon_argb: build_tray_icon_argb(),
            };
            let _tray_handle = tray.spawn().await.expect("failed to register KDE tray");

            loop {
                match connect_async(WS_URL).await {
                    Ok((ws_stream, _)) => {
                        let (mut write, mut read) = ws_stream.split();
                        loop {
                            tokio::select! {
                                Some(Ok(msg)) = read.next() => {
                                    if let WsMsg::Text(txt) = msg {
                                        if let Ok(ev) = serde_json::from_str::<BackendEvent>(&txt) {
                                            let _ = ui_tx.send(UiEvent::Backend(ev)).await;
                                        }
                                    }
                                }
                                Some(client_ev) = ws_out_rx.recv() => {
                                    if let Ok(json) = serde_json::to_string(&client_ev) {
                                        if write.send(WsMsg::Text(json.into())).await.is_err() {
                                            break;
                                        }
                                    }
                                }
                                else => break,
                            }
                        }
                    }
                    Err(_) => tokio::time::sleep(Duration::from_secs(2)).await,
                }
            }
        });
    });
}

fn draw_rounded_rect(cr: &gtk4::cairo::Context, x: f64, y: f64, w: f64, h: f64, r: f64) {
    cr.new_sub_path();
    cr.arc(x + w - r, y + r, r, -PI / 2.0, 0.0);
    cr.arc(x + w - r, y + h - r, r, 0.0, PI / 2.0);
    cr.arc(x + r, y + h - r, r, PI / 2.0, PI);
    cr.arc(x + r, y + r, r, PI, 3.0 * PI / 2.0);
    cr.close_path();
}

fn draw_mascot(cr: &gtk4::cairo::Context, w: f64, h: f64, state: AppyState, phase: f64) {
    // 1. Squircle body
    draw_rounded_rect(cr, w * 0.10, h * 0.22, w * 0.80, h * 0.72, w * 0.24);
    cr.set_source_rgba(218.0 / 255.0, 216.0 / 255.0, 208.0 / 255.0, 0.92);
    let _ = cr.fill_preserve();
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.28);
    cr.set_line_width((w * 0.016).max(1.0));
    let _ = cr.stroke();

    // 2. Blush-white leaf
    cr.move_to(w * 0.50, h * 0.23);
    cr.curve_to(w * 0.52, h * 0.06, w * 0.58, h * 0.05, w * 0.67, h * 0.04);
    cr.curve_to(w * 0.66, h * 0.16, w * 0.60, h * 0.21, w * 0.50, h * 0.23);
    cr.close_path();
    cr.set_source_rgba(251.0 / 255.0, 242.0 / 255.0, 242.0 / 255.0, 0.94);
    let _ = cr.fill();

    // 3. Face
    let cy = h * 0.59;
    cr.set_source_rgb(20.0 / 255.0, 20.0 / 255.0, 24.0 / 255.0);
    match state {
        AppyState::Booting | AppyState::Thinking | AppyState::Compacting => {
            let spacing = w * 0.14;
            let base_x = w * 0.50 - spacing;
            for i in 0..3 {
                let offset = (phase - (i as f64 * 0.7)).sin();
                let y = cy - offset * (h * 0.045);
                let r = w * (0.042 + 0.008 * offset.max(0.0));
                cr.arc(base_x + i as f64 * spacing, y, r, 0.0, TAU);
                let _ = cr.fill();
            }
        }
        AppyState::Listening | AppyState::Speaking => {
            cr.set_line_width((w * 0.06).max(1.6));
            cr.set_line_cap(gtk4::cairo::LineCap::Round);
            for &ex in &[w * 0.36, w * 0.64] {
                cr.move_to(ex - w * 0.07, cy + h * 0.03);
                cr.curve_to(
                    ex - w * 0.02,
                    cy - h * 0.06,
                    ex + w * 0.02,
                    cy - h * 0.06,
                    ex + w * 0.07,
                    cy + h * 0.03,
                );
                let _ = cr.stroke();
            }
        }
        AppyState::Idle => {
            for &ex in &[w * 0.37, w * 0.63] {
                draw_rounded_rect(
                    cr,
                    ex - w * 0.038,
                    cy - h * 0.08,
                    w * 0.076,
                    h * 0.16,
                    w * 0.038,
                );
                let _ = cr.fill();
            }
        }
    }
}

fn current_timestamp() -> String {
    glib::DateTime::now_local()
        .ok()
        .and_then(|dt| dt.format("%H:%M").ok())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "now".into())
}

fn append_message_bubble(
    messages_box: &GtkBox,
    scrolled: &ScrolledWindow,
    text: &str,
    from_user: bool,
) {
    let mut count = 0;
    let mut child = messages_box.first_child();
    while let Some(c) = child {
        count += 1;
        child = c.next_sibling();
    }
    if count >= MAX_MESSAGES {
        if let Some(first) = messages_box.first_child() {
            messages_box.remove(&first);
        }
    }

    let bubble = GtkBox::new(Orientation::Vertical, 2);
    bubble.add_css_class("bubble");
    bubble.add_css_class(if from_user { "bubble-user" } else { "bubble-appy" });
    bubble.set_halign(if from_user { Align::End } else { Align::Start });

    let msg_label = Label::builder()
        .label(text)
        .xalign(0.0)
        .wrap(true)
        .max_width_chars(30)
        .build();
    msg_label.add_css_class("bubble-text");

    let time_label = Label::builder()
        .label(&current_timestamp())
        .xalign(1.0)
        .halign(Align::End)
        .build();
    time_label.add_css_class("bubble-time");

    bubble.append(&msg_label);
    bubble.append(&time_label);
    messages_box.append(&bubble);

    let bubble_anim = bubble.clone();
    glib::idle_add_local_once(move || {
        bubble_anim.add_css_class("visible");
    });

    let scrolled_clone = scrolled.clone();
    glib::idle_add_local_once(move || {
        let adj = scrolled_clone.vadjustment();
        adj.set_value(adj.upper());
    });
}

fn main() {
    let app = Application::builder()
        .application_id("dev.appy.overlay")
        .build();

    app.connect_activate(|app| {
        let css = CssProvider::new();
        css.load_from_data(
            r#"
            window {
                background-color: transparent;
                background: none;
            }

            /* macOS-style spring container */
            .chat-panel {
                opacity: 0;
                transform: translateX(-28px) scale(0.94);
                transition:
                    transform 260ms cubic-bezier(0.22, 1.15, 0.36, 1.0),
                    opacity 170ms ease;
            }
            .chat-panel.open {
                opacity: 1;
                transform: translateX(0px) scale(1.0);
            }

            scrolledwindow, viewport {
                background: transparent;
                border: none;
            }

            /* Classic comic / WhatsApp solid-black bubbles */
            .bubble {
                background-color: #000000;
                border: 1px solid rgba(255, 255, 255, 0.14);
                padding: 8px 12px 6px 12px;
                opacity: 0;
                transform: translateY(10px) scale(0.95);
                transition:
                    transform 220ms cubic-bezier(0.22, 1.15, 0.36, 1.0),
                    opacity 160ms ease;
            }
            .bubble.visible {
                opacity: 1;
                transform: translateY(0px) scale(1.0);
            }
            .bubble-appy {
                border-radius: 16px 16px 16px 2px;
            }
            .bubble-user {
                border-radius: 16px 16px 2px 16px;
            }

            .bubble-text {
                color: #ffffff;
                font-size: 13px;
            }
            .bubble-time {
                color: #8e8e93;
                font-size: 10px;
                margin-top: 2px;
            }

            /* Full-width gray pill input bar */
            .input-bubble {
                background-color: #242626;
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 22px;
                padding: 6px 14px;
            }
            entry.chat-input {
                background: transparent;
                color: #ffffff;
                border: none;
                box-shadow: none;
                outline: none;
                min-height: 24px;
                font-size: 13px;
                caret-color: #ffffff;
            }
            entry.chat-input placeholder {
                color: #8e8e93;
            }

            /* Minimized taskbar pill */
            .mini-pill {
                background-color: #000000;
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 999px;
                padding: 3px 12px 3px 6px;
            }
            .mini-pill-label {
                color: #ffffff;
                font-size: 12px;
                font-weight: 600;
            }
            "#,
        );
        gtk4::style_context_add_provider_for_display(
            &Display::default().expect("display"),
            &css,
            STYLE_PROVIDER_PRIORITY_APPLICATION,
        );

        let state = Rc::new(RefCell::new(SharedState {
            state: AppyState::Idle,
            phase: 0.0,
            hovered: false,
            dragging: false,
            voice_cooldown_until: None,
            chat_open: false,
            message_count: 0,
            margin_right: 24,
            margin_bottom: 56,
            pending_drag: None,
            drag_cooldown: 0,
        }));

        // --- 1. Main Floating Overlay Window ---
        let window = ApplicationWindow::builder()
            .application(app)
            .decorated(false)
            .resizable(false)
            .build();

        window.init_layer_shell();
        window.set_namespace(Some("appy-overlay"));
        window.set_layer(Layer::Top);
        window.set_anchor(Edge::Bottom, true);
        window.set_anchor(Edge::Right, true);
        window.set_margin(Edge::Right, 24);
        window.set_margin(Edge::Bottom, 56);
        window.set_keyboard_mode(KeyboardMode::OnDemand);

        let mascot = DrawingArea::new();
        mascot.set_content_width(96);
        mascot.set_content_height(96);
        mascot.set_valign(Align::End);
        let state_draw = state.clone();
        mascot.set_draw_func(move |_area, cr, width, height| {
            let st = state_draw.borrow();
            draw_mascot(cr, width as f64, height as f64, st.state, st.phase);
        });

        let messages_box = GtkBox::new(Orientation::Vertical, 6);
        messages_box.set_valign(Align::End);

        let scrolled = ScrolledWindow::builder()
            .hscrollbar_policy(PolicyType::Never)
            .vscrollbar_policy(PolicyType::Automatic)
            .propagate_natural_height(true)
            .max_content_height(300)
            .width_request(290)
            .child(&messages_box)
            .build();

        let input_entry = Entry::builder()
            .placeholder_text("Type a message")
            .hexpand(true)
            .build();
        input_entry.add_css_class("chat-input");

        let input_focus = EventControllerFocus::new();
        input_entry.add_controller(input_focus.clone());

        let input_bubble = GtkBox::new(Orientation::Horizontal, 0);
        input_bubble.add_css_class("input-bubble");
        input_bubble.set_hexpand(true);
        input_bubble.set_halign(Align::Fill);
        input_bubble.append(&input_entry);

        let chat_panel = GtkBox::new(Orientation::Vertical, 8);
        chat_panel.add_css_class("chat-panel");
        chat_panel.set_width_request(290);
        chat_panel.set_valign(Align::End);
        chat_panel.set_sensitive(false);
        chat_panel.append(&scrolled);
        chat_panel.append(&input_bubble);

        let root = GtkBox::new(Orientation::Horizontal, 12);
        root.set_valign(Align::End);
        root.append(&mascot);
        root.append(&chat_panel);
        window.set_child(Some(&root));

        // --- 2. Minimized Taskbar Pill Window ---
        let pill_window = ApplicationWindow::builder()
            .application(app)
            .decorated(false)
            .resizable(false)
            .build();

        pill_window.init_layer_shell();
        pill_window.set_namespace(Some("appy-pill"));
        pill_window.set_layer(Layer::Overlay);
        pill_window.set_exclusive_zone(-1);
        pill_window.set_anchor(Edge::Bottom, true);
        pill_window.set_anchor(Edge::Right, true);
        pill_window.set_margin(Edge::Right, PILL_MARGIN_RIGHT);
        pill_window.set_margin(Edge::Bottom, PILL_MARGIN_BOTTOM);
        pill_window.set_keyboard_mode(KeyboardMode::None);

        let mini_mascot = DrawingArea::new();
        mini_mascot.set_content_width(28);
        mini_mascot.set_content_height(28);
        let state_mini = state.clone();
        mini_mascot.set_draw_func(move |_area, cr, width, height| {
            let st = state_mini.borrow();
            draw_mascot(cr, width as f64, height as f64, st.state, st.phase);
        });

        let pill_count_label = Label::builder().label("0 msg").build();
        pill_count_label.add_css_class("mini-pill-label");

        let mini_pill_box = GtkBox::new(Orientation::Horizontal, 8);
        mini_pill_box.add_css_class("mini-pill");
        mini_pill_box.set_valign(Align::Center);
        mini_pill_box.append(&mini_mascot);
        mini_pill_box.append(&pill_count_label);
        pill_window.set_child(Some(&mini_pill_box));

        // Click pill -> restore main overlay
        let pill_click = GestureClick::new();
        let win_restore = window.clone();
        let pill_hide = pill_window.clone();
        pill_click.connect_pressed(move |_, _, _, _| {
            pill_hide.set_visible(false);
            win_restore.set_visible(true);
        });
        mini_pill_box.add_controller(pill_click);

        // Hover on/off main overlay
        let motion = EventControllerMotion::new();
        let st_enter = state.clone();
        motion.connect_enter(move |_, _, _| {
            st_enter.borrow_mut().hovered = true;
        });
        let st_leave = state.clone();
        motion.connect_leave(move |_| {
            let mut s = st_leave.borrow_mut();
            s.hovered = false;
            s.voice_cooldown_until = None;
        });
        root.add_controller(motion);

        // Double-click mascot -> minimize into taskbar pill
        let click = GestureClick::new();
        let win_min = window.clone();
        let pill_show = pill_window.clone();
        click.connect_pressed(move |_, n_press, _, _| {
            if n_press == 2 {
                win_min.set_visible(false);
                pill_show.set_visible(true);
            }
        });
        mascot.add_controller(click);

        // Drag mascot (with Wayland cooldown)
        let drag = GestureDrag::new();
        let st_drag_start = state.clone();
        drag.connect_drag_begin(move |_, _, _| {
            let mut s = st_drag_start.borrow_mut();
            s.dragging = true;
            s.pending_drag = None;
        });
        let st_drag_upd = state.clone();
        drag.connect_drag_update(move |_, dx, dy| {
            let mut s = st_drag_upd.borrow_mut();
            if s.drag_cooldown == 0 {
                s.pending_drag = Some((dx, dy));
            }
        });
        let st_drag_end = state.clone();
        drag.connect_drag_end(move |_, _, _| {
            let mut s = st_drag_end.borrow_mut();
            s.dragging = false;
            s.pending_drag = None;
        });
        mascot.add_controller(drag);

        // Wire background services (Tray + WebSocket)
        let (ui_tx, ui_rx) = async_channel::unbounded::<UiEvent>();
        let (ws_out_tx, ws_out_rx) = mpsc::unbounded_channel::<ClientEvent>();
        spawn_background_services(ui_tx, ws_out_rx);

        // Submitting text from bottom bar
        let st_sub = state.clone();
        let msgs_sub = messages_box.clone();
        let scroll_sub = scrolled.clone();
        let mascot_sub = mascot.clone();
        let mini_sub = mini_mascot.clone();
        let count_sub = pill_count_label.clone();
        let ws_tx_sub: UnboundedSender<ClientEvent> = ws_out_tx.clone();
        input_entry.connect_activate(move |entry| {
            let txt = entry.text().trim().to_string();
            if !txt.is_empty() {
                append_message_bubble(&msgs_sub, &scroll_sub, &txt, true);
                let _ = ws_tx_sub.send(ClientEvent::UserText { text: txt });
                entry.set_text("");
                let mut s = st_sub.borrow_mut();
                s.message_count += 1;
                s.state = AppyState::Thinking;
                count_sub.set_text(&format!("{} msg", s.message_count));
                mascot_sub.queue_draw();
                mini_sub.queue_draw();
            }
        });

        // Handle Tray + WebSocket events
        let st_ev = state.clone();
        let win_ev = window.clone();
        let pill_ev = pill_window.clone();
        let app_ev = app.clone();
        let input_ev = input_entry.clone();
        let msgs_ev = messages_box.clone();
        let scroll_ev = scrolled.clone();
        let mascot_ev = mascot.clone();
        let mini_ev = mini_mascot.clone();
        let count_ev = pill_count_label.clone();
        glib::spawn_future_local(async move {
            while let Ok(ev) = ui_rx.recv().await {
                match ev {
                    UiEvent::ToggleOverlay => {
                        let show_main = !win_ev.is_visible();
                        win_ev.set_visible(show_main);
                        pill_ev.set_visible(!show_main);
                    }
                    UiEvent::Quit => app_ev.quit(),
                    UiEvent::Backend(bev) => {
                        let mut s = st_ev.borrow_mut();
                        s.trigger_voice_cooldown();
                        match bev {
                            BackendEvent::Wake => s.state = AppyState::Listening,
                            BackendEvent::Transcript { text, is_final } => {
                                s.state = AppyState::Listening;
                                if is_final {
                                    input_ev.set_text("");
                                    append_message_bubble(&msgs_ev, &scroll_ev, &text, true);
                                    s.message_count += 1;
                                } else {
                                    input_ev.set_text(&text);
                                }
                            }
                            BackendEvent::Status { state, label } => {
                                s.state = state;
                                if !label.is_empty() {
                                    append_message_bubble(&msgs_ev, &scroll_ev, &label, false);
                                    s.message_count += 1;
                                }
                            }
                            BackendEvent::Response { text } => {
                                s.state = AppyState::Speaking;
                                append_message_bubble(&msgs_ev, &scroll_ev, &text, false);
                                s.message_count += 1;
                            }
                            BackendEvent::OpenTool { tool, payload } => {
                                println!("OpenTool requested: {tool} -> {payload}");
                            }
                        }
                        count_ev.set_text(&format!("{} msg", s.message_count));
                        mascot_ev.queue_draw();
                        mini_ev.queue_draw();
                    }
                }
            }
        });

        // 60fps animation, drag commit with cooldown, & spring open/close sync
        let st_tick = state.clone();
        let win_tick = window.clone();
        let mascot_tick = mascot.clone();
        let mini_tick = mini_mascot.clone();
        let panel_tick = chat_panel.clone();
        let focus_tick = input_focus.clone();
        glib::timeout_add_local(Duration::from_millis(16), move || {
            let mut s = st_tick.borrow_mut();

            if s.drag_cooldown > 0 {
                s.drag_cooldown -= 1;
                s.pending_drag = None;
            } else if let Some((dx, dy)) = s.pending_drag.take() {
                let ix = dx.round() as i32;
                let iy = dy.round() as i32;
                if ix.abs() >= 2 || iy.abs() >= 2 {
                    s.margin_right = (s.margin_right - ix).max(0);
                    s.margin_bottom = (s.margin_bottom - iy).max(0);
                    win_tick.set_margin(Edge::Right, s.margin_right);
                    win_tick.set_margin(Edge::Bottom, s.margin_bottom);
                    s.drag_cooldown = 1;
                }
            }

            let input_selected = win_tick.is_active() && focus_tick.contains_focus();
            let show = s.should_show_chat(input_selected);
            if s.chat_open != show {
                s.chat_open = show;
                panel_tick.set_sensitive(show);
                if show {
                    panel_tick.add_css_class("open");
                } else {
                    panel_tick.remove_css_class("open");
                }
            }

            if s.is_busy() {
                s.phase = (s.phase + 0.14) % TAU;
                mascot_tick.queue_draw();
                mini_tick.queue_draw();
            } else if s.state == AppyState::Speaking && !show {
                s.state = AppyState::Idle;
                mascot_tick.queue_draw();
                mini_tick.queue_draw();
            }

            glib::ControlFlow::Continue
        });

        // Start minimized in the taskbar pill
        window.set_visible(false);
        pill_window.set_visible(true);
    });

    app.run();
}