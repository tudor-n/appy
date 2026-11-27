//! Long-term memory in the overlay: the review panel (what the consolidator learned,
//! waiting for Approve / Deny / Edit) and the chips for memory commits.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::{Duration, Instant};

use gtk4::prelude::*;
use gtk4::{
    Align, Box as GtkBox, Button, EventControllerScroll, EventControllerScrollFlags, Label,
    Orientation, PolicyType, ScrolledWindow, Stack, StackTransitionType, TextView, glib,
};

use crate::input::set_class;
use crate::protocol::{Decision, MemoryKind, Proposal};

const EDIT_MIN_HEIGHT: i32 = 140;


/// One line of a note diff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DiffLine {
    Same(String),
    Del(String),
    Add(String),
    /// Unchanged lines left out between changes.
    Gap,
}

/// Lines of context kept around each change.
const CONTEXT: usize = 1;

/// A line diff of two notes (LCS), showing only changes and `CONTEXT` lines around them.
pub fn diff_lines(before: &str, after: &str) -> Vec<DiffLine> {
    let a: Vec<&str> = before.lines().collect();
    let b: Vec<&str> = after.lines().collect();
    // lcs[i][j] = length of the longest common subsequence of a[i..] and b[j..]
    let mut lcs = vec![vec![0usize; b.len() + 1]; a.len() + 1];
    for i in (0..a.len()).rev() {
        for j in (0..b.len()).rev() {
            lcs[i][j] = if a[i] == b[j] {
                lcs[i + 1][j + 1] + 1
            } else {
                lcs[i + 1][j].max(lcs[i][j + 1])
            };
        }
    }
    let mut full = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() || j < b.len() {
        if i < a.len() && j < b.len() && a[i] == b[j] {
            full.push(DiffLine::Same(a[i].to_string()));
            i += 1;
            j += 1;
        } else if i < a.len() && (j == b.len() || lcs[i + 1][j] >= lcs[i][j + 1]) {
            full.push(DiffLine::Del(a[i].to_string())); // deletions first: old line, then new
            i += 1;
        } else {
            full.push(DiffLine::Add(b[j].to_string()));
            j += 1;
        }
    }
    // Keep changes and their context; collapse other unchanged runs into one Gap
    let changed: Vec<bool> = full.iter().map(|l| !matches!(l, DiffLine::Same(_))).collect();
    let near = |k: usize| {
        let lo = k.saturating_sub(CONTEXT);
        let hi = (k + CONTEXT).min(full.len() - 1);
        (lo..=hi).any(|m| changed[m])
    };
    let mut out = Vec::new();
    let mut skipped = false;
    for (k, line) in full.iter().enumerate() {
        if near(k) {
            if skipped && !out.is_empty() {
                out.push(DiffLine::Gap);
            }
            skipped = false;
            out.push(line.clone());
        } else {
            skipped = true;
        }
    }
    out
}

pub fn chip_text(kind: MemoryKind, summary: &str) -> String {
    let fact = summary.strip_prefix("- ").unwrap_or(summary);
    match kind {
        MemoryKind::Remember => format!("Remembered: {fact}"),
        MemoryKind::Forget => format!("Forgot: {fact}"),
        MemoryKind::Approved => format!("Saved: {fact}"),
        MemoryKind::Edited => format!("Saved your edit: {fact}"),
        MemoryKind::Auto => format!("Auto-saved: {fact}"),
        MemoryKind::Undo => format!("Undid: {}", fact.strip_prefix("memory: ").unwrap_or(fact)),
        MemoryKind::Other => format!("Memory: {fact}"),
    }
}

/// A button on a proposal row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Accept,
    Deny,
    Edit,
    Save,
    Cancel,
}

impl Action {
    fn label(self) -> &'static str {
        match self {
            Action::Accept => "Approve",
            Action::Deny => "Deny",
            Action::Edit => "Edit",
            Action::Save => "Save",
            Action::Cancel => "Cancel",
        }
    }
}

/// The edit box's state: survives list refreshes, and a failed Save.
#[derive(Debug, Clone, PartialEq)]
pub struct Editing {
    pub slug: String,
    pub text: String,
    /// The note as the card showed it when editing started; the Mac refuses the edit if
    /// the note has changed since (so a fact remembered meanwhile isn't overwritten).
    pub before: String,
}

/// A decision to send to the backend.
#[derive(Debug, Clone, PartialEq)]
pub struct Outgoing {
    pub slug: String,
    pub decision: Decision,
    pub text: Option<String>,
    pub before: Option<String>,
}

