//! Appy's desktop body: a transparent layer-shell overlay with the mascot and chat,
//! talking to the Python brain over WebSocket and to the KDE panel pill over HTTP.
//!
//! Module map:
//! - `app`      App: owns the widgets, state and channels
//! - `ui`       widget tree, layer-shell setup, click-through input region
//! - `chat`     message log (bubbles, status pills, smooth scroll)
//! - `input`    message field + effort picker
//! - `mascot`   cairo drawing
//! - `state`    plain-data UI state and animation physics
//! - `events`   backend event -> state/UI
//! - `controls` pointer and keyboard handling
//! - `tick`     per-frame loop
//! - `net`      WebSocket client + panel pill HTTP bridge (Tokio thread)
//! - `protocol` wire types

mod app;
mod chat;
mod controls;
mod events;
mod input;
mod mascot;
mod net;
mod protocol;
mod state;
mod tick;
mod ui;

use gtk4::prelude::*;
use gtk4::{Application, glib};

fn main() -> glib::ExitCode {
    let gtk_app = Application::builder()
        .application_id("dev.appy.overlay")
        .build();

    gtk_app.connect_activate(|gtk_app| {
        // Launching appy-ui again re-activates this instance; keep the single overlay
        // instead of building a second one that fights over the ports.
        if !gtk_app.windows().is_empty() {
            return;
        }
        app::App::build(gtk_app);
    });

    gtk_app.run()
}
