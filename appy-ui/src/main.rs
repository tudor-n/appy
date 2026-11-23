use std::cell::RefCell;
use std::f64::consts::{PI, TAU};
use std::rc::Rc;
use std::sync::{Arc, Mutex};
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
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::mpsc::{self, UnboundedSender};
use tokio_tungstenite::{connect_async, tungstenite::Message as WsMsg};

const WS_URL: &str = "ws://127.0.0.1:8001/ws";
const PILL_BRIDGE_ADDR: &str = "127.0.0.1:8002";
const VOICE_COOLDOWN: Duration = Duration::from_secs(8);
const MAX_MESSAGES: usize = 24;
const BLINK_DURATION_MS: f64 = 160.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum AppyState {
    Idle,
    Listening,
    Booting,
    Thinking,
    Compacting,
    Speaking,
}

impl AppyState {
    fn is_busy(self) -> bool {
        matches!(
            self,
            AppyState::Booting | AppyState::Thinking | AppyState::Compacting
        )
    }

    fn live_status_text(self) -> Option<&'static str> {
        match self {
            AppyState::Booting => Some("loading"),
            AppyState::Thinking => Some("thinking"),
            AppyState::Compacting => Some("compacting"),
            _ => None,
        }
    }

    fn done_status_text(self) -> &'static str {
        match self {
            AppyState::Booting => "loaded",
            AppyState::Thinking => "thought",
            AppyState::Compacting => "compacted",
            _ => "done",
        }
    }
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

#[derive(Debug, Clone, serde::Serialize)]
struct PillSyncState {
    state: AppyState,
    msg_count: usize,
    minimized: bool,
}

fn format_live_dots(word: &str, step: u128) -> String {
    match step % 3 {
        0 => format!("{word}.<span alpha=\"1\">..</span>"),
        1 => format!("{word}..<span alpha=\"1\">.</span>"),
        _ => format!("{word}..."),
    }
}

fn format_duration(d: Duration) -> String {
    let total_secs = d.as_secs();
    let mins = total_secs / 60;
    let secs = total_secs % 60;
    if mins > 0 {
        format!("{mins}m{secs}s")
    } else {
        format!("{secs}s")
    }
}

struct SharedState {
    state: AppyState,
    phase: f64,
    hovered: bool,
    dragging: bool,
    voice_cooldown_until: Option<Instant>,
    chat_open: bool,
    message_count: usize,
    margin_right: f64,
    margin_bottom: f64,
    pending_drag: Option<(f64, f64)>,
    drag_cooldown: u8,
    wobble_x: f64,
    wobble_y: f64,
    wobble_vx: f64,
    wobble_vy: f64,
    blink_scale: f64,
    blink_start: Option<Instant>,
    next_blink: Instant,
    blink_seed: u64,
    active_status_pill: Option<(AppyState, Label, Instant)>,
}

