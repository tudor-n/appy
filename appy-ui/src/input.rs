//! The message input pill: a growing multi-line text field plus the effort picker.

use gtk4::prelude::*;
use gtk4::{
    Align, Box as GtkBox, Button, EventControllerFocus, Label, Orientation, Overlay, PolicyType,
    Revealer, RevealerTransitionType, ScrolledWindow, TextBuffer, TextView, WrapMode,
};

use crate::protocol::Effort;

/// The input grows with its text up to this height, then scrolls.
const INPUT_MAX_HEIGHT: i32 = 132;

pub struct ChatInput {
    pub container: GtkBox,
    pub view: TextView,
    pub buffer: TextBuffer,
    pub focus: EventControllerFocus,
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

        let effort = EffortPicker::new();

        let row = GtkBox::new(Orientation::Horizontal, 8);
        row.set_hexpand(true);
        row.append(&overlay);
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
