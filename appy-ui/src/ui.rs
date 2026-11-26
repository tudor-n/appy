//! Widget tree construction. `Ui` holds a handle to every widget the app touches later.

use gtk4::cairo::{RectangleInt, Region};
use gtk4::gdk::Display;
use gtk4::prelude::*;
use gtk4::{
    Align, Application, ApplicationWindow, Box as GtkBox, CssProvider, DrawingArea, Fixed, Label,
    Orientation, STYLE_PROVIDER_PRIORITY_APPLICATION,
};
use gtk4_layer_shell::{Edge, KeyboardMode, Layer, LayerShell};

use crate::chat::ChatLog;
use crate::input::ChatInput;

const STYLE: &str = include_str!("style.css");
const CHAT_WIDTH: i32 = 290;

pub struct Ui {
    pub window: ApplicationWindow,
    /// Fullscreen container; the shell is positioned inside it by hand.
    pub fixed: Fixed,
    /// The visible shell: mascot column + chat panel.
    pub root: GtkBox,
    pub mascot: DrawingArea,
    pub chat_panel: GtkBox,
    pub chat: ChatLog,
    pub input: ChatInput,
    /// Rewritten every frame while wobbling; layered above the static stylesheet.
    pub wobble_css: CssProvider,
}

impl Ui {
    pub fn build(app: &Application) -> Self {
        let wobble_css = install_styles();

        let window = ApplicationWindow::builder()
            .application(app)
            .decorated(false)
            .resizable(false)
            .build();
        init_layer_shell(&window);

        let mascot = DrawingArea::new();
        mascot.set_content_width(120);
        mascot.set_content_height(136);
        mascot.set_vexpand(true);
        mascot.set_valign(Align::End);

        let left_col = GtkBox::new(Orientation::Vertical, 6);
        left_col.set_valign(Align::Fill);
        left_col.append(&brand_header());
        left_col.append(&mascot);

        let chat = ChatLog::new();
        let input = ChatInput::new();

        // Wobble transforms are applied to this box (see tick.rs)
        let chat_wobble_box = GtkBox::new(Orientation::Vertical, 8);
        chat_wobble_box.add_css_class("chat-wobble");
        chat_wobble_box.append(&chat.scrolled);
        chat_wobble_box.append(&input.container);

        let chat_panel = GtkBox::new(Orientation::Vertical, 0);
        chat_panel.add_css_class("chat-panel");
        chat_panel.set_width_request(CHAT_WIDTH);
        chat_panel.set_valign(Align::End);
        chat_panel.set_sensitive(false);
        chat_panel.append(&chat_wobble_box);

        let root = GtkBox::new(Orientation::Horizontal, 10);
        root.add_css_class("appy-shell");
        root.set_valign(Align::End);
        root.set_margin_start(18);
        root.set_margin_end(18);
        root.set_margin_top(16);
        root.set_margin_bottom(16);
        root.append(&left_col);
        root.append(&chat_panel);

        // Fullscreen Fixed container keeps the layer-shell surface stationary for 1:1 drag coords
        let fixed = Fixed::new();
        fixed.put(&root, 0.0, 0.0);
        window.set_child(Some(&fixed));

        Self {
            window,
            fixed,
            root,
            mascot,
            chat_panel,
            chat,
            input,
            wobble_css,
        }
    }

    pub fn move_shell(&self, x: f64, y: f64) {
        self.fixed.move_(&self.root, x, y);
    }

    /// The widget whose bounds should catch the pointer. While the chat is hidden only
    /// the mascot does; the invisible message column must stay click-through.
    pub fn input_target(&self, chat_open: bool, locked: bool) -> gtk4::Widget {
        if chat_open || locked {
            self.root.clone().upcast()
        } else {
            self.mascot.clone().upcast()
        }
    }

    /// Bounds the input region should cover: the whole surface while dragging (so the
    /// pointer can't escape the drag), otherwise `target`'s bounds.
    pub fn input_bounds(
        &self,
        target: &gtk4::Widget,
        dragging: bool,
    ) -> Option<(i32, i32, i32, i32)> {
        if dragging {
            return Some((0, 0, self.window.width(), self.window.height()));
        }
        target.compute_bounds(&self.window).map(|b| {
            (
                b.x().floor() as i32,
                b.y().floor() as i32,
                b.width().ceil() as i32,
                b.height().ceil() as i32,
            )
        })
    }

    /// Restrict the Wayland input region so clicks outside it fall through to the desktop.
    pub fn set_input_region(&self, bounds: (i32, i32, i32, i32)) {
        let Some(surface) = self.window.surface() else {
            return;
        };
        let (x, y, w, h) = bounds;
        surface.set_input_region(&Region::create_rectangle(&RectangleInt::new(x, y, w, h)));
    }

    pub fn update_input_region(&self, chat_open: bool, locked: bool, dragging: bool) {
        let target = self.input_target(chat_open, locked);
        if let Some(bounds) = self.input_bounds(&target, dragging) {
            self.set_input_region(bounds);
        }
    }
}

/// Installs the static stylesheet and returns the (initially empty) wobble provider.
fn install_styles() -> CssProvider {
    let display = Display::default().expect("no default display");

    let css = CssProvider::new();
    css.load_from_data(STYLE);
    gtk4::style_context_add_provider_for_display(
        &display,
        &css,
        STYLE_PROVIDER_PRIORITY_APPLICATION,
    );

    let wobble_css = CssProvider::new();
    gtk4::style_context_add_provider_for_display(
        &display,
        &wobble_css,
        STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
    );
    wobble_css
}

/// A transparent fullscreen overlay above normal windows, taking keyboard focus only
/// when clicked.
fn init_layer_shell(window: &ApplicationWindow) {
    window.init_layer_shell();
    window.set_namespace(Some("appy-overlay"));
    window.set_layer(Layer::Top);
    for edge in [Edge::Top, Edge::Bottom, Edge::Left, Edge::Right] {
        window.set_anchor(edge, true);
    }
    window.set_exclusive_zone(0);
    window.set_keyboard_mode(KeyboardMode::OnDemand);
}

/// "Appy / AI assistant" title, shown only in glass (locked) mode.
fn brand_header() -> GtkBox {
    let title = Label::builder()
        .label("Appy")
        .xalign(0.0)
        .halign(Align::Start)
        .build();
    title.add_css_class("brand-title");

    let subtitle = Label::builder()
        .label("AI assistant v4.0")
        .xalign(0.0)
        .halign(Align::Start)
        .build();
    subtitle.add_css_class("brand-subtitle");

    let header = GtkBox::new(Orientation::Vertical, 1);
    header.add_css_class("brand-header");
    header.set_halign(Align::Start);
    header.set_valign(Align::Start);
    header.set_can_target(false);
    header.append(&title);
    header.append(&subtitle);
    header
}