/// Everything the panel shows, as plain data (the widget only draws it).
#[derive(Debug, Default)]
pub struct TrayState {
    pub items: Vec<Proposal>,
    pub editing: Option<Editing>,
    /// The proposal on screen (one per page).
    pub page: usize,
    /// Proposals a decision was sent for; their buttons wait for the next list.
    pending: Vec<String>,
    /// Set by Later: the (slug, after) pairs on screen then. The panel stays hidden
    /// until the list holds something not in here.
    later: Option<Vec<(String, String)>>,
}

impl TrayState {
    /// The backend's whole list. Edit mode survives as long as its proposal is still in it
    /// (so a failed Save keeps the text, with the error shown above it).
    pub fn set_items(&mut self, items: Vec<Proposal>) {
        if self
            .editing
            .as_ref()
            .is_some_and(|e| !items.iter().any(|i| i.slug == e.slug))
        {
            self.editing = None;
        }
        self.pending.clear();
        if let Some(seen) = &self.later {
            let new = items
                .iter()
                .any(|i| !seen.iter().any(|(slug, after)| *slug == i.slug && *after == i.after));
            if new {
                self.later = None;
            }
        }
        self.items = items;
        self.page = self.page.min(self.items.len().saturating_sub(1));
    }

    pub fn next_page(&mut self) {
        if self.page + 1 < self.items.len() {
            self.page += 1;
        }
    }

    pub fn prev_page(&mut self) {
        self.page = self.page.saturating_sub(1);
    }

    /// "2 of 3" ("" with nothing to review).
    pub fn page_label(&self) -> String {
        if self.items.is_empty() {
            String::new()
        } else {
            format!("{} of {}", self.page + 1, self.items.len())
        }
    }

    /// Hide the panel without deciding anything, until something new arrives.
    pub fn later(&mut self) {
        self.later = Some(
            self.items
                .iter()
                .map(|i| (i.slug.clone(), i.after.clone()))
                .collect(),
        );
    }

    pub fn is_hidden(&self) -> bool {
        self.later.is_some()
    }

    fn approvable(&self, item: &Proposal) -> bool {
        !item.stale
            && !self.is_busy(&item.slug)
            && !self.editing.as_ref().is_some_and(|e| e.slug == item.slug)
    }

    pub fn can_approve_all(&self) -> bool {
        self.items.iter().any(|i| self.approvable(i))
    }

    /// Approve every proposal that can be approved right now (not being redone, not
    /// waiting on a decision, not open in Edit).
    pub fn approve_all(&mut self) -> Vec<Outgoing> {
        let slugs: Vec<String> = self
            .items
            .iter()
            .filter(|i| self.approvable(i))
            .map(|i| i.slug.clone())
            .collect();
        slugs
            .iter()
            .filter_map(|slug| self.press(slug, Action::Accept))
            .collect()
    }

    pub fn start_edit(&mut self, slug: &str) {
        if let Some(item) = self.items.iter().find(|i| i.slug == slug) {
            self.editing = Some(Editing {
                slug: item.slug.clone(),
                text: item.after.clone(),
                before: item.before.clone(),
            });
        }
    }

    pub fn type_text(&mut self, text: &str) {
        if let Some(e) = self.editing.as_mut() {
            e.text = text.to_string();
        }
    }

    pub fn is_busy(&self, slug: &str) -> bool {
        self.pending.iter().any(|s| s == slug)
    }

    /// The buttons for a row, with whether each is enabled.
    pub fn actions(&self, item: &Proposal) -> Vec<(Action, bool)> {
        let ready = !self.is_busy(&item.slug);
        if self.editing.as_ref().is_some_and(|e| e.slug == item.slug) {
            return vec![(Action::Save, ready), (Action::Cancel, true)];
        }
        // A stale proposal is being redone; it can't be accepted yet, but it can be
        // dropped or rewritten by hand (so it can never get stuck)
        let mut actions = if item.stale { vec![] } else { vec![(Action::Accept, ready)] };
        actions.push((Action::Deny, ready));
        actions.push((Action::Edit, ready));
        actions
    }

    /// A button press. Returns the decision to send, if any.
    pub fn press(&mut self, slug: &str, action: Action) -> Option<Outgoing> {
        if !self.items.iter().any(|i| i.slug == slug) {
            return None;
        }
        let decision = match action {
            Action::Edit => {
                self.start_edit(slug);
                return None;
            }
            Action::Cancel => {
                self.editing = None;
                return None;
            }
            Action::Accept => Decision::Accept,
            Action::Deny => Decision::Deny,
            Action::Save => Decision::Edit,
        };
        let (text, before) = match (action, &self.editing) {
            (Action::Save, Some(e)) if e.slug == slug => (Some(e.text.clone()), Some(e.before.clone())),
            (Action::Save, _) => return None,
            _ => (None, None),
        };
        self.pending.push(slug.to_string());
        Some(Outgoing {
            slug: slug.to_string(),
            decision,
            text,
            before,
        })
    }
}

