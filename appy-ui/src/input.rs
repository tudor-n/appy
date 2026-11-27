//! The message input pill: a growing multi-line text field, the "compact now" ring
//! (which also shows how full the context is) and the effort picker.

use std::cell::Cell;
use std::f64::consts::{FRAC_PI_2, TAU};
use std::rc::Rc;
use std::time::{Duration, Instant};

use gtk4::cairo::Context;
use gtk4::prelude::*;
use gtk4::{
    Align, Box as GtkBox, Button, DrawingArea, EventControllerFocus, EventControllerMotion,
    Label, Orientation, Overlay, PolicyType,
    Revealer, RevealerTransitionType, ScrolledWindow, TextBuffer, TextView, WrapMode,
};

use crate::mascot;
use crate::protocol::Effort;
use crate::state::per_frame;

/// The input grows with its text up to this height, then scrolls.
const INPUT_MAX_HEIGHT: i32 = 132;

pub struct ChatInput {
    pub container: GtkBox,
    pub view: TextView,
    pub buffer: TextBuffer,
    pub focus: EventControllerFocus,
    pub compact: ContextRing,
    pub effort: EffortPicker,
}

impl ChatInput {
    pub fn new() -> Self {
        // Enter sends, Shift+Enter inserts a newline (wired up in controls.rs).
        let view = TextView::builder()
            .wrap_mode(WrapMode::WordChar)
            .accepts_tab(false)
            .hexpand(true)
            .top_margin(4)
            .bottom_margin(4)
            .left_margin(0)
            .right_margin(0)
            .build();
        view.add_css_class("chat-input");
        let buffer = view.buffer();

        let scroll = ScrolledWindow::builder()
            .hscrollbar_policy(PolicyType::Never)
            // External (not Automatic): Automatic reserves room for a scrollbar track,
            // which made the pill ~20px taller than one line and pushed the text off-centre.
            .vscrollbar_policy(PolicyType::External)
            .propagate_natural_height(true)
            .min_content_height(26)
            .max_content_height(INPUT_MAX_HEIGHT)
            .hexpand(true)
            .child(&view)
            .build();

        let placeholder = Label::builder()
            .label("Type a message")
            .xalign(0.0)
            .halign(Align::Start)
            .valign(Align::Center)
            .build();
        placeholder.add_css_class("chat-placeholder");
        placeholder.set_can_target(false);
        buffer.connect_changed({
            let placeholder = placeholder.clone();
            move |buf| placeholder.set_visible(buf.char_count() == 0)
        });

        let overlay = Overlay::new();
        overlay.set_hexpand(true);
        overlay.set_child(Some(&scroll));
        overlay.add_overlay(&placeholder);

        let focus = EventControllerFocus::new();
        view.add_controller(focus.clone());

        // "Compact now": a bare ring next to the effort chip, since both are about the
        // conversation rather than the message
        let compact = ContextRing::new();

        let effort = EffortPicker::new();

        let row = GtkBox::new(Orientation::Horizontal, 8);
        row.set_hexpand(true);
        row.append(&overlay);
        row.append(&compact.area);
        row.append(&effort.chip);

        let container = GtkBox::new(Orientation::Vertical, 0);
        container.add_css_class("input-bubble");
        container.set_hexpand(true);
        container.set_halign(Align::Fill);
        container.append(&effort.revealer);
        container.append(&row);

        Self {
            container,
            view,
            buffer,
            focus,
            compact,
            effort,
        }
    }

    /// The trimmed input text, or None if it's blank.
    pub fn text(&self) -> Option<String> {
        let (start, end) = self.buffer.bounds();
        let text = self.buffer.text(&start, &end, false).trim().to_string();
        (!text.is_empty()).then_some(text)
    }
}

/// A chip at the bottom-right of the input pill that expands a row of effort options.
pub struct EffortPicker {
    pub chip: Button,
    pub revealer: Revealer,
    pub options: Vec<(Effort, Button)>,
}

