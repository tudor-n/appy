//! The now-playing face of Appy's card: what Riff is playing, shown when Appy starts
//! music or is asked what's on. It lives in the weather card's slot (weather.rs swaps
//! between the two), so it flies out and back exactly like the weather does.
//!
//! The glyph is cairo in the mascot's ink, like the weather icons: an eighth note while
//! playing, two bars while paused. No album art, so the card stays in the UI's palette.

use std::cell::Cell;
use std::f64::consts::{PI, TAU};
use std::rc::Rc;

use gtk4::cairo::{Context, LineCap};
use gtk4::prelude::*;
use gtk4::{Align, Box as GtkBox, DrawingArea, Label, Orientation};

use crate::protocol::{MusicReport, PlaybackState};
use crate::weather::INK;

const GLYPH_W: i32 = 40;
const GLYPH_H: i32 = 30;

pub struct MusicFace {
    pub root: GtkBox,
    glyph: DrawingArea,
    state: Rc<Cell<PlaybackState>>,
    title: Label,
    artist: Label,
}

impl MusicFace {
    pub fn new() -> Self {
        let state = Rc::new(Cell::new(PlaybackState::Playing));
        let glyph = DrawingArea::new();
        glyph.set_content_width(GLYPH_W);
        glyph.set_content_height(GLYPH_H);
        glyph.set_halign(Align::Center);
        glyph.set_draw_func({
            let state = state.clone();
            move |_, cr, w, h| draw_glyph(cr, w as f64, h as f64, state.get())
        });

        let title = Label::builder()
            .wrap(true)
            .wrap_mode(gtk4::pango::WrapMode::WordChar)
            .lines(2)
            .ellipsize(gtk4::pango::EllipsizeMode::End)
            .max_width_chars(12)
            .justify(gtk4::Justification::Center)
            .build();
        title.add_css_class("music-title");
        let artist = Label::builder()
            .ellipsize(gtk4::pango::EllipsizeMode::End)
            .max_width_chars(14)
            .build();
        artist.add_css_class("music-artist");

        let root = GtkBox::new(Orientation::Vertical, 0);
        root.set_valign(Align::Center);
        root.set_halign(Align::Center);
        root.set_vexpand(true);
        root.append(&glyph);
        root.append(&title);
        root.append(&artist);

        Self {
            root,
            glyph,
            state,
            title,
            artist,
        }
    }

    pub fn set(&self, track: &MusicReport) {
        self.state.set(track.state);
        self.glyph.queue_draw();
        self.title.set_text(&track.title);
        self.artist.set_text(&track.artist);
        self.artist.set_visible(!track.artist.is_empty());
    }

    /// "Karma Police by Radiohead" (paused) for the card's tooltip.
    pub fn tooltip(track: &MusicReport) -> String {
        let what = if track.artist.is_empty() {
            track.title.clone()
        } else {
            format!("{} by {}", track.title, track.artist)
        };
        match track.state {
            PlaybackState::Playing => what,
            _ => format!("{what} (paused)"),
        }
    }
}

fn draw_glyph(cr: &Context, w: f64, h: f64, state: PlaybackState) {
    let (cx, cy) = (w * 0.5, h * 0.5);
    cr.set_source_rgb(INK.0, INK.1, INK.2);
    cr.set_line_cap(LineCap::Round);
    match state {
        PlaybackState::Playing => note(cr, cx, cy),
        PlaybackState::Paused | PlaybackState::Unknown => pause_bars(cr, cx, cy),
    }
}

/// An eighth note: a tilted oval head, a stem, and a flag curling off its top.
fn note(cr: &Context, cx: f64, cy: f64) {
    let (head_x, head_y) = (cx - 3.0, cy + 8.0);
    let _ = cr.save();
    cr.translate(head_x, head_y);
    cr.rotate(-PI / 7.0);
    cr.scale(1.35, 1.0);
    cr.arc(0.0, 0.0, 4.6, 0.0, TAU);
    let _ = cr.restore();
    let _ = cr.fill();

    let stem_x = head_x + 5.6;
    cr.set_line_width(2.4);
    cr.move_to(stem_x, head_y - 1.0);
    cr.line_to(stem_x, cy - 12.0);
    let _ = cr.stroke();

    cr.set_line_width(2.6);
    cr.move_to(stem_x, cy - 12.0);
    cr.curve_to(stem_x + 2.0, cy - 6.0, stem_x + 10.0, cy - 6.0, stem_x + 7.0, cy + 2.0);
    let _ = cr.stroke();
}

/// Two rounded bars.
fn pause_bars(cr: &Context, cx: f64, cy: f64) {
    cr.set_line_width(5.0);
    for dx in [-5.0, 5.0] {
        cr.move_to(cx + dx, cy - 9.0);
        cr.line_to(cx + dx, cy + 9.0);
        let _ = cr.stroke();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn track(state: PlaybackState, artist: &str) -> MusicReport {
        MusicReport {
            title: "Karma Police".into(),
            artist: artist.into(),
            state,
        }
    }

    #[test]
    fn tooltip_names_track_and_pause() {
        assert_eq!(
            MusicFace::tooltip(&track(PlaybackState::Playing, "Radiohead")),
            "Karma Police by Radiohead"
        );
        assert_eq!(
            MusicFace::tooltip(&track(PlaybackState::Paused, "")),
            "Karma Police (paused)"
        );
    }
}
