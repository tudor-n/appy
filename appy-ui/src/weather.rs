//! The weather card: a small square Appy "throws" out next to himself when he checks
//! the weather. Its position is driven by tick.rs; the pop-in/out is CSS.
//!
//! The icon is painted with cairo in the mascot's own palette (ink black and the app's
//! grays) rather than using emoji, so it matches the rest of the UI.

use std::cell::Cell;
use std::f64::consts::{PI, TAU};
use std::rc::Rc;

use gtk4::cairo::{Context, LineCap, Operator};
use gtk4::prelude::*;
use gtk4::{Align, Box as GtkBox, DrawingArea, Label, Orientation};

use crate::protocol::{WeatherIcon, WeatherReport};

/// Card edge length in px (it's square).
pub const CARD_SIZE: f64 = 104.0;
const ICON_W: i32 = 48;
const ICON_H: i32 = 40;

/// The mascot's ink and the chat's grays (#141418, #636366, #8e8e93).
const INK: (f64, f64, f64) = (20.0 / 255.0, 20.0 / 255.0, 24.0 / 255.0);
const DARK_GRAY: (f64, f64, f64) = (99.0 / 255.0, 99.0 / 255.0, 102.0 / 255.0);
const GRAY: (f64, f64, f64) = (142.0 / 255.0, 142.0 / 255.0, 147.0 / 255.0);

pub struct WeatherCard {
    pub root: GtkBox,
    icon: DrawingArea,
    current_icon: Rc<Cell<WeatherIcon>>,
    temp: Label,
    place: Label,
}

impl WeatherCard {
    pub fn new() -> Self {
        let current_icon = Rc::new(Cell::new(WeatherIcon::Cloudy));
        let icon = DrawingArea::new();
        icon.set_content_width(ICON_W);
        icon.set_content_height(ICON_H);
        icon.set_halign(Align::Center);
        icon.set_draw_func({
            let current_icon = current_icon.clone();
            move |_, cr, w, h| draw_icon(cr, w as f64, h as f64, current_icon.get())
        });

        let temp = Label::new(None);
        temp.add_css_class("weather-temp");
        let place = Label::builder()
            .ellipsize(gtk4::pango::EllipsizeMode::End)
            .max_width_chars(14)
            .build();
        place.add_css_class("weather-place");

        let inner = GtkBox::new(Orientation::Vertical, 0);
        inner.set_valign(Align::Center);
        inner.set_halign(Align::Center);
        inner.set_vexpand(true);
        inner.append(&icon);
        inner.append(&temp);
        inner.append(&place);

        let root = GtkBox::new(Orientation::Vertical, 0);
        root.add_css_class("weather-card");
        root.set_size_request(CARD_SIZE as i32, CARD_SIZE as i32);
        root.append(&inner);
        root.set_visible(false);

        Self {
            root,
            icon,
            current_icon,
            temp,
            place,
        }
    }

