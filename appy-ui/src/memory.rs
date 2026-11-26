//! Long-term memory in the overlay: the review tray (what the consolidator learned,
//! waiting for Accept / Deny / Edit) and the chips for memory commits.

use std::cell::RefCell;
use std::rc::Rc;

use gtk4::prelude::*;
use gtk4::{Align, Box as GtkBox, Button, Label, Orientation, PolicyType, ScrolledWindow, TextView, glib};

use crate::protocol::{Decision, MemoryKind, Proposal};

const TRAY_MAX_HEIGHT: i32 = 260;
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

pub fn tray_label(n: usize) -> String {
    format!("{n} memory update{}", if n == 1 { "" } else { "s" })
}

pub fn chip_text(kind: MemoryKind, summary: &str) -> String {
    let fact = summary.strip_prefix("- ").unwrap_or(summary);
    match kind {
        MemoryKind::Remember => format!("Remembered: {fact}"),
        MemoryKind::Forget => format!("Forgot: {fact}"),
        MemoryKind::Approved => format!("Saved: {fact}"),
        MemoryKind::Edited => format!("Saved your edit: {fact}"),
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
            Action::Accept => "Accept",
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

/// Everything the tray shows, as plain data (the widget only draws it).
#[derive(Debug, Default)]
pub struct TrayState {
    pub items: Vec<Proposal>,
    pub expanded: bool,
    pub editing: Option<Editing>,
    /// Proposals a decision was sent for; their buttons wait for the next list.
    pending: Vec<String>,
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
        self.items = items;
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

/// The chip's Undo button label: every change can be undone; after an undo, the next
/// press goes further back.
pub fn undo_label(kind: MemoryKind) -> Option<&'static str> {
    match kind {
        MemoryKind::Remember | MemoryKind::Forget | MemoryKind::Approved | MemoryKind::Edited => {
            Some("Undo")
        }
        MemoryKind::Undo => Some("Undo earlier"),
        MemoryKind::Other => None,
    }
}

type SendFn = dyn Fn(Outgoing);

/// The collapsed "N memory updates" card above the input, expanding into one diff per
/// note with Accept / Deny / Edit. Hidden while there's nothing to review.
#[derive(Clone)]
pub struct MemoryTray {
    pub root: GtkBox,
    inner: Rc<Inner>,
}

struct Inner {
    header: Button,
    scroller: ScrolledWindow,
    list: GtkBox,
    state: RefCell<TrayState>,
    on_send: RefCell<Option<Rc<SendFn>>>,
}

impl MemoryTray {
    pub fn new() -> Self {
        let root = GtkBox::new(Orientation::Vertical, 4);
        root.add_css_class("memory-tray");
        root.set_visible(false);

        let header = Button::new();
        header.add_css_class("memory-header");
        header.set_halign(Align::Fill);
        header.set_focus_on_click(false);
        root.append(&header);

        let list = GtkBox::new(Orientation::Vertical, 0);
        let scroller = ScrolledWindow::builder()
            .hscrollbar_policy(PolicyType::Never)
            .vscrollbar_policy(PolicyType::Automatic)
            .propagate_natural_height(true)
            .max_content_height(TRAY_MAX_HEIGHT)
            .child(&list)
            .visible(false)
            .build();
        root.append(&scroller);

        let tray = Self {
            root,
            inner: Rc::new(Inner {
                header,
                scroller,
                list,
                state: RefCell::new(TrayState::default()),
                on_send: RefCell::new(None),
            }),
        };
        let t = tray.clone();
        tray.inner.header.connect_clicked(move |_| {
            {
                let mut state = t.inner.state.borrow_mut();
                state.expanded = !state.expanded;
            }
            t.render();
        });
        tray
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

    fn press(&self, slug: &str, action: Action) {
        let out = self.inner.state.borrow_mut().press(slug, action);
        if let Some(out) = out {
            let send = self.inner.on_send.borrow().clone();
            if let Some(send) = send {
                send(out);
            }
        }
        self.render();
    }

    fn render(&self) {
        let inner = &self.inner;
        let (items, expanded, editing) = {
            let state = inner.state.borrow();
            (state.items.clone(), state.expanded, state.editing.clone())
        };
        self.root.set_visible(!items.is_empty());
        let arrow = if expanded { "▾" } else { "▸" };
        inner.header.set_label(&format!("{}  {arrow}", tray_label(items.len())));
        inner.scroller.set_visible(expanded && !items.is_empty());
        while let Some(child) = inner.list.first_child() {
            inner.list.remove(&child);
        }
        if !expanded {
            return;
        }
        for item in &items {
            let edit = editing.as_ref().filter(|e| e.slug == item.slug);
            inner.list.append(&self.row(item, edit));
        }
    }

    fn row(&self, item: &Proposal, editing: Option<&Editing>) -> GtkBox {
        let row = GtkBox::new(Orientation::Vertical, 3);
        row.add_css_class("memory-item");
        row.append(&text_label(&item.title, "memory-title"));
        row.append(&text_label(&item.summary, "memory-summary"));
        if let Some(err) = &item.error {
            row.append(&text_label(err, "memory-error"));
        }
        if item.stale && editing.is_none() {
            row.append(&text_label("updating…", "memory-summary"));
        }

        if let Some(e) = editing {
            let view = TextView::builder()
                .wrap_mode(gtk4::WrapMode::WordChar)
                .monospace(true)
                .accepts_tab(false)
                .build();
            view.add_css_class("memory-edit");
            // A TextView measures its text lazily; without a minimum it starts 0 lines tall
            view.set_size_request(-1, EDIT_MIN_HEIGHT);
            view.buffer().set_text(&e.text);
            let t = self.clone();
            view.buffer().connect_changed(move |buf| {
                let text = buf.text(&buf.start_iter(), &buf.end_iter(), false);
                t.inner.state.borrow_mut().type_text(&text);
            });
            row.append(&view);
            let v = view.clone();
            glib::idle_add_local_once(move || {
                v.grab_focus();
            });
        } else if !item.stale {
            let diff = GtkBox::new(Orientation::Vertical, 0);
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
            row.append(&diff);
        }

        let buttons = GtkBox::new(Orientation::Horizontal, 6);
        buttons.add_css_class("memory-buttons");
        let actions = self.inner.state.borrow().actions(item);
        for (action, enabled) in actions {
            let b = button(
                action.label(),
                matches!(action, Action::Accept | Action::Save),
            );
            b.set_sensitive(enabled);
            let t = self.clone();
            let slug = item.slug.clone();
            b.connect_clicked(move |_| t.press(&slug, action));
            buttons.append(&b);
        }
        row.append(&buttons);
        row
    }
}

fn text_label(text: &str, class: &str) -> Label {
    let label = Label::builder()
        .label(text)
        .xalign(0.0)
        .wrap(true)
        .wrap_mode(gtk4::pango::WrapMode::WordChar)
        .max_width_chars(34)
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

    #[test]
    fn undo_labels() {
        assert_eq!(undo_label(MemoryKind::Remember), Some("Undo"));
        assert_eq!(undo_label(MemoryKind::Approved), Some("Undo"));
        assert_eq!(undo_label(MemoryKind::Undo), Some("Undo earlier"));
        assert_eq!(undo_label(MemoryKind::Other), None);
    }

    #[test]
    fn tray_label_plural() {
        assert_eq!(tray_label(1), "1 memory update");
        assert_eq!(tray_label(3), "3 memory updates");
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
    }
}