impl EffortPicker {
    fn new() -> Self {
        let chip = Button::builder()
            .label(Effort::Off.short())
            .valign(Align::End)
            .margin_bottom(3) // centres the chip on a single line, stays bottom-aligned when it grows
            .focus_on_click(false)
            .build();
        chip.add_css_class("effort-chip");
        chip.set_tooltip_text(Some("Reasoning effort"));

        let row = GtkBox::new(Orientation::Horizontal, 4);
        row.add_css_class("effort-row");
        let caption = Label::new(Some("effort"));
        caption.add_css_class("effort-caption");
        row.append(&caption);

        let options: Vec<(Effort, Button)> = Effort::ALL
            .into_iter()
            .map(|e| {
                let b = Button::builder()
                    .label(e.short())
                    .focus_on_click(false)
                    .build();
                b.add_css_class("effort-opt");
                b.set_tooltip_text(Some(e.tooltip()));
                row.append(&b);
                (e, b)
            })
            .collect();

        let revealer = Revealer::builder()
            .transition_type(RevealerTransitionType::SlideUp)
            .transition_duration(180)
            .reveal_child(false)
            .child(&row)
            .build();
        revealer.connect_reveal_child_notify({
            let chip = chip.clone();
            move |r| set_class(&chip, "open", r.reveals_child())
        });
        chip.connect_clicked({
            let revealer = revealer.clone();
            move |_| revealer.set_reveal_child(!revealer.reveals_child())
        });

        let picker = Self {
            chip,
            revealer,
            options,
        };
        picker.show(Effort::Off);
        picker
    }

    /// Reflect `effort` on the chip and highlight the matching option.
    pub fn show(&self, effort: Effort) {
        self.chip.set_label(effort.short());
        self.chip
            .set_tooltip_text(Some(&format!("Reasoning effort: {}", effort.tooltip())));
        set_class(&self.chip, "off", effort == Effort::Off);
        for (e, b) in &self.options {
            set_class(b, "active", *e == effort);
        }
    }

    pub fn collapse(&self) {
        self.revealer.set_reveal_child(false);
    }
}

pub fn set_class(widget: &impl IsA<gtk4::Widget>, class: &str, on: bool) {
    if on {
        widget.add_css_class(class);
    } else {
        widget.remove_css_class(class);
    }
}

/// The ring's diameter, the (larger, invisible) box it sits in so it's easy to hit, and
/// its line width.
const RING_SIZE: f64 = 14.0;
const RING_BOX: i32 = 20;
const RING_LINE: f64 = 1.5;
/// Per-frame easing of the fill toward a new context size (slow enough to see it drain
/// after a compaction).
const RING_EASE: f64 = 0.12;
/// A click with no answer from the backend stops looking busy after this long.
const PENDING_TIMEOUT: Duration = Duration::from_secs(30);

/// The ring's centre and radius inside a `w` x `h` box: RING_SIZE across, centred, with
/// the stroke inside that.
fn ring_geometry(w: f64, h: f64) -> (f64, f64, f64) {
    (w * 0.5, h * 0.5, (RING_SIZE - RING_LINE) * 0.5 - 0.5)
}

/// How full the ring is for `tokens` of a `limit` (where auto-compaction kicks in).
fn ring_fill(tokens: u64, limit: u64) -> f64 {
    (tokens as f64 / limit.max(1) as f64).clamp(0.0, 1.0)
}

fn ring_tooltip(fill: f64) -> String {
    format!(
        "Context {}% full. Click to compact the conversation",
        (fill * 100.0).round()
    )
}

#[derive(Default)]
struct RingModel {
    hovered: Cell<bool>,
    /// Where the fill is heading (0..=1); None until the backend reports the context.
    target: Cell<Option<f64>>,
    /// The fill as drawn, eased toward `target` every frame.
    shown: Cell<f64>,
    /// Clicked (or compacting) and waiting for the new context size: drawn lit.
    pending_since: Cell<Option<Instant>>,
}

/// "Compact now", doubling as a context meter: a faint track with an arc in Appy's own
/// white filling clockwise from 12 o'clock as the conversation grows toward the point
/// where it's compacted automatically. Drawn by hand rather than as a styled Button, so
/// the theme can't add button chrome around it. Clicks: controls.rs.
pub struct ContextRing {
    pub area: DrawingArea,
    model: Rc<RingModel>,
}

