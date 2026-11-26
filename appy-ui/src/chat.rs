//! The scrolling message log: chat bubbles, status pills and smooth scrolling.

use std::cell::Cell;
use std::rc::Rc;

use gtk4::prelude::*;
use gtk4::{Align, Box as GtkBox, Label, Orientation, PolicyType, ScrolledWindow, glib};

/// Oldest entries are dropped beyond this many, to bound memory and layout cost.
const MAX_ENTRIES: usize = 500;
const LOG_WIDTH: i32 = 290;
const LOG_MAX_HEIGHT: i32 = 360;

pub struct ChatLog {
    pub scrolled: ScrolledWindow,
    entries: GtkBox,
    /// Where smooth scrolling is heading; advanced each frame by `step_scroll`.
    /// Kept in a Cell (not SharedState) so appending never contends for that RefCell.
    scroll_target: Rc<Cell<Option<f64>>>,
}

impl ChatLog {
    pub fn new() -> Self {
        let entries = GtkBox::new(Orientation::Vertical, 6);
        entries.set_valign(Align::End);

        let scrolled = ScrolledWindow::builder()
            .hscrollbar_policy(PolicyType::Never)
            .vscrollbar_policy(PolicyType::External)
            .propagate_natural_height(true)
            .max_content_height(LOG_MAX_HEIGHT)
            .width_request(LOG_WIDTH)
            .child(&entries)
            .build();

        Self {
            scrolled,
            entries,
            scroll_target: Rc::new(Cell::new(None)),
        }
    }

    pub fn push_bubble(&self, text: &str, from_user: bool) {
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
            // WordChar so a very long unbroken string wraps instead of forcing the whole
            // overlay wider (the default Word mode can't break inside a word).
            .wrap_mode(gtk4::pango::WrapMode::WordChar)
            .max_width_chars(30)
            .build();
        msg_label.add_css_class("bubble-text");

        let time_label = Label::builder()
            .label(current_timestamp())
            .xalign(1.0)
            .halign(Align::End)
            .build();
        time_label.add_css_class("bubble-time");

        bubble.append(&msg_label);
        bubble.append(&time_label);
        self.push(&bubble);
    }

    /// Appends a live status pill and returns its label so the caller can animate it.
    pub fn push_status(&self, markup: &str) -> Label {
        let pill = GtkBox::new(Orientation::Horizontal, 0);
        pill.add_css_class("status-pill");
        pill.add_css_class("live");
        pill.set_halign(Align::Start);

        let label = Label::new(None);
        label.set_markup(markup);
        label.add_css_class("status-pill-text");
        pill.append(&label);
        self.push(&pill);
        label
    }

    fn push(&self, entry: &GtkBox) {
        self.trim();
        self.entries.append(entry);

        // Add "visible" on the next idle so the CSS entrance transition runs
        let entry = entry.clone();
        glib::idle_add_local_once(move || entry.add_css_class("visible"));

        self.scroll_to_bottom();
    }

    fn trim(&self) {
        let mut count = 0;
        let mut child = self.entries.first_child();
        while let Some(c) = child {
            count += 1;
            child = c.next_sibling();
        }
        if count >= MAX_ENTRIES
            && let Some(first) = self.entries.first_child()
        {
            self.entries.remove(&first);
        }
    }

    fn max_scroll(&self) -> f64 {
        let adj = self.scrolled.vadjustment();
        (adj.upper() - adj.page_size()).max(0.0)
    }

    /// Smoothly scroll to the bottom once the new entry has been laid out.
    pub fn scroll_to_bottom(&self) {
        let scrolled = self.scrolled.clone();
        let target = self.scroll_target.clone();
        glib::idle_add_local_once(move || {
            let adj = scrolled.vadjustment();
            target.set(Some((adj.upper() - adj.page_size()).max(0.0)));
        });
    }

    /// Smoothly scroll by `delta` pixels, accumulating onto any scroll in progress.
    pub fn scroll_by(&self, delta: f64) {
        let base = self
            .scroll_target
            .get()
            .unwrap_or_else(|| self.scrolled.vadjustment().value());
        self.scroll_target
            .set(Some((base + delta).clamp(0.0, self.max_scroll())));
    }

    /// Advance smooth scrolling by one frame.
    pub fn step_scroll(&self) {
        let Some(target) = self.scroll_target.get() else {
            return;
        };
        let adj = self.scrolled.vadjustment();
        let (value, done) = ease_toward(adj.value(), target.clamp(0.0, self.max_scroll()));
        adj.set_value(value);
        self.scroll_target.set(if done {
            None
        } else {
            Some(target.clamp(0.0, self.max_scroll()))
        });
    }
}

/// One frame of exponential easing. Returns (new value, arrived).
fn ease_toward(current: f64, target: f64) -> (f64, bool) {
    let diff = target - current;
    if diff.abs() < 0.6 {
        (target, true)
    } else {
        (current + diff * 0.22, false)
    }
}

/// "thinking." / "thinking.." / "thinking..." with the missing dots kept as invisible
/// placeholders so the pill doesn't change width while animating.
pub fn format_live_dots(word: &str, step: u128) -> String {
    match step % 3 {
        0 => format!("{word}.<span alpha=\"1\">..</span>"),
        1 => format!("{word}..<span alpha=\"1\">.</span>"),
        _ => format!("{word}..."),
    }
}

fn current_timestamp() -> String {
    glib::DateTime::now_local()
        .ok()
        .and_then(|dt| dt.format("%H:%M").ok())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "now".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn easing_converges() {
        let (mut v, mut done) = (0.0, false);
        let mut frames = 0;
        while !done {
            (v, done) = ease_toward(v, 300.0);
            frames += 1;
            assert!(frames < 100);
        }
        assert_eq!(v, 300.0);
    }

    #[test]
    fn live_dots_cycle() {
        assert_eq!(format_live_dots("thinking", 2), "thinking...");
        assert_eq!(
            format_live_dots("thinking", 3),
            format_live_dots("thinking", 0)
        );
    }
}
