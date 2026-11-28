//! The pop-out cards Appy throws next to himself (weather, now playing). Each is a small
//! square with the same look and pop-in/out; tick.rs moves them, and stacks them in one
//! column beside Appy when both are up (music on top).

use gtk4::prelude::*;
use gtk4::{Box as GtkBox, Orientation};

/// Card edge length in px (they're square).
pub const CARD_SIZE: f64 = 104.0;

/// Which card. The index is its slot in `SharedState::cards`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CardKind {
    Weather,
    Music,
}

impl CardKind {
    pub const ALL: [CardKind; 2] = [CardKind::Weather, CardKind::Music];

    pub fn index(self) -> usize {
        match self {
            CardKind::Weather => 0,
            CardKind::Music => 1,
        }
    }
}

/// The card's square body; its content is appended by the weather or music card.
pub struct CardShell {
    pub root: GtkBox,
}

impl CardShell {
    pub fn new() -> Self {
        let root = GtkBox::new(Orientation::Vertical, 0);
        root.add_css_class("card");
        root.set_size_request(CARD_SIZE as i32, CARD_SIZE as i32);
        root.set_visible(false);
        Self { root }
    }

    /// Make the card present but still scaled down; adding the "shown" class a frame
    /// later runs the CSS pop-in.
    pub fn appear(&self) {
        self.root.remove_css_class("shown");
        self.root.set_visible(true);
    }

    /// Start the exit animation; `hide` once it has run.
    pub fn pop_out(&self) {
        self.root.remove_css_class("shown");
    }

    pub fn hide(&self) {
        self.root.set_visible(false);
    }

    /// On screen and not on its way out: it should catch the pointer.
    pub fn is_present(&self) -> bool {
        self.root.is_visible() && self.root.has_css_class("shown")
    }
}
