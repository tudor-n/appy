//! Long-term memory in the overlay: the review tray (what the consolidator learned,
//! waiting for Accept / Deny / Edit) and the chips for memory commits.

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use gtk4::prelude::*;
use gtk4::{Align, Box as GtkBox, Button, Label, Orientation, PolicyType, ScrolledWindow, TextView};

use crate::protocol::{Decision, MemoryKind, Proposal};

const TRAY_MAX_HEIGHT: i32 = 260;
const EDIT_MIN_HEIGHT: i32 = 140;

type DecideFn = dyn Fn(String, Decision, Option<String>);

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
    expanded: Cell<bool>,
    items: RefCell<Vec<Proposal>>,
    /// The proposal being edited and the text typed so far (kept across refreshes).
    editing: RefCell<Option<(String, String)>>,
    /// Proposals a button was pressed on; their buttons stay disabled until the next list.
    pending: RefCell<Vec<String>>,
    on_decide: RefCell<Option<Rc<DecideFn>>>,
}

impl MemoryTray {
    pub fn new() -> Self {
        let root = GtkBox::new(Orientation::Vertical, 4);
        root.add_css_class("memory-tray");
        root.set_visible(false);

        let header = Button::new();
        header.add_css_class("memory-header");
        header.set_halign(Align::Fill);
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
                expanded: Cell::new(false),
                items: RefCell::new(Vec::new()),
                editing: RefCell::new(None),
                pending: RefCell::new(Vec::new()),
                on_decide: RefCell::new(None),
            }),
        };
        let t = tray.clone();
        tray.inner.header.connect_clicked(move |_| {
            t.inner.expanded.set(!t.inner.expanded.get());
            t.render();
        });
        tray
    }

    /// Where Accept / Deny / Edit go (the App sends them to the backend).
    pub fn set_on_decide(&self, f: impl Fn(String, Decision, Option<String>) + 'static) {
        *self.inner.on_decide.borrow_mut() = Some(Rc::new(f));
    }

    /// Show the current list (the backend always sends all of it).
    pub fn set_items(&self, items: &[Proposal]) {
        let mut editing = self.inner.editing.borrow_mut();
        if editing.as_ref().is_some_and(|(slug, _)| !items.iter().any(|i| &i.slug == slug)) {
            *editing = None;
        }
        drop(editing);
        self.inner.pending.borrow_mut().clear();
        *self.inner.items.borrow_mut() = items.to_vec();
        self.render();
    }

    fn decide(&self, slug: &str, decision: Decision, text: Option<String>) {
        self.inner.pending.borrow_mut().push(slug.to_string());
        if decision == Decision::Edit {
            *self.inner.editing.borrow_mut() = None;
        }
        let handler = self.inner.on_decide.borrow().clone();
        if let Some(f) = handler {
            f(slug.to_string(), decision, text);
        }
        self.render();
    }

    fn render(&self) {
        let inner = &self.inner;
        let items = inner.items.borrow().clone();
        self.root.set_visible(!items.is_empty());
        let arrow = if inner.expanded.get() { "▾" } else { "▸" };
        inner.header.set_label(&format!("{}  {arrow}", tray_label(items.len())));
        inner.scroller.set_visible(inner.expanded.get() && !items.is_empty());
        while let Some(child) = inner.list.first_child() {
            inner.list.remove(&child);
        }
        if inner.expanded.get() {
            for item in &items {
                inner.list.append(&self.row(item));
            }
        }
    }

    fn row(&self, item: &Proposal) -> GtkBox {
        let row = GtkBox::new(Orientation::Vertical, 3);
        row.add_css_class("memory-item");
        row.append(&text_label(&item.title, "memory-title"));
        row.append(&text_label(&item.summary, "memory-summary"));
        if let Some(err) = &item.error {
            row.append(&text_label(err, "memory-error"));
        }
        if item.stale {
            row.append(&text_label("updating…", "memory-summary"));
            return row;
        }

        let editing = self.inner.editing.borrow().clone();
        let busy = self.inner.pending.borrow().contains(&item.slug);
        let buttons = GtkBox::new(Orientation::Horizontal, 6);
        buttons.add_css_class("memory-buttons");

        if let Some((_, text)) = editing.filter(|(slug, _)| slug == &item.slug) {
            let view = TextView::builder()
                .wrap_mode(gtk4::WrapMode::WordChar)
                .monospace(true)
                .accepts_tab(false)
                .build();
            view.add_css_class("memory-edit");
            // A TextView measures its text lazily; without a minimum it starts 0 lines tall
            view.set_size_request(-1, EDIT_MIN_HEIGHT);
            view.buffer().set_text(&text);
            let t = self.clone();
            let slug = item.slug.clone();
            view.buffer().connect_changed(move |buf| {
                let text = buf.text(&buf.start_iter(), &buf.end_iter(), false).to_string();
                *t.inner.editing.borrow_mut() = Some((slug.clone(), text));
            });
            row.append(&view);

            let save = button("Save", true);
            let t = self.clone();
            let slug = item.slug.clone();
            save.connect_clicked(move |_| {
                let text = t.inner.editing.borrow().as_ref().map(|(_, text)| text.clone());
                t.decide(&slug, Decision::Edit, text);
            });
            let cancel = button("Cancel", false);
            let t = self.clone();
            cancel.connect_clicked(move |_| {
                *t.inner.editing.borrow_mut() = None;
                t.render();
            });
            buttons.append(&save);
            buttons.append(&cancel);
            row.append(&buttons);
            return row;
        }

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

        for (name, decision, primary) in [
            ("Accept", Decision::Accept, true),
            ("Deny", Decision::Deny, false),
            ("Edit", Decision::Edit, false),
        ] {
            let b = button(name, primary);
            b.set_sensitive(!busy);
            let t = self.clone();
            let slug = item.slug.clone();
            let after = item.after.clone();
            b.connect_clicked(move |_| match decision {
                Decision::Edit => {
                    *t.inner.editing.borrow_mut() = Some((slug.clone(), after.clone()));
                    t.render();
                }
                _ => t.decide(&slug, decision, None),
            });
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