/// Memory changes this close together share one pill in the chat.
pub const BURST_WINDOW: Duration = Duration::from_secs(4);

/// One pill's worth of memory changes: "Remembered 3 facts", unfolding into the facts.
#[derive(Debug, Clone)]
pub struct MemoryBurst {
    /// A burst of undos (they never mix with changes).
    undo: bool,
    /// Every change was of a kind this UI doesn't know (no Undo for those).
    unknown: bool,
    changes: usize,
    remembered: usize,
    forgot: usize,
    /// (sentence, removed)
    lines: Vec<(String, bool)>,
    last: Instant,
}

impl MemoryBurst {
    pub fn new(
        kind: MemoryKind,
        summary: &str,
        facts: Vec<String>,
        dropped: Vec<String>,
        now: Instant,
    ) -> Self {
        let mut burst = Self {
            undo: kind == MemoryKind::Undo,
            unknown: true,
            changes: 0,
            remembered: 0,
            forgot: 0,
            lines: Vec::new(),
            last: now,
        };
        burst.add(kind, summary, facts, dropped, now);
        burst
    }

    /// Whether a change arriving `now` joins this pill.
    pub fn accepts(&self, kind: MemoryKind, now: Instant) -> bool {
        (kind == MemoryKind::Undo) == self.undo
            && now.saturating_duration_since(self.last) <= BURST_WINDOW
    }

    pub fn add(
        &mut self,
        kind: MemoryKind,
        summary: &str,
        facts: Vec<String>,
        dropped: Vec<String>,
        now: Instant,
    ) {
        self.changes += 1;
        self.last = now;
        self.unknown &= kind == MemoryKind::Other;
        if facts.is_empty() && dropped.is_empty() {
            let text = summary.strip_prefix("memory: ").unwrap_or(summary);
            let text = ["[approved]", "[edited]", "[auto]"]
                .iter()
                .fold(text, |t, tag| t.strip_suffix(tag).unwrap_or(t))
                .trim();
            let text = text.strip_prefix("- ").unwrap_or(text);
            if !self.undo {
                self.remembered += 1;
            }
            self.lines.push((text.to_string(), false));
            return;
        }
        self.remembered += facts.len();
        self.forgot += dropped.len();
        self.lines.extend(facts.into_iter().map(|f| (f, false)));
        self.lines.extend(dropped.into_iter().map(|d| (d, true)));
    }

    pub fn title(&self) -> String {
        let count = |n: usize, one: &str, many: &str| {
            if n == 1 {
                one.to_string()
            } else {
                format!("{n} {many}")
            }
        };
        if self.undo {
            return format!("Undid {}", count(self.changes, "a change", "changes"));
        }
        match (self.remembered, self.forgot) {
            (r, 0) => format!("Remembered {}", count(r, "a fact", "facts")),
            (0, f) => format!("Forgot {}", count(f, "a fact", "facts")),
            (r, f) => format!("Remembered {} · forgot {f}", count(r, "a fact", "facts")),
        }
    }

    /// What the dropdown lists: (sentence, removed).
    pub fn lines(&self) -> Vec<(String, bool)> {
        self.lines.clone()
    }

    /// How many `memory_undo`s the Undo button sends: the whole pill, or (on an "Undid"
    /// pill) one more step back.
    pub fn undo_count(&self) -> usize {
        if self.undo { 1 } else { self.changes }
    }

    pub fn undo_label(&self) -> Option<&'static str> {
        if self.undo {
            Some("Undo earlier")
        } else if self.unknown {
            None
        } else {
            Some("Undo")
        }
    }
}

/// The chip's Undo button label: every change can be undone; after an undo, the next
/// press goes further back.
pub fn undo_label(kind: MemoryKind) -> Option<&'static str> {
    match kind {
        MemoryKind::Remember
        | MemoryKind::Forget
        | MemoryKind::Approved
        | MemoryKind::Edited
        | MemoryKind::Auto => {
            Some("Undo")
        }
        MemoryKind::Undo => Some("Undo earlier"),
        MemoryKind::Other => None,
    }
}

type SendFn = dyn Fn(Outgoing);

/// The panel's width; the chat is 290 px, the panel is wider so a note reads easily.
pub const MEMORY_WIDTH: i32 = 360;
/// A long diff scrolls inside its card past this, so the buttons never leave the card.
const DIFF_MAX_HEIGHT: i32 = 180;
const PAGE_TRANSITION_MS: u32 = 220;
/// One wheel notch or touchpad swipe moves one page; more scrolling within this is ignored.
const PAGE_COOLDOWN: Duration = Duration::from_millis(250);