impl SharedState {
    fn is_busy(&self) -> bool {
        self.state.is_busy()
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

    fn next_blink_delay(&mut self) -> Duration {
        // Simple LCG for natural 2.8s–5.4s human blink intervals without extra crates
        self.blink_seed = self
            .blink_seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let ms = 2800 + ((self.blink_seed >> 33) % 2600);
        Duration::from_millis(ms)
    }

    fn finish_active_status(&mut self) {
        if let Some((prev_state, label, started)) = self.active_status_pill.take() {
            let elapsed = format_duration(started.elapsed());
            label.set_text(&format!("{} for {}", prev_state.done_status_text(), elapsed));
            if let Some(pill) = label.parent() {
                pill.remove_css_class("live");
            }
        }
    }
}

fn sync_pill(
    pill_sync: &Arc<Mutex<PillSyncState>>,
    state: AppyState,
    msg_count: usize,
    minimized: bool,
) {
    if let Ok(mut p) = pill_sync.lock() {
        p.state = state;
        p.msg_count = msg_count;
        p.minimized = minimized;
    }
}

fn spawn_background_services(
    ui_tx: async_channel::Sender<UiEvent>,
    mut ws_out_rx: mpsc::UnboundedReceiver<ClientEvent>,
    pill_sync: Arc<Mutex<PillSyncState>>,
) {
    std::thread::spawn(move || {
        let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
        rt.block_on(async move {
            // 1. Local HTTP bridge for the native KDE Plasma panel pill (127.0.0.1:8002)
            let ui_tx_http = ui_tx.clone();
            let pill_sync_http = pill_sync.clone();
            tokio::spawn(async move {
                if let Ok(listener) = TcpListener::bind(PILL_BRIDGE_ADDR).await {
                    while let Ok((mut stream, _)) = listener.accept().await {
                        let tx = ui_tx_http.clone();
                        let sync = pill_sync_http.clone();
                        tokio::spawn(async move {
                            let mut buf = [0u8; 512];
                            let n = stream.read(&mut buf).await.unwrap_or(0);
                            let req = String::from_utf8_lossy(&buf[..n]);

                            let body = if req.starts_with("POST /toggle") {
                                let _ = tx.send(UiEvent::ToggleOverlay).await;
                                r#"{"ok":true}"#.to_string()
                            } else if req.starts_with("POST /quit") {
                                let _ = tx.send(UiEvent::Quit).await;
                                r#"{"ok":true}"#.to_string()
                            } else {
                                let snapshot =
                                    sync.lock().map(|g| g.clone()).unwrap_or(PillSyncState {
                                        state: AppyState::Idle,
                                        msg_count: 0,
                                        minimized: true,
                                    });
                                serde_json::to_string(&snapshot).unwrap_or_else(|_| "{}".into())
                            };

                            let resp = format!(
                                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                                body.len(),
                                body
                            );
                            let _ = stream.write_all(resp.as_bytes()).await;
                        });
                    }
                }
            });

            // 2. Auto-reconnecting WebSocket to Python backend (127.0.0.1:8001)
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
    let r = r.min(w * 0.5).min(h * 0.5);
    cr.new_sub_path();
    cr.arc(x + w - r, y + r, r, -PI / 2.0, 0.0);
    cr.arc(x + w - r, y + h - r, r, 0.0, PI / 2.0);
    cr.arc(x + r, y + h - r, r, PI / 2.0, PI);
    cr.arc(x + r, y + r, r, PI, 3.0 * PI / 2.0);
    cr.close_path();
}

fn draw_mascot(
    cr: &gtk4::cairo::Context,
    canvas_w: f64,
    canvas_h: f64,
    state: AppyState,
    phase: f64,
    wobble_x: f64,
    wobble_y: f64,
    blink_scale: f64,
) {
    let _ = cr.save();

    // Center the 96x96 mascot inside the 120x120 padded canvas
    let w = 96.0;
    let h = 96.0;
    let pad_x = (canvas_w - w) * 0.5;
    let pad_y = (canvas_h - h) * 0.5;
    let cx = canvas_w * 0.5;
    let cy_pivot = canvas_h * 0.58;

    // KDE Wobbly Windows style shear + tilt + squash/stretch
    let shear_x = (wobble_x * 0.015).clamp(-0.30, 0.30);
    let rot = (wobble_x * 0.005).clamp(-0.16, 0.16);
    let stretch_y = (wobble_y * 0.009).clamp(-0.18, 0.18);
    let scale_x = 1.0 - stretch_y * 0.55;
    let scale_y = 1.0 + stretch_y;

    cr.translate(cx, cy_pivot);
    cr.rotate(rot);
    cr.transform(gtk4::cairo::Matrix::new(
        scale_x, 0.0, shear_x, scale_y, 0.0, 0.0,
    ));
    cr.translate(-cx + pad_x, -cy_pivot + pad_y);

    // 1. Squircle body
    draw_rounded_rect(cr, w * 0.10, h * 0.22, w * 0.80, h * 0.72, w * 0.24);
    cr.set_source_rgba(218.0 / 255.0, 216.0 / 255.0, 208.0 / 255.0, 0.92);
    let _ = cr.fill();

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
            let eye_h = (h * 0.24 * blink_scale).max(h * 0.025);
            let eye_w = w * 0.072;
            for &ex in &[w * 0.37, w * 0.63] {
                draw_rounded_rect(
                    cr,
                    ex - eye_w * 0.5,
                    cy - eye_h * 0.5,
                    eye_w,
                    eye_h,
                    eye_w * 0.5,
                );
                let _ = cr.fill();
            }
        }
    }

    let _ = cr.restore();
}

fn current_timestamp() -> String {
    glib::DateTime::now_local()
        .ok()
        .and_then(|dt| dt.format("%H:%M").ok())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "now".into())
}

fn trim_history(messages_box: &GtkBox) {
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
}

fn scroll_to_bottom(scrolled: &ScrolledWindow) {
    let scrolled_clone = scrolled.clone();
    glib::idle_add_local_once(move || {
        let adj = scrolled_clone.vadjustment();
        adj.set_value(adj.upper());
    });
}

fn append_message_bubble(
    messages_box: &GtkBox,
    scrolled: &ScrolledWindow,
    text: &str,
    from_user: bool,
) {
    trim_history(messages_box);

    let bubble = GtkBox::new(Orientation::Vertical, 2);
    bubble.add_css_class("bubble");
    bubble.add_css_class(if from_user {
        "bubble-user"
    } else {
        "bubble-appy"
    });
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

    scroll_to_bottom(scrolled);
}

fn append_status_pill(
    messages_box: &GtkBox,
    scrolled: &ScrolledWindow,
    text: &str,
) -> Label {
    trim_history(messages_box);

    let pill = GtkBox::new(Orientation::Horizontal, 0);
    pill.add_css_class("status-pill");
    pill.add_css_class("live");
    pill.set_halign(Align::Start);

    let label = Label::new(None);
    label.set_markup(text);
    label.add_css_class("status-pill-text");
    pill.append(&label);
    messages_box.append(&pill);

    let pill_anim = pill.clone();
    glib::idle_add_local_once(move || {
        pill_anim.add_css_class("visible");
    });

    scroll_to_bottom(scrolled);
    label
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
                font-family: "SF Pro Text", "Inter", sans-serif;
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

            /* Borderless solid-black comic / WhatsApp bubbles */
            .bubble {
                background-color: #000000;
                border: none;
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
                font-weight: 400;
                letter-spacing: -0.01em;
            }
            .bubble-time {
                color: #8e8e93;
                font-size: 10px;
                font-weight: 500;
                margin-top: 2px;
            }

            /* White status pills for thinking / loading / compacting -> thought / loaded / compacted */
            .status-pill {
                background-color: #ffffff;
                border: none;
                border-radius: 999px;
                padding: 4px 11px;
                opacity: 0;
                transform: translateY(8px) scale(0.94);
                transition:
                    transform 200ms cubic-bezier(0.22, 1.15, 0.36, 1.0),
                    opacity 150ms ease;
            }
            .status-pill.visible {
                opacity: 0.88;
                transform: translateY(0px) scale(1.0);
            }
            .status-pill.visible.live {
                opacity: 1.0;
            }
            .status-pill-text {
                color: #000000;
                font-size: 11px;
                font-weight: 600;
                letter-spacing: -0.01em;
            }

            /* Full-width borderless gray pill input bar */
            .input-bubble {
                background-color: #242626;
                border: none;
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
                letter-spacing: -0.01em;
                caret-color: #ffffff;
            }
            entry.chat-input placeholder {
                color: #8e8e93;
            }
            "#,
        );
        gtk4::style_context_add_provider_for_display(
            &Display::default().expect("display"),
            &css,
            STYLE_PROVIDER_PRIORITY_APPLICATION,
        );

        let wobble_css = CssProvider::new();
        gtk4::style_context_add_provider_for_display(
            &Display::default().expect("display"),
            &wobble_css,
            STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        );

        let state = Rc::new(RefCell::new(SharedState {
            state: AppyState::Idle,
            phase: 0.0,
            hovered: false,
            dragging: false,
            voice_cooldown_until: None,
            chat_open: false,
            message_count: 0,
            margin_right: 24.0,
            margin_bottom: 56.0,
            pending_drag: None,
            drag_cooldown: 0,
            wobble_x: 0.0,
            wobble_y: 0.0,
            wobble_vx: 0.0,
            wobble_vy: 0.0,
            blink_scale: 1.0,
            blink_start: None,
            next_blink: Instant::now() + Duration::from_millis(3200),
            blink_seed: 0x517cc1b727220a95,
            active_status_pill: None,
        }));

        let pill_sync = Arc::new(Mutex::new(PillSyncState {
            state: AppyState::Idle,
            msg_count: 0,
            minimized: true,
        }));

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
        mascot.set_content_width(120);
        mascot.set_content_height(120);
        mascot.set_valign(Align::End);
        let state_draw = state.clone();
        mascot.set_draw_func(move |_area, cr, width, height| {
            let st = state_draw.borrow();
            draw_mascot(
                cr,
                width as f64,
                height as f64,
                st.state,
                st.phase,
                st.wobble_x,
                st.wobble_y,
                st.blink_scale,
            );
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

        let chat_wobble_box = GtkBox::new(Orientation::Vertical, 8);
        chat_wobble_box.add_css_class("chat-wobble");
        chat_wobble_box.append(&scrolled);
        chat_wobble_box.append(&input_bubble);

        let chat_panel = GtkBox::new(Orientation::Vertical, 0);
        chat_panel.add_css_class("chat-panel");
        chat_panel.set_width_request(290);
        chat_panel.set_valign(Align::End);
        chat_panel.set_sensitive(false);
        chat_panel.append(&chat_wobble_box);

        let root = GtkBox::new(Orientation::Horizontal, 12);
        root.set_valign(Align::End);
        root.set_margin_end(16);
        root.set_margin_top(12);
        root.set_margin_bottom(8);
        root.append(&mascot);
        root.append(&chat_panel);
        window.set_child(Some(&root));

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

        // Double-click mascot -> minimize into native KDE panel pill
        let click = GestureClick::new();
        let win_min = window.clone();
        let st_min = state.clone();
        let sync_min = pill_sync.clone();
        click.connect_pressed(move |_, n_press, _, _| {
            if n_press == 2 {
                win_min.set_visible(false);
                let s = st_min.borrow();
                sync_pill(&sync_min, s.state, s.message_count, true);
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

        // Wire background services (Panel Pill Bridge + WebSocket)
        let (ui_tx, ui_rx) = async_channel::unbounded::<UiEvent>();
        let (ws_out_tx, ws_out_rx) = mpsc::unbounded_channel::<ClientEvent>();
        spawn_background_services(ui_tx, ws_out_rx, pill_sync.clone());

        // Submitting text from bottom bar
        let st_sub = state.clone();
        let win_sub = window.clone();
        let sync_sub = pill_sync.clone();
        let msgs_sub = messages_box.clone();
        let scroll_sub = scrolled.clone();
        let mascot_sub = mascot.clone();
        let ws_tx_sub: UnboundedSender<ClientEvent> = ws_out_tx.clone();
        input_entry.connect_activate(move |entry| {
            let txt = entry.text().trim().to_string();
            if !txt.is_empty() {
                append_message_bubble(&msgs_sub, &scroll_sub, &txt, true);
                let _ = ws_tx_sub.send(ClientEvent::UserText { text: txt });
                entry.set_text("");

                let mut s = st_sub.borrow_mut();
                s.finish_active_status();
                let status_label =
                    append_status_pill(&msgs_sub, &scroll_sub, &format_live_dots("thinking", 0));
                s.active_status_pill = Some((AppyState::Thinking, status_label, Instant::now()));
                s.message_count += 1;
                s.state = AppyState::Thinking;
                sync_pill(&sync_sub, s.state, s.message_count, !win_sub.is_visible());
                mascot_sub.queue_draw();
            }
        });

        // Handle Panel Pill and WebSocket events
        let st_ev = state.clone();
        let win_ev = window.clone();
        let sync_ev = pill_sync.clone();
        let app_ev = app.clone();
        let input_ev = input_entry.clone();
        let msgs_ev = messages_box.clone();
        let scroll_ev = scrolled.clone();
        let mascot_ev = mascot.clone();
        glib::spawn_future_local(async move {
            while let Ok(ev) = ui_rx.recv().await {
                match ev {
                    UiEvent::ToggleOverlay => {
                        let show_main = !win_ev.is_visible();
                        win_ev.set_visible(show_main);
                        let s = st_ev.borrow();
                        sync_pill(&sync_ev, s.state, s.message_count, !show_main);
                    }
                    UiEvent::Quit => app_ev.quit(),
                    UiEvent::Backend(bev) => {
                        let mut s = st_ev.borrow_mut();
                        s.trigger_voice_cooldown();
                        match bev {
                            BackendEvent::Wake => {
                                s.finish_active_status();
                                s.state = AppyState::Listening;
                            }
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
                            BackendEvent::Status { state: next_state, label } => {
                                let already_same_busy = s
                                    .active_status_pill
                                    .as_ref()
                                    .map(|(st, _, _)| *st == next_state)
                                    .unwrap_or(false);

                                if next_state.is_busy() {
                                    if !already_same_busy {
                                        s.finish_active_status();
                                        let text = next_state
                                            .live_status_text()
                                            .unwrap_or(label.as_str());
                                        let lbl = append_status_pill(
                                            &msgs_ev,
                                            &scroll_ev,
                                            &format_live_dots(text, 0),
                                        );
                                        s.active_status_pill =
                                            Some((next_state, lbl, Instant::now()));
                                    }
                                } else {
                                    s.finish_active_status();
                                }
                                s.state = next_state;
                            }
                            BackendEvent::Response { text } => {
                                s.finish_active_status();
                                s.state = AppyState::Speaking;
                                append_message_bubble(&msgs_ev, &scroll_ev, &text, false);
                                s.message_count += 1;
                            }
                            BackendEvent::OpenTool { tool, payload } => {
                                println!("OpenTool requested: {tool} -> {payload}");
                            }
                        }
                        sync_pill(&sync_ev, s.state, s.message_count, !win_ev.is_visible());
                        mascot_ev.queue_draw();
                    }
                }
            }
        });

        // 60fps animation, blinking, smooth drag + wobbly spring physics, & chat sync
        let st_tick = state.clone();
        let win_tick = window.clone();
        let sync_tick = pill_sync.clone();
        let mascot_tick = mascot.clone();
        let panel_tick = chat_panel.clone();
        let focus_tick = input_focus.clone();
        let wobble_css_tick = wobble_css.clone();
        let mut was_wobbling = false;
        glib::timeout_add_local(Duration::from_millis(16), move || {
            let mut s = st_tick.borrow_mut();
            let now = Instant::now();

            // 1. Smooth drag commit with Wayland cooldown
            if s.drag_cooldown > 0 {
                s.drag_cooldown -= 1;
                s.pending_drag = None;
            } else if let Some((dx, dy)) = s.pending_drag.take() {
                if dx.abs() >= 0.5 || dy.abs() >= 0.5 {
                    let prev_r = s.margin_right.round() as i32;
                    let prev_b = s.margin_bottom.round() as i32;

                    s.margin_right = (s.margin_right - dx).max(0.0);
                    s.margin_bottom = (s.margin_bottom - dy).max(0.0);

                    let next_r = s.margin_right.round() as i32;
                    let next_b = s.margin_bottom.round() as i32;

                    if next_r != prev_r || next_b != prev_b {
                        win_tick.set_margin(Edge::Right, next_r);
                        win_tick.set_margin(Edge::Bottom, next_b);
                        s.drag_cooldown = 1;
                    }

                    s.wobble_vx = (s.wobble_vx - dx * 0.55).clamp(-35.0, 35.0);
                    s.wobble_vy = (s.wobble_vy + dy * 0.55).clamp(-35.0, 35.0);
                }
            }

            // 2. Spring-damper wobble physics
            s.wobble_vx = (s.wobble_vx - s.wobble_x * 0.22) * 0.78;
            s.wobble_vy = (s.wobble_vy - s.wobble_y * 0.22) * 0.78;
            s.wobble_x += s.wobble_vx;
            s.wobble_y += s.wobble_vy;

            let is_wobbling = s.wobble_x.abs() > 0.05
                || s.wobble_y.abs() > 0.05
                || s.wobble_vx.abs() > 0.05
                || s.wobble_vy.abs() > 0.05;

            if !is_wobbling {
                s.wobble_x = 0.0;
                s.wobble_y = 0.0;
                s.wobble_vx = 0.0;
                s.wobble_vy = 0.0;
            }

            if is_wobbling {
                let tx = (s.wobble_x * 0.45).clamp(-14.0, 14.0);
                let ty = (s.wobble_y * 0.35).clamp(-10.0, 10.0);
                let skew_x = (s.wobble_x * 0.35).clamp(-8.0, 8.0);
                let rot = (s.wobble_x * 0.12).clamp(-3.5, 3.5);
                wobble_css_tick.load_from_data(&format!(
                    ".chat-wobble {{ transform: translate({tx:.2}px, {ty:.2}px) skewX({skew_x:.2}deg) rotate({rot:.2}deg); }}"
                ));
                was_wobbling = true;
            } else if was_wobbling {
                wobble_css_tick.load_from_data(".chat-wobble { transform: none; }");
                was_wobbling = false;
            }

            // 3. Natural human blinking (Idle state)
            let mut needs_blink_draw = false;
            if s.state == AppyState::Idle {
                if let Some(start) = s.blink_start {
                    let elapsed = now.duration_since(start).as_secs_f64() * 1000.0;
                    if elapsed >= BLINK_DURATION_MS {
                        s.blink_scale = 1.0;
                        s.blink_start = None;
                        let delay = s.next_blink_delay();
                        s.next_blink = now + delay;
                    } else {
                        let t = elapsed / BLINK_DURATION_MS;
                        // Smooth sine close & open
                        s.blink_scale = 1.0 - (t * PI).sin();
                    }
                    needs_blink_draw = true;
                } else if now >= s.next_blink {
                    s.blink_start = Some(now);
                }
            } else if s.blink_start.is_some() || (s.blink_scale - 1.0).abs() > f64::EPSILON {
                s.blink_start = None;
                s.blink_scale = 1.0;
                needs_blink_draw = true;
            }

            // Animate live status pill dots (. -> .. -> ...) without resizing the pill
            if let Some((st, ref label, started)) = s.active_status_pill {
                if let Some(live_word) = st.live_status_text() {
                    let step = started.elapsed().as_millis() / 400;
                    let next_markup = format_live_dots(live_word, step);
                    if label.label() != next_markup {
                        label.set_markup(&next_markup);
                    }
                }
            }

            // 4. Chat panel open/close sync
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

            // 5. Mascot redraws
            if s.is_busy() {
                s.phase = (s.phase + 0.14) % TAU;
                mascot_tick.queue_draw();
            } else if is_wobbling || needs_blink_draw {
                mascot_tick.queue_draw();
            } else if s.state == AppyState::Speaking && !show {
                s.state = AppyState::Idle;
                sync_pill(&sync_tick, s.state, s.message_count, !win_tick.is_visible());
                mascot_tick.queue_draw();
            }

            glib::ControlFlow::Continue
        });

        // Start minimized in the KDE panel pill
        window.set_visible(false);
    });

    app.run();
}