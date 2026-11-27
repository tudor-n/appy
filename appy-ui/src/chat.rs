//! The scrolling message log: chat bubbles, status pills and smooth scrolling.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::Instant;

use gtk4::prelude::*;
use gtk4::{
    Align, Box as GtkBox, Button, EventControllerMotion, Label, Orientation, PolicyType,
    Revealer, RevealerTransitionType, ScrolledWindow, glib,
};

use crate::memory::MemoryBurst;
use crate::protocol::MemoryKind;

/// Oldest entries are dropped beyond this many, to bound memory and layout cost.
const MAX_ENTRIES: usize = 500;
const LOG_WIDTH: i32 = 290;
const LOG_MAX_HEIGHT: i32 = 360;
const PILL_UNFOLD_MS: u32 = 220;

/// The newest memory pill, while changes can still join it.
struct MemoryPill {
    burst: MemoryBurst,
    title: Label,
    list: GtkBox,
    /// The `memory_undo`s the pill's Undo sends (grows with the burst).
    undo_what: Rc<RefCell<Vec<Vec<String>>>>,
}

pub struct ChatLog {
    pub scrolled: ScrolledWindow,
    entries: GtkBox,
    /// Where a manual smooth scroll is heading; advanced each frame by `step_scroll`.
    /// Cells (not SharedState) so appending never contends for that RefCell.
    scroll_target: Cell<Option<f64>>,
    /// Stick to the bottom: every frame eases toward the *current* bottom, so newly
    /// added entries come into view once GTK has laid them out (their height isn't
    /// known when they're appended). Scrolling up unpins; reaching the bottom re-pins.
    pinned: Cell<bool>,
    /// Undo buttons on memory pills; only the newest one stays enabled.
    undo_buttons: RefCell<Vec<Button>>,
    /// The pill the next memory change may join (any other entry ends it).
    memory: RefCell<Option<MemoryPill>>,
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
            scroll_target: Cell::new(None),
            pinned: Cell::new(true),
            undo_buttons: RefCell::new(Vec::new()),
            memory: RefCell::new(None),
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

    /// A memory change. Changes in a burst share one centred pill ("Remembered 3 facts")
    /// that unfolds into the facts on hover. Its Undo takes back the whole pill; Undo
    /// always reverts the newest change, so only the newest pill's button is live, and
    /// each works once.
    pub fn push_memory(
        &self,
        kind: MemoryKind,
        summary: &str,
        facts: Vec<String>,
        dropped: Vec<String>,
        commit: &str,
        undo: Rc<dyn Fn(Vec<String>)>,
    ) {
        let now = Instant::now();
        {
            let mut current = self.memory.borrow_mut();
            if let Some(pill) = current.as_mut().filter(|p| p.burst.accepts(kind, now)) {
                pill.burst.add(kind, summary, facts, dropped, commit, now);
                pill.title.set_label(&pill.burst.title());
                fill_facts(&pill.list, &pill.burst);
                *pill.undo_what.borrow_mut() = undo_what(&pill.burst);
                return;
            }
        }
        for old in self.undo_buttons.take() {
            old.set_sensitive(false);
        }
        let burst = MemoryBurst::new(kind, summary, facts, dropped, commit, now);

        let pill = GtkBox::new(Orientation::Vertical, 0);
        pill.add_css_class("status-pill");
        pill.add_css_class("memory-pill");
        pill.set_halign(Align::Center);
        let title = Label::new(Some(&burst.title()));
        title.add_css_class("status-pill-text");
        pill.append(&title);

        let body = GtkBox::new(Orientation::Vertical, 3);
        body.add_css_class("memory-facts");
        let list = GtkBox::new(Orientation::Vertical, 3);
        fill_facts(&list, &burst);
        body.append(&list);
        let undo_what_now = Rc::new(RefCell::new(undo_what(&burst)));
        if let Some(label) = burst.undo_label() {
            let button = Button::with_label(label);
            button.add_css_class("memory-undo");
            button.set_focus_on_click(false);
            button.set_halign(Align::End);
            let what = undo_what_now.clone();
            let closed = burst.close_handle();
            button.connect_clicked(move |b| {
                b.set_sensitive(false);
                closed.set(true); // what happens next (the undo itself) gets its own pill
                for commits in what.borrow().iter() {
                    undo(commits.clone());
                }
            });
            body.append(&button);
            self.undo_buttons.borrow_mut().push(button);
        }
        let revealer = Revealer::builder()
            .transition_type(RevealerTransitionType::SlideDown)
            .transition_duration(PILL_UNFOLD_MS)
            .child(&body)
            .build();
        pill.append(&revealer);

        let motion = EventControllerMotion::new();
        let r = revealer.clone();
        motion.connect_enter(move |_, _, _| r.set_reveal_child(true));
        let r = revealer.clone();
        motion.connect_leave(move |_| r.set_reveal_child(false));
        pill.add_controller(motion);

        self.push(&pill);
        *self.memory.borrow_mut() = Some(MemoryPill {
            burst,
            title,
            list,
            undo_what: undo_what_now,
        });
    }

    /// A one-line notice in the chat (e.g. an undo that didn't happen).
    pub fn push_notice(&self, text: &str) {
        let pill = GtkBox::new(Orientation::Horizontal, 0);
        pill.add_css_class("status-pill");
        pill.add_css_class("memory-notice");
        pill.set_halign(Align::Center);
        let label = Label::builder()
            .label(text)
            .wrap(true)
            .wrap_mode(gtk4::pango::WrapMode::WordChar)
            .max_width_chars(34)
            .build();
        label.add_css_class("status-pill-text");
        pill.append(&label);
        self.push(&pill);
    }

    fn push(&self, entry: &GtkBox) {
        self.memory.replace(None); // anything new in the chat ends a memory burst
        self.trim();
        self.entries.append(entry);

        // Add "visible" on the next idle so the CSS entrance transition runs
        let entry = entry.clone();
        glib::idle_add_local_once(move || entry.add_css_class("visible"));

        // Every new message (yours or Appy's) brings the log back to the bottom
        self.pinned.set(true);
        self.scroll_target.set(None);
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

    /// Smoothly scroll by `delta` pixels, accumulating onto any scroll in progress.
    pub fn scroll_by(&self, delta: f64) {
        let max = self.max_scroll();
        let base = match (self.pinned.get(), self.scroll_target.get()) {
            (true, _) => max,
            (false, Some(target)) => target,
            (false, None) => self.scrolled.vadjustment().value(),
        };
        let target = (base + delta).clamp(0.0, max);
        self.pinned.set(is_at_bottom(target, max));
        self.scroll_target.set(Some(target));
    }

    /// Advance smooth scrolling by one frame.
    pub fn step_scroll(&self) {
        let max = self.max_scroll();
        let target = if self.pinned.get() {
            max
        } else if let Some(target) = self.scroll_target.get() {
            target.clamp(0.0, max)
        } else {
            return;
        };
        let adj = self.scrolled.vadjustment();
        let (value, done) = ease_toward(adj.value(), target);
        if value != adj.value() {
            adj.set_value(value);
        }
        self.scroll_target.set(if done || self.pinned.get() {
            None
        } else {
            Some(target)
        });
    }
}

/// Within a pixel of the bottom counts as "at the bottom".
fn is_at_bottom(value: f64, max: f64) -> bool {
    value >= max - 1.0
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
    fn bottom_detection() {
        assert!(is_at_bottom(300.0, 300.0));
        assert!(is_at_bottom(299.5, 300.0));
        assert!(!is_at_bottom(250.0, 300.0));
        assert!(is_at_bottom(0.0, 0.0)); // nothing to scroll
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

/// One wrapped line per fact; removed ones struck through.
fn fill_facts(list: &GtkBox, burst: &MemoryBurst) {
    while let Some(child) = list.first_child() {
        list.remove(&child);
    }
    for (text, removed) in burst.lines() {
        let label = Label::builder()
            .label(&text)
            .xalign(0.0)
            .wrap(true)
            .wrap_mode(gtk4::pango::WrapMode::WordChar)
            .max_width_chars(34)
            .build();
        label.add_css_class("memory-fact");
        if removed {
            label.add_css_class("dropped");
        }
        list.append(&label);
    }
}

/// The `memory_undo` messages a pill's Undo sends: one naming exactly its commits, or
/// (without commit ids, or for "Undo earlier") one plain undo per step.
fn undo_what(burst: &MemoryBurst) -> Vec<Vec<String>> {
    match burst.undo_commits() {
        Some(commits) => vec![commits],
        None => vec![Vec::new(); burst.undo_count()],
    }
}