/// The memory review panel: floats above (or below) the chat, one proposal per page with
/// Approve / Deny / Edit, plus Later and Approve all. Placed by tick.rs.
#[derive(Clone)]
pub struct MemoryPanel {
    pub root: GtkBox,
    inner: Rc<Inner>,
}

struct Inner {
    count: Label,
    up: Button,
    down: Button,
    approve_all: Button,
    stack: Stack,
    state: RefCell<TrayState>,
    on_send: RefCell<Option<Rc<SendFn>>>,
    last_page_turn: Cell<Option<Instant>>,
}

impl MemoryPanel {
    pub fn new() -> Self {
        let root = GtkBox::new(Orientation::Vertical, 8);
        root.add_css_class("memory-panel");
        root.set_width_request(MEMORY_WIDTH);
        root.set_valign(Align::Start);
        root.set_halign(Align::Start);
        root.set_can_target(false);

        let header = GtkBox::new(Orientation::Horizontal, 6);
        header.add_css_class("memory-header");
        let title = Label::new(Some("Memory"));
        title.add_css_class("memory-heading");
        let count = Label::new(None);
        count.add_css_class("memory-count");
        let spacer = GtkBox::new(Orientation::Horizontal, 0);
        spacer.set_hexpand(true);
        let up = nav_button("▲", "Previous");
        let down = nav_button("▼", "Next");
        let later = button("Later", false);
        let approve_all = button("Approve all", true);
        header.append(&title);
        header.append(&count);
        header.append(&spacer);
        header.append(&up);
        header.append(&down);
        header.append(&later);
        header.append(&approve_all);
        root.append(&header);

        let stack = Stack::builder()
            .vhomogeneous(true)
            .hhomogeneous(true)
            .transition_duration(PAGE_TRANSITION_MS)
            .build();
        root.append(&stack);

        let panel = Self {
            root,
            inner: Rc::new(Inner {
                count,
                up,
                down,
                approve_all,
                stack,
                state: RefCell::new(TrayState::default()),
                on_send: RefCell::new(None),
                last_page_turn: Cell::new(None),
            }),
        };

        let p = panel.clone();
        panel.inner.up.connect_clicked(move |_| p.turn_page(-1));
        let p = panel.clone();
        panel.inner.down.connect_clicked(move |_| p.turn_page(1));
        let p = panel.clone();
        later.connect_clicked(move |_| {
            p.inner.state.borrow_mut().later();
            p.render();
        });
        let p = panel.clone();
        panel.inner.approve_all.connect_clicked(move |_| {
            let out = p.inner.state.borrow_mut().approve_all();
            p.send(out);
            p.render();
        });

        // DISCRETE: GTK turns touchpad deltas into whole steps, like wheel notches
        let scroll = EventControllerScroll::new(
            EventControllerScrollFlags::VERTICAL | EventControllerScrollFlags::DISCRETE,
        );
        let p = panel.clone();
        scroll.connect_scroll(move |_, _, dy| {
            if dy != 0.0 {
                let now = Instant::now();
                let cooling = p
                    .inner
                    .last_page_turn
                    .get()
                    .is_some_and(|t| now.duration_since(t) < PAGE_COOLDOWN);
                if !cooling {
                    p.turn_page(if dy > 0.0 { 1 } else { -1 });
                }
            }
            glib::Propagation::Stop
        });
        panel.root.add_controller(scroll);
        panel
    }

    /// Where decisions go (the App sends them to the backend).
    pub fn set_on_send(&self, f: impl Fn(Outgoing) + 'static) {
        *self.inner.on_send.borrow_mut() = Some(Rc::new(f));
    }

    /// Show the current list (the backend always sends all of it).
    pub fn set_items(&self, items: &[Proposal]) {
        self.inner.state.borrow_mut().set_items(items.to_vec());
        self.render();
    }

    /// Something to review, and not put away with Later.
    pub fn is_wanted(&self) -> bool {
        let state = self.inner.state.borrow();
        !state.items.is_empty() && !state.is_hidden()
    }

    /// Show or hide (with the chat's fade); hidden, it takes no clicks.
    pub fn set_shown(&self, shown: bool) {
        set_class(&self.root, "open", shown);
        self.root.set_can_target(shown);
    }

    pub fn is_shown(&self) -> bool {
        self.root.has_css_class("open")
    }

    /// Keyboard focus is in the panel (the Edit box): keep the chat open while typing.
    pub fn contains_focus(&self) -> bool {
        self.is_shown() && self.root.focus_child().is_some()
    }

    fn send(&self, out: Vec<Outgoing>) {
        let send = self.inner.on_send.borrow().clone();
        if let Some(send) = send {
            for o in out {
                send(o);
            }
        }
    }

    fn press(&self, slug: &str, action: Action) {
        let out = self.inner.state.borrow_mut().press(slug, action);
        self.send(out.into_iter().collect());
        self.render();
    }

    fn turn_page(&self, step: i32) {
        let moved = {
            let mut state = self.inner.state.borrow_mut();
            let before = state.page;
            if step > 0 {
                state.next_page();
            } else {
                state.prev_page();
            }
            state.page != before
        };
        if moved {
            self.inner.last_page_turn.set(Some(Instant::now()));
            let transition = if step > 0 {
                StackTransitionType::SlideUp
            } else {
                StackTransitionType::SlideDown
            };
            self.show_page(transition);
        }
    }

    /// Point the stack at the current page and update the header.
    fn show_page(&self, transition: StackTransitionType) {
        let inner = &self.inner;
        let state = inner.state.borrow();
        inner.count.set_label(&state.page_label());
        inner.up.set_sensitive(state.page > 0);
        inner.down.set_sensitive(state.page + 1 < state.items.len());
        inner.approve_all.set_sensitive(state.can_approve_all());
        let name = format!("p{}", state.page);
        if inner.stack.child_by_name(&name).is_some() {
            inner.stack.set_visible_child_full(&name, transition);
        }
    }

    /// Rebuild every page (the list, a decision or Edit changed).
    fn render(&self) {
        let (items, editing) = {
            let state = self.inner.state.borrow();
            (state.items.clone(), state.editing.clone())
        };
        let stack = &self.inner.stack;
        while let Some(child) = stack.first_child() {
            stack.remove(&child);
        }
        for (i, item) in items.iter().enumerate() {
            let edit = editing.as_ref().filter(|e| e.slug == item.slug);
            stack.add_named(&self.page(item, edit), Some(&format!("p{i}")));
        }
        self.show_page(StackTransitionType::None);
    }

    fn page(&self, item: &Proposal, editing: Option<&Editing>) -> GtkBox {
        let page = GtkBox::new(Orientation::Vertical, 4);
        page.add_css_class("memory-item");
        page.append(&text_label(&item.title, "memory-title"));
        page.append(&text_label(&item.summary, "memory-summary"));
        if let Some(err) = &item.error {
            page.append(&text_label(err, "memory-error"));
        }
        if item.stale && editing.is_none() {
            page.append(&text_label("updating…", "memory-summary"));
        }

        // The middle grows to fill the page, so the buttons sit at the bottom
        let body = GtkBox::new(Orientation::Vertical, 0);
        body.set_vexpand(true);
        if let Some(e) = editing {
            let view = TextView::builder()
                .wrap_mode(gtk4::WrapMode::WordChar)
                .monospace(true)
                .accepts_tab(false)
                .build();
            view.add_css_class("memory-edit");
            view.buffer().set_text(&e.text);
            let t = self.clone();
            view.buffer().connect_changed(move |buf| {
                let text = buf.text(&buf.start_iter(), &buf.end_iter(), false);
                t.inner.state.borrow_mut().type_text(&text);
            });
            let scroller = scroller(&view);
            // A TextView measures its text lazily; without a minimum it starts 0 lines tall
            scroller.set_min_content_height(EDIT_MIN_HEIGHT);
            body.append(&scroller);
            let v = view.clone();
            glib::idle_add_local_once(move || {
                v.grab_focus();
            });
        } else if !item.stale {
            let diff = GtkBox::new(Orientation::Vertical, 1);
            diff.add_css_class("memory-diff");
            for line in diff_lines(&item.before, &item.after) {
                // Facts are "- " bullets; the diff marker replaces the bullet
                let fact = |t: &str| t.strip_prefix("- ").unwrap_or(t).to_string();
                let (text, class) = match line {
                    DiffLine::Same(t) => (format!("  {}", fact(&t)), "diff-same"),
                    DiffLine::Del(t) => (format!("− {}", fact(&t)), "diff-del"),
                    DiffLine::Add(t) => (format!("+ {}", fact(&t)), "diff-add"),
                    DiffLine::Gap => ("  …".to_string(), "diff-same"),
                };
                diff.append(&text_label(&text, class));
            }
            body.append(&scroller(&diff));
        }
        page.append(&body);

        let buttons = GtkBox::new(Orientation::Horizontal, 6);
        buttons.add_css_class("memory-buttons");
        buttons.set_halign(Align::End);
        let actions = self.inner.state.borrow().actions(item);
        // Approve / Save last, at the right edge
        for (action, enabled) in actions.into_iter().rev() {
            let b = button(action.label(), matches!(action, Action::Accept | Action::Save));
            b.set_sensitive(enabled);
            let t = self.clone();
            let slug = item.slug.clone();
            b.connect_clicked(move |_| t.press(&slug, action));
            buttons.append(&b);
        }
        page.append(&buttons);
        page
    }
}

