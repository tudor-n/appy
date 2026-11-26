//! The weather card: a small square Appy "throws" out next to himself when he checks
//! the weather. Its position is driven by tick.rs; the pop-in/out is CSS.

use gtk4::prelude::*;
use gtk4::{Align, Box as GtkBox, Label, Orientation};

/// Card edge length in px (it's square).
pub const CARD_SIZE: f64 = 104.0;

pub struct WeatherCard {
    pub root: GtkBox,
    emoji: Label,
    temp: Label,
    place: Label,
}

impl WeatherCard {
    pub fn new() -> Self {
        let emoji = Label::new(None);
        emoji.add_css_class("weather-emoji");
        let temp = Label::new(None);
        temp.add_css_class("weather-temp");
        let place = Label::builder()
            .ellipsize(gtk4::pango::EllipsizeMode::End)
            .max_width_chars(12)
            .build();
        place.add_css_class("weather-place");

        let inner = GtkBox::new(Orientation::Vertical, 0);
        inner.set_valign(Align::Center);
        inner.set_halign(Align::Center);
        inner.set_vexpand(true);
        inner.append(&emoji);
        inner.append(&temp);
        inner.append(&place);

        let root = GtkBox::new(Orientation::Vertical, 0);
        root.add_css_class("weather-card");
        root.set_size_request(CARD_SIZE as i32, CARD_SIZE as i32);
        root.append(&inner);
        root.set_visible(false);

        Self {
            root,
            emoji,
            temp,
            place,
        }
    }

    pub fn set(&self, emoji: &str, temp_c: f64, city: &str, condition: &str) {
        self.emoji.set_text(emoji);
        self.temp.set_text(&format_temp(temp_c));
        self.place.set_text(city);
        self.root.set_tooltip_text(Some(&format!(
            "{city}: {condition}, {}",
            format_temp(temp_c)
        )));
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

/// "21°", "-3°"; rounding never yields "-0°".
pub fn format_temp(temp_c: f64) -> String {
    let rounded = temp_c.round();
    let rounded = if rounded == 0.0 { 0.0 } else { rounded };
    format!("{rounded:.0}°")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn temperatures() {
        assert_eq!(format_temp(21.4), "21°");
        assert_eq!(format_temp(-2.6), "-3°");
        assert_eq!(format_temp(-0.3), "0°");
    }
}