    pub fn set(&self, report: &WeatherReport) {
        self.current_icon.set(report.icon);
        self.icon.queue_draw();
        self.temp
            .set_markup(&temp_markup(report.temp_c, report.low_c));
        let place = match &report.when {
            Some(day) => format!("{day} · {}", report.city),
            None => report.city.clone(),
        };
        self.place.set_text(&place);
        let temps = match report.low_c {
            Some(low) => format!("{} / {}", format_temp(report.temp_c), format_temp(low)),
            None => format_temp(report.temp_c),
        };
        self.root
            .set_tooltip_text(Some(&format!("{place}: {}, {temps}", report.condition)));
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

/// "21°" for now; "8° 2°" (low in gray, smaller) for a forecast day.
fn temp_markup(temp_c: f64, low_c: Option<f64>) -> String {
    match low_c {
        Some(low) => format!(
            "{}<span size=\"62%\" foreground=\"#8e8e93\"> {}</span>",
            format_temp(temp_c),
            format_temp(low)
        ),
        None => format_temp(temp_c),
    }
}

/// "21°", "-3°"; rounding never yields "-0°".
pub fn format_temp(temp_c: f64) -> String {
    let rounded = temp_c.round();
    let rounded = if rounded == 0.0 { 0.0 } else { rounded };
    format!("{rounded:.0}°")
}

fn set_color(cr: &Context, (r, g, b): (f64, f64, f64)) {
    cr.set_source_rgb(r, g, b);
}

fn draw_icon(cr: &Context, w: f64, h: f64, icon: WeatherIcon) {
    let (cx, cy) = (w * 0.5, h * 0.5);
    cr.set_line_cap(LineCap::Round);
    match icon {
        WeatherIcon::Clear => sun(cr, cx, cy, 1.0),
        WeatherIcon::ClearNight => moon(cr, cx, cy, 1.0),
        WeatherIcon::PartlyCloudy => {
            sun(cr, cx + 7.0, cy - 6.0, 0.75);
            cloud(cr, cx - 3.0, cy + 4.0, 0.8, GRAY);
        }
        WeatherIcon::PartlyCloudyNight => {
            moon(cr, cx + 7.0, cy - 6.0, 0.75);
            cloud(cr, cx - 3.0, cy + 4.0, 0.8, GRAY);
        }
        WeatherIcon::Cloudy | WeatherIcon::Unknown => {
            cloud(cr, cx + 6.0, cy - 5.0, 0.7, GRAY);
            cloud(cr, cx - 2.0, cy + 3.0, 0.9, DARK_GRAY);
        }
        WeatherIcon::Fog => {
            set_color(cr, GRAY);
            cr.set_line_width(3.0);
            for (dy, half) in [(-9.0, 14.0), (0.0, 18.0), (9.0, 11.0)] {
                cr.move_to(cx - half, cy + dy);
                cr.line_to(cx + half, cy + dy);
                let _ = cr.stroke();
            }
        }
        WeatherIcon::Drizzle => {
            cloud(cr, cx, cy - 5.0, 0.85, GRAY);
            set_color(cr, INK);
            for dx in [-8.0, 0.0, 8.0] {
                cr.arc(cx + dx, cy + 13.0 + (dx / 8.0).abs() * 2.0, 1.8, 0.0, TAU);
                let _ = cr.fill();
            }
        }
        WeatherIcon::Rain => {
            cloud(cr, cx, cy - 5.0, 0.85, GRAY);
            set_color(cr, INK);
            cr.set_line_width(2.2);
            for dx in [-8.0, 0.0, 8.0] {
                cr.move_to(cx + dx + 2.0, cy + 9.0);
                cr.line_to(cx + dx - 2.0, cy + 17.0);
                let _ = cr.stroke();
            }
        }
        WeatherIcon::Snow => {
            cloud(cr, cx, cy - 5.0, 0.85, GRAY);
            set_color(cr, INK);
            cr.set_line_width(1.6);
            for dx in [-9.0, 0.0, 9.0] {
                flake(cr, cx + dx, cy + 14.0, 3.2);
            }
        }
        WeatherIcon::Thunder => {
            cloud(cr, cx, cy - 6.0, 0.85, DARK_GRAY);
            set_color(cr, INK);
            cr.move_to(cx + 2.0, cy + 2.0);
            cr.line_to(cx - 5.0, cy + 12.0);
            cr.line_to(cx, cy + 12.0);
            cr.line_to(cx - 3.0, cy + 20.0);
            cr.line_to(cx + 6.0, cy + 8.0);
            cr.line_to(cx + 1.0, cy + 8.0);
            cr.close_path();
            let _ = cr.fill();
        }
    }
}

fn sun(cr: &Context, cx: f64, cy: f64, scale: f64) {
    set_color(cr, INK);
    cr.arc(cx, cy, 7.0 * scale, 0.0, TAU);
    let _ = cr.fill();
    cr.set_line_width(2.2 * scale);
    for i in 0..8 {
        let a = i as f64 * PI / 4.0;
        let (s, c) = a.sin_cos();
        cr.move_to(cx + c * 10.5 * scale, cy + s * 10.5 * scale);
        cr.line_to(cx + c * 14.0 * scale, cy + s * 14.0 * scale);
        let _ = cr.stroke();
    }
}

fn moon(cr: &Context, cx: f64, cy: f64, scale: f64) {
    set_color(cr, INK);
    cr.arc(cx, cy, 11.0 * scale, 0.0, TAU);
    let _ = cr.fill();
    // Bite out the crescent (the drawing area is transparent, so the card shows through)
    let _ = cr.save();
    cr.set_operator(Operator::Clear);
    cr.arc(cx + 6.0 * scale, cy - 4.0 * scale, 9.5 * scale, 0.0, TAU);
    let _ = cr.fill();
    let _ = cr.restore();
}

/// A cloud centred on (cx, cy): three puffs on a rounded base. Anything already drawn
/// under it is cleared first, so it reads cleanly in front of a sun or moon.
fn cloud(cr: &Context, cx: f64, cy: f64, scale: f64, color: (f64, f64, f64)) {
    let shape = |cr: &Context| {
        cr.new_sub_path();
        cr.arc(cx - 8.0 * scale, cy + 1.0 * scale, 7.0 * scale, 0.0, TAU);
        cr.new_sub_path();
        cr.arc(cx + 2.0 * scale, cy - 3.0 * scale, 9.0 * scale, 0.0, TAU);
        cr.new_sub_path();
        cr.arc(cx + 10.0 * scale, cy + 2.0 * scale, 6.0 * scale, 0.0, TAU);
        let (x, y, w, h, r) = (
            cx - 15.0 * scale,
            cy + 1.0 * scale,
            31.0 * scale,
            7.0 * scale,
            3.5 * scale,
        );
        cr.new_sub_path();
        cr.arc(x + w - r, y + r, r, -PI / 2.0, 0.0);
        cr.arc(x + w - r, y + h - r, r, 0.0, PI / 2.0);
        cr.arc(x + r, y + h - r, r, PI / 2.0, PI);
        cr.arc(x + r, y + r, r, PI, 3.0 * PI / 2.0);
        cr.close_path();
    };

    // Clear a slightly larger outline so the cloud sits in front of what's behind it
    let _ = cr.save();
    cr.set_operator(Operator::Clear);
    shape(cr);
    cr.set_line_width(4.0);
    let _ = cr.stroke_preserve();
    let _ = cr.fill();
    let _ = cr.restore();

    set_color(cr, color);
    shape(cr);
    let _ = cr.fill();
}

fn flake(cr: &Context, cx: f64, cy: f64, r: f64) {
    for i in 0..3 {
        let a = i as f64 * PI / 3.0;
        let (s, c) = a.sin_cos();
        cr.move_to(cx - c * r, cy - s * r);
        cr.line_to(cx + c * r, cy + s * r);
        let _ = cr.stroke();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn temperatures() {
        assert_eq!(format_temp(21.4), "21°");
        assert_eq!(format_temp(-2.6), "-3°");
        assert_eq!(format_temp(-0.3), "0°");
        assert_eq!(temp_markup(21.4, None), "21°");
        assert!(temp_markup(5.0, Some(-0.4)).starts_with("5°<span"));
        assert!(temp_markup(5.0, Some(-0.4)).contains("> 0°</span>"));
    }
}