fn scroller(child: &impl IsA<gtk4::Widget>) -> ScrolledWindow {
    ScrolledWindow::builder()
        .hscrollbar_policy(PolicyType::Never)
        .vscrollbar_policy(PolicyType::Automatic)
        .propagate_natural_height(true)
        .max_content_height(DIFF_MAX_HEIGHT)
        .child(child)
        .build()
}

fn nav_button(glyph: &str, tooltip: &str) -> Button {
    let b = Button::with_label(glyph);
    b.set_focus_on_click(false);
    b.set_tooltip_text(Some(tooltip));
    b.add_css_class("memory-nav");
    b
}

fn text_label(text: &str, class: &str) -> Label {
    let label = Label::builder()
        .label(text)
        .xalign(0.0)
        .wrap(true)
        .wrap_mode(gtk4::pango::WrapMode::WordChar)
        .max_width_chars(40)
        .build();
    label.add_css_class(class);
    label
}

fn button(name: &str, primary: bool) -> Button {
    let b = Button::with_label(name);
    // Clicking shouldn't pull focus out of the chat input (Tab still reaches it)
    b.set_focus_on_click(false);
    b.add_css_class("memory-btn");
    if primary {
        b.add_css_class("primary");
    }
    b
}

#[cfg(test)]
mod tests {
    use super::*;
    use DiffLine::*;

    fn s(text: &str) -> String {
        text.to_string()
    }

    #[test]
    fn diff_lines_marks_add_and_del() {
        let before = "# Bench\n> gear\n- PSU is a Riden.\n- Scope is a Rigol.\n";
        let after = "# Bench\n> gear\n- PSU is a Korad.\n- Scope is a Rigol.\n";
        assert_eq!(
            diff_lines(before, after),
            vec![
                Same(s("> gear")),
                Del(s("- PSU is a Riden.")),
                Add(s("- PSU is a Korad.")),
                Same(s("- Scope is a Rigol.")),
            ]
        );
    }

    #[test]
    fn diff_lines_context_and_gaps() {
        let before = "a\nb\nc\nd\ne\nf\ng\n";
        let after = "a\nB\nc\nd\ne\nf\ng\nh\n";
        assert_eq!(
            diff_lines(before, after),
            vec![
                Same(s("a")),
                Del(s("b")),
                Add(s("B")),
                Same(s("c")),
                Gap,
                Same(s("g")),
                Add(s("h")),
            ]
        );
    }

    #[test]
    fn diff_lines_new_note_all_added() {
        assert_eq!(
            diff_lines("", "# Lumen\n- DuckDB\n"),
            vec![Add(s("# Lumen")), Add(s("- DuckDB"))]
        );
        assert_eq!(diff_lines("x\n", "x\n"), vec![]);
    }

    fn item(slug: &str, stale: bool) -> Proposal {
        Proposal {
            slug: slug.into(),
            title: slug.into(),
            summary: "s".into(),
            before: "# B\n- old\n".into(),
            after: "# B\n- new\n".into(),
            stale,
            error: None,
        }
    }

    #[test]
    fn failed_edit_keeps_the_typed_text_and_edit_mode() {
        let mut t = TrayState::default();
        t.set_items(vec![item("bench", false)]);
        t.start_edit("bench");
        t.type_text("# B\nprose line\n");
        let sent = t.press("bench", Action::Save).unwrap();
        assert_eq!(sent.decision, Decision::Edit);
        assert_eq!(sent.text.as_deref(), Some("# B\nprose line\n"));
        assert_eq!(sent.before.as_deref(), Some("# B\n- old\n"));
        assert!(t.is_busy("bench"));
        // the Mac refuses it: the list comes back with the error on that item
        let mut failed = item("bench", false);
        failed.error = Some("edit not saved: non-bullet line".into());
        t.set_items(vec![failed]);
        let editing = t.editing.as_ref().unwrap();
        assert_eq!(editing.text, "# B\nprose line\n");
        assert!(!t.is_busy("bench"));
        assert_eq!(
            t.actions(&t.items[0]),
            vec![(Action::Save, true), (Action::Cancel, true)]
        );
        // accepted later: the item leaves the list and edit mode ends
        t.set_items(vec![]);
        assert!(t.editing.is_none());
    }