impl ContextRing {
    fn new() -> Self {
        let area = DrawingArea::builder()
            .content_width(RING_BOX)
            .content_height(RING_BOX)
            .valign(Align::Center)
            .build();
        area.set_tooltip_text(Some("Compact the conversation into a summary"));
        area.set_cursor_from_name(Some("pointer"));

        let model = Rc::new(RingModel::default());
        area.set_draw_func({
            let model = model.clone();
            move |_, cr: &Context, w, h| draw_ring(cr, w as f64, h as f64, &model)
        });

        let motion = EventControllerMotion::new();
        motion.connect_enter({
            let (model, area) = (model.clone(), area.clone());
            move |_, _, _| {
                model.hovered.set(true);
                area.queue_draw();
            }
        });
        motion.connect_leave({
            let (model, area) = (model.clone(), area.clone());
            move |_| {
                model.hovered.set(false);
                area.queue_draw();
            }
        });
        area.add_controller(motion);
        Self { area, model }
    }

    /// The backend's latest context size. Ends the "pending" look of a click.
    pub fn set_context(&self, tokens: u64, limit: u64) {
        let fill = ring_fill(tokens, limit);
        self.model.target.set(Some(fill));
        self.model.pending_since.set(None);
        self.area.set_tooltip_text(Some(&ring_tooltip(fill)));
        self.area.queue_draw();
    }

    /// Show that a compaction was asked for (or is running) until the new size arrives.
    pub fn set_pending(&self, now: Instant) {
        self.model.pending_since.set(Some(now));
        self.area.queue_draw();
    }

    /// Advance the fill animation by `frames` 60 Hz frames.
    pub fn step(&self, frames: f64, now: Instant) {
        let m = &self.model;
        if m.pending_since
            .get()
            .is_some_and(|since| now.duration_since(since) > PENDING_TIMEOUT)
        {
            m.pending_since.set(None);
            self.area.queue_draw();
        }
        let Some(target) = m.target.get() else {
            return;
        };
        let shown = m.shown.get();
        if shown == target {
            return;
        }
        let next = shown + (target - shown) * per_frame(RING_EASE, frames);
        m.shown.set(if (target - next).abs() < 0.002 { target } else { next });
        self.area.queue_draw();
    }
}

fn draw_ring(cr: &Context, w: f64, h: f64, m: &RingModel) {
    let (cx, cy, r) = ring_geometry(w, h);
    let lit = m.hovered.get() || m.pending_since.get().is_some();
    cr.set_line_width(RING_LINE);

    // Track. Until the context is known it's the whole button, as bright as before
    let track = match (m.target.get().is_some(), lit) {
        (false, false) => 0.32,
        (false, true) => 0.75,
        (true, false) => 0.2,
        (true, true) => 0.4,
    };
    cr.set_source_rgba(1.0, 1.0, 1.0, track);
    cr.arc(cx, cy, r, 0.0, TAU);
    let _ = cr.stroke();

    let fill = m.shown.get();
    if fill > 0.0 {
        let (red, green, blue, alpha) = mascot::BODY;
        cr.set_source_rgba(red, green, blue, if lit { 1.0 } else { alpha });
        // Cairo's angles start at 3 o'clock; ours at 12, going clockwise
        cr.arc(cx, cy, r, -FRAC_PI_2, -FRAC_PI_2 + TAU * fill);
        let _ = cr.stroke();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ring_draws_inside_its_box_at_its_old_size() {
        let size = RING_BOX as f64;
        let (cx, cy, r) = ring_geometry(size, size);
        assert!(cx - r - RING_LINE / 2.0 >= 0.0 && cx + r + RING_LINE / 2.0 <= size);
        assert!(cy - r - RING_LINE / 2.0 >= 0.0 && cy + r + RING_LINE / 2.0 <= size);
        // The hit box grew; the ring itself didn't
        assert_eq!(r, (14.0 - RING_LINE) * 0.5 - 0.5);
    }

    #[test]
    fn ring_fills_toward_the_auto_compact_point() {
        assert_eq!(ring_fill(0, 9500), 0.0);
        assert_eq!(ring_fill(4750, 9500), 0.5);
        assert_eq!(ring_fill(12000, 9500), 1.0); // over: full, not past
        assert_eq!(ring_fill(10, 0), 1.0); // never divides by zero
        assert_eq!(
            ring_tooltip(0.642),
            "Context 64% full. Click to compact the conversation"
        );
    }
}