    #[test]
    fn edit_keeps_the_before_it_started_from() {
        let mut t = TrayState::default();
        t.set_items(vec![item("bench", false)]);
        t.start_edit("bench");
        let mut moved = item("bench", false);
        moved.before = "# B\n- old\n- remembered meanwhile\n".into();
        t.set_items(vec![moved]);
        let sent = t.press("bench", Action::Save).unwrap();
        assert_eq!(sent.before.as_deref(), Some("# B\n- old\n")); // the Mac will refuse it
    }

    #[test]
    fn stale_rows_can_still_be_denied_or_edited() {
        let mut t = TrayState::default();
        t.set_items(vec![item("bench", true), item("lab", false)]);
        assert_eq!(
            t.actions(&t.items[0]),
            vec![(Action::Deny, true), (Action::Edit, true)]
        );
        assert_eq!(
            t.actions(&t.items[1]),
            vec![(Action::Accept, true), (Action::Deny, true), (Action::Edit, true)]
        );
        let sent = t.press("lab", Action::Accept).unwrap();
        assert_eq!((sent.decision, sent.text, sent.before), (Decision::Accept, None, None));
        assert_eq!(
            t.actions(&t.items[1]),
            vec![(Action::Accept, false), (Action::Deny, false), (Action::Edit, false)]
        );
        assert!(t.press("nope", Action::Deny).is_none());
    }

    #[test]
    fn cancel_leaves_edit_mode() {
        let mut t = TrayState::default();
        t.set_items(vec![item("bench", false)]);
        t.start_edit("bench");
        assert!(t.press("bench", Action::Cancel).is_none());
        assert!(t.editing.is_none());
    }

    fn items(slugs: &[&str]) -> Vec<Proposal> {
        slugs.iter().map(|s| item(s, false)).collect()
    }

    #[test]
    fn pages_follow_the_list() {
        let mut t = TrayState::default();
        assert_eq!(t.page_label(), "");
        t.set_items(items(&["a", "b", "c"]));
        assert_eq!(t.page_label(), "1 of 3");
        t.prev_page();
        assert_eq!(t.page, 0);
        t.next_page();
        t.next_page();
        t.next_page();
        assert_eq!(t.page_label(), "3 of 3");
        // the last one is approved: the list shrinks and the page follows
        t.set_items(items(&["a", "b"]));
        assert_eq!(t.page_label(), "2 of 2");
        // approving the one on screen shows the next in its place
        t.prev_page();
        t.set_items(items(&["b"]));
        assert_eq!((t.page, t.page_label().as_str()), (0, "1 of 1"));
        t.set_items(vec![]);
        assert_eq!((t.page, t.page_label().as_str()), (0, ""));
    }

    #[test]
    fn later_hides_until_something_new() {
        let mut t = TrayState::default();
        t.set_items(items(&["a", "b"]));
        assert!(!t.is_hidden());
        t.later();
        assert!(t.is_hidden());
        t.set_items(items(&["a", "b"])); // the 60s poll, nothing new
        assert!(t.is_hidden());
        t.set_items(items(&["a"])); // one approved elsewhere
        assert!(t.is_hidden());
        let mut changed = item("a", false);
        changed.after = "# B\n- newer\n".into();
        t.set_items(vec![changed.clone()]);
        assert!(!t.is_hidden());
        t.later();
        t.set_items(vec![changed, item("c", false)]); // a new note
        assert!(!t.is_hidden());
    }

    #[test]
    fn approve_all_skips_stale_busy_and_editing() {
        let mut t = TrayState::default();
        t.set_items(vec![item("a", true), item("b", false), item("c", false), item("d", false)]);
        t.start_edit("b");
        t.press("c", Action::Deny).unwrap();
        assert!(t.can_approve_all());
        let sent = t.approve_all();
        assert_eq!(
            sent.iter().map(|o| (o.slug.as_str(), o.decision)).collect::<Vec<_>>(),
            vec![("d", Decision::Accept)]
        );
        assert!(t.is_busy("d"));
        assert!(!t.can_approve_all());
        assert!(t.approve_all().is_empty());
    }

    #[test]
    fn accept_button_reads_approve() {
        assert_eq!(Action::Accept.label(), "Approve");
    }

    fn v(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn burst_title_counts() {
        let t0 = Instant::now();
        let mut b = MemoryBurst::new(MemoryKind::Approved, "s", v(&["A."]), vec![], t0);
        assert_eq!(b.title(), "Remembered a fact");
        b.add(MemoryKind::Auto, "s", v(&["B.", "C."]), vec![], t0);
        assert_eq!(b.title(), "Remembered 3 facts");
        let f = MemoryBurst::new(MemoryKind::Forget, "- x", vec![], v(&["x"]), t0);
        assert_eq!(f.title(), "Forgot a fact");
        let mut m = MemoryBurst::new(MemoryKind::Remember, "- a", v(&["a"]), vec![], t0);
        m.add(MemoryKind::Approved, "s", v(&["b"]), v(&["c"]), t0);
        assert_eq!(m.title(), "Remembered 2 facts · forgot 1");
        let mut u = MemoryBurst::new(MemoryKind::Undo, "memory: x [approved]", vec![], vec![], t0);
        assert_eq!(u.title(), "Undid a change");
        u.add(MemoryKind::Undo, "memory: y", vec![], vec![], t0);
        assert_eq!(u.title(), "Undid 2 changes");
        assert_eq!(m.lines(), vec![("a".into(), false), ("b".into(), false), ("c".into(), true)]);
    }

    #[test]
    fn burst_window_and_family() {
        let t0 = Instant::now();
        let mut b = MemoryBurst::new(MemoryKind::Approved, "s", v(&["A."]), vec![], t0);
        assert!(b.accepts(MemoryKind::Auto, t0 + Duration::from_secs(3)));
        assert!(!b.accepts(MemoryKind::Undo, t0 + Duration::from_secs(1)));
        b.add(MemoryKind::Auto, "s", v(&["B."]), vec![], t0 + Duration::from_secs(3));
        // the window runs from the newest change
        assert!(b.accepts(MemoryKind::Approved, t0 + Duration::from_secs(6)));
        assert!(!b.accepts(MemoryKind::Approved, t0 + Duration::from_secs(8)));
        let u = MemoryBurst::new(MemoryKind::Undo, "memory: x", vec![], vec![], t0);
        assert!(u.accepts(MemoryKind::Undo, t0));
        assert!(!u.accepts(MemoryKind::Approved, t0));
    }

    #[test]
    fn summary_fallback_when_no_facts() {
        let t0 = Instant::now();
        let b = MemoryBurst::new(MemoryKind::Approved, "Retitled the note", vec![], vec![], t0);
        assert_eq!(b.title(), "Remembered a fact");
        assert_eq!(b.lines(), vec![("Retitled the note".into(), false)]);
        let u = MemoryBurst::new(MemoryKind::Undo, "memory: Added the HUD [approved]", vec![], vec![], t0);
        assert_eq!(u.lines(), vec![("Added the HUD".into(), false)]);
    }

    #[test]
    fn undo_count_matches_changes() {
        let t0 = Instant::now();
        let mut b = MemoryBurst::new(MemoryKind::Approved, "s", v(&["A."]), vec![], t0);
        b.add(MemoryKind::Approved, "s", v(&["B.", "C."]), vec![], t0);
        b.add(MemoryKind::Auto, "s", v(&["D."]), vec![], t0);
        assert_eq!((b.undo_count(), b.undo_label()), (3, Some("Undo")));
        let mut u = MemoryBurst::new(MemoryKind::Undo, "memory: x", vec![], vec![], t0);
        u.add(MemoryKind::Undo, "memory: y", vec![], vec![], t0);
        assert_eq!((u.undo_count(), u.undo_label()), (1, Some("Undo earlier")));
        let o = MemoryBurst::new(MemoryKind::Other, "x", vec![], vec![], t0);
        assert_eq!(o.undo_label(), None);
    }

    #[test]
    fn undo_labels() {
        assert_eq!(undo_label(MemoryKind::Remember), Some("Undo"));
        assert_eq!(undo_label(MemoryKind::Approved), Some("Undo"));
        assert_eq!(undo_label(MemoryKind::Undo), Some("Undo earlier"));
        assert_eq!(undo_label(MemoryKind::Auto), Some("Undo"));
        assert_eq!(undo_label(MemoryKind::Other), None);
    }

    #[test]
    fn chip_text_by_kind() {
        assert_eq!(
            chip_text(MemoryKind::Remember, "- Tudor lives in Sibiu."),
            "Remembered: Tudor lives in Sibiu."
        );
        assert_eq!(
            chip_text(MemoryKind::Forget, "- The PSU is a Riden."),
            "Forgot: The PSU is a Riden."
        );
        assert_eq!(chip_text(MemoryKind::Approved, "added the HUD"), "Saved: added the HUD");
        assert_eq!(
            chip_text(MemoryKind::Edited, "added the HUD"),
            "Saved your edit: added the HUD"
        );
        assert_eq!(
            chip_text(MemoryKind::Undo, "memory: remember core: x"),
            "Undid: remember core: x"
        );
        assert_eq!(chip_text(MemoryKind::Other, "x"), "Memory: x");
        assert_eq!(chip_text(MemoryKind::Auto, "PSU is a Korad"), "Auto-saved: PSU is a Korad");
    }
}


