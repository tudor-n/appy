//! Widget tree construction. `Ui` holds a handle to every widget the app touches later.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use gtk4::cairo::{RectangleInt, Region};
use gtk4::gdk::{Display, Monitor, Rectangle};
use gtk4::prelude::*;
use gtk4::{
    Align, Application, ApplicationWindow, Box as GtkBox, CssProvider, DrawingArea, Label,
    Orientation, Overlay, STYLE_PROVIDER_PRIORITY_APPLICATION, glib,
};
use gtk4_layer_shell::{Edge, KeyboardMode, Layer, LayerShell};

use crate::chat::ChatLog;
use crate::memory::MemoryPanel;
use crate::input::{ChatInput, set_class};
use crate::layout::{self, HSide, Rect, Shell};
use crate::mascot;
use crate::card::{CardKind, CardShell};
use crate::music::MusicCard;
use crate::weather::WeatherCard;

const STYLE: &str = include_str!("style.css");
const CHAT_WIDTH: i32 = 290;

pub type IntRect = (i32, i32, i32, i32);

pub struct Ui {
    pub window: ApplicationWindow,
    /// Fullscreen overlay whose children (the shell and the weather card) are placed
    /// by hand. An Overlay sizes itself from its (empty) main child only, so children
    /// can hang off the screen edge without growing the window, unlike a GtkFixed.
    pub stage: Overlay,
    /// The visible shell: mascot column + chat panel.
    pub root: GtkBox,
    left_col: GtkBox,
    pub mascot: DrawingArea,
    pub chat_panel: GtkBox,
    pub chat: ChatLog,
    /// What the consolidator learned, waiting for review: floats above (or below) the
    /// chat, placed by tick.rs like the weather card.
    pub memory: MemoryPanel,
    pub input: ChatInput,
    pub card: WeatherCard,
    pub music_card: MusicCard,
    shell_pos: Rc<Cell<(i32, i32)>>,
    /// The shell's rect (x, y, w, h) in the last layout. Widget bounds on the stage are
    /// from that layout, which lags `shell_pos` by a frame while the shell moves.
    shell_laid_out: Rc<Cell<(i32, i32, i32, i32)>>,
    card_pos: [Rc<Cell<(i32, i32)>>; 2],
    memory_pos: Rc<Cell<(i32, i32)>>,
    /// Rewritten every frame while wobbling; layered above the static stylesheet.
    pub wobble_css: CssProvider,
}

impl Ui {
    pub fn build(app: &Application) -> Self {
        let wobble_css = install_styles();

        let window = ApplicationWindow::builder()
            .application(app)
            .decorated(false)
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
        let memory = MemoryPanel::new();
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
        root.set_margin_start(18);
        root.set_margin_end(18);
        root.set_margin_top(16);
        root.set_margin_bottom(16);
        root.append(&left_col);
        root.append(&chat_panel);

        let card = WeatherCard::new();
        let music_card = MusicCard::new();

        let filler = GtkBox::new(Orientation::Vertical, 0);
        filler.set_hexpand(true);
        filler.set_vexpand(true);
        filler.set_can_target(false);

        let stage = Overlay::new();
        stage.add_css_class("stage");
        stage.set_child(Some(&filler));
        stage.add_overlay(&root);
        stage.add_overlay(&card.shell.root);
        stage.add_overlay(&music_card.shell.root);
        stage.add_overlay(&memory.root);

        let shell_pos = Rc::new(Cell::new((0, 0)));
        let shell_laid_out = Rc::new(Cell::new((0, 0, 0, 0)));
        let card_pos = [Rc::new(Cell::new((0, 0))), Rc::new(Cell::new((0, 0)))];
        let memory_pos = Rc::new(Cell::new((0, 0)));
        stage.connect_get_child_position({
            let (root, card_root, music_root, memory_root) = (
                root.clone(),
                card.shell.root.clone(),
                music_card.shell.root.clone(),
                memory.root.clone(),
            );
            let (shell_pos, card_pos, memory_pos) =
                (shell_pos.clone(), card_pos.clone(), memory_pos.clone());
            let shell_laid_out = shell_laid_out.clone();
            move |_, child| {
                let (x, y) = if child == root.upcast_ref::<gtk4::Widget>() {
                    shell_pos.get()
                } else if child == card_root.upcast_ref::<gtk4::Widget>() {
                    card_pos[CardKind::Weather.index()].get()
                } else if child == music_root.upcast_ref::<gtk4::Widget>() {
                    card_pos[CardKind::Music.index()].get()
                } else if child == memory_root.upcast_ref::<gtk4::Widget>() {
                    memory_pos.get()
                } else {
                    return None;
                };
                if child == memory_root.upcast_ref::<gtk4::Widget>() {
                    let (w, h) = natural_height_size(child);
                    return Some(Rectangle::new(x, y, w, h));
                }
                // Minimum size, like the GtkFixed this replaced: it keeps the message
                // list as tall as the mascot column instead of its full 360px
                let (minimum, _) = child.preferred_size();
                if child == root.upcast_ref::<gtk4::Widget>() {
                    shell_laid_out.set((x, y, minimum.width(), minimum.height()));
                }
                Some(Rectangle::new(x, y, minimum.width(), minimum.height()))
            }
        });
        window.set_child(Some(&stage));

        Self {
            window,
            stage,
            root,
            left_col,
            mascot,
            chat_panel,
            chat,
            memory,
            input,
            card,
            music_card,
            shell_pos,
            shell_laid_out,
            card_pos,
            memory_pos,
            wobble_css,
        }
    }

    pub fn window_size(&self) -> (f64, f64) {
        (self.window.width() as f64, self.window.height() as f64)
    }

    pub fn move_shell(&self, (x, y): (f64, f64)) {
        let pos = (x.round() as i32, y.round() as i32);
        if self.shell_pos.replace(pos) != pos {
            self.stage.queue_allocate();
        }
    }

    /// The card of that kind (its square, pop-in/out and position).
    pub fn card_shell(&self, kind: CardKind) -> &CardShell {
        match kind {
            CardKind::Weather => &self.card.shell,
            CardKind::Music => &self.music_card.shell,
        }
    }

    pub fn move_card(&self, kind: CardKind, (x, y): (f64, f64)) {
        let pos = (x.round() as i32, y.round() as i32);
        if self.card_pos[kind.index()].replace(pos) != pos {
            self.stage.queue_allocate();
        }
    }

    pub fn move_memory(&self, (x, y): (f64, f64)) {
        let pos = (x.round() as i32, y.round() as i32);
        if self.memory_pos.replace(pos) != pos {
            self.stage.queue_allocate();
        }
    }

    /// The memory panel's size as laid out (see `natural_height_size`).
    pub fn memory_size(&self) -> (f64, f64) {
        let (w, h) = natural_height_size(self.memory.root.upcast_ref());
        (w as f64, h as f64)
    }

    /// What the memory panel lines up with: the chat panel, or the whole glass panel in
    /// glass mode. (While the chat slides in, the panel rides along with it.)
    pub fn memory_anchor(&self, locked: bool) -> Option<Rect> {
        if locked {
            self.shell_bounds()
        } else {
            self.in_shell(self.chat_panel.upcast_ref())
        }
    }

    /// Where `widget` (inside the shell) is on the stage, going by where the shell has
    /// just been put rather than where it was last laid out. While dragging, the stage's
    /// own layout is a frame behind, and anything placed from it trails and shakes.
    fn in_shell(&self, widget: &gtk4::Widget) -> Option<Rect> {
        let b = widget
            .compute_bounds(&self.stage)
            .filter(|b| b.width() > 0.0)?;
        let (dx, dy) = self.shell_moved();
        Some(Rect {
            x: b.x() as f64 + dx,
            y: b.y() as f64 + dy,
            w: b.width() as f64,
            h: b.height() as f64,
        })
    }

    /// How far the shell has been moved since it was last laid out.
    fn shell_moved(&self) -> (f64, f64) {
        let (px, py) = self.shell_pos.get();
        let (lx, ly, _, _) = self.shell_laid_out.get();
        ((px - lx) as f64, (py - ly) as f64)
    }

    /// When `pointer` (window coordinates) has left this window onto another monitor:
    /// that monitor, the pointer's position on it, and its size.
    pub fn monitor_across(&self, pointer: (f64, f64)) -> Option<(Monitor, (f64, f64), (f64, f64))> {
        let (w, h) = self.window_size();
        if pointer.0 >= 0.0 && pointer.0 < w && pointer.1 >= 0.0 && pointer.1 < h {
            return None;
        }
        let surface = self.window.surface()?;
        let display = surface.display();
        let here = display.monitor_at_surface(&surface)?.geometry();
        // The window starts at its monitor's top-left (a top panel would offset it a bit)
        let global = (here.x() as f64 + pointer.0, here.y() as f64 + pointer.1);

        let monitors: Vec<Monitor> = display
            .monitors()
            .iter::<Monitor>()
            .filter_map(Result::ok)
            .collect();
        let rects: Vec<Rect> = monitors
            .iter()
            .map(|m| {
                let g = m.geometry();
                Rect {
                    x: g.x() as f64,
                    y: g.y() as f64,
                    w: g.width() as f64,
                    h: g.height() as f64,
                }
            })
            .collect();
        let i = layout::monitor_at(global, &rects)?;
        let r = rects[i];
        let same = r.x == here.x() as f64 && r.y == here.y() as f64;
        (!same).then(|| (monitors[i].clone(), (global.0 - r.x, global.1 - r.y), (r.w, r.h)))
    }

    /// How tall the message log may grow: up to near the top of the screen, from where
    /// its bottom is now.
    pub fn log_room(&self) -> f64 {
        const TOP_GAP: f64 = 24.0;
        self.in_shell(self.chat.scrolled.upcast_ref())
            .map_or(0.0, |log| log.y + log.h - TOP_GAP)
    }

    /// The memory panel's rect, while it's shown.
    pub fn memory_bounds(&self) -> Option<Rect> {
        if !self.memory.is_shown() {
            return None;
        }
        let (x, y) = self.memory_pos.get();
        let (w, h) = self.memory_size();
        Some(Rect {
            x: x as f64,
            y: y as f64,
            w,
            h,
        })
    }

    /// The shell's visible rect (margins excluded): the glass panel in glass mode.
    pub fn shell_bounds(&self) -> Option<Rect> {
        self.in_shell(self.root.upcast_ref())
    }

    /// The mascot body relative to the mascot's own drawing area.
    pub fn body_in_mascot(&self) -> Rect {
        let (x, y, w, h) =
            mascot::body_rect(self.mascot.width() as f64, self.mascot.height() as f64);
        Rect { x, y, w, h }
    }

    /// Measure the shell, predicting where the mascot would sit with the chat on the
    /// other side (mirrored). None until the first layout.
    ///
    /// The shell's size is asked for fresh, but the mascot's place in it comes from the
    /// last layout, which can be a frame old (the shell just moved, flipped sides or grew).
    /// So it's measured inside the shell, where moving changes nothing; the side is read
    /// off that same layout; and the height from the bottom, where the mascot sits.
    pub fn measure_shell(&self) -> Option<Shell> {
        let (minimum, _) = self.root.preferred_size();
        let size = (minimum.width() as f64, minimum.height() as f64);
        let mascot = self.mascot.compute_bounds(&self.stage)?;
        let chat = self.chat_panel.compute_bounds(&self.stage)?;
        let (lx, ly, _, lh) = self.shell_laid_out.get();
        if size.0 <= 0.0 || mascot.width() <= 0.0 || lh <= 0 {
            return None;
        }

        // Offsets inside the shell as it was laid out (bounds and rect from one layout)
        let ox = mascot.x() as f64 - lx as f64;
        let mirrored = size.0 - ox - mascot.width() as f64;
        let laid_out_side = if mascot.x() < chat.x() {
            HSide::Right
        } else {
            HSide::Left
        };
        let mascot_x = match laid_out_side {
            HSide::Right => [ox, mirrored],
            HSide::Left => [mirrored, ox],
        };
        // The mascot sits at the bottom, so its distance from the bottom carries over
        let below = lh as f64 - (mascot.y() as f64 - ly as f64) - mascot.height() as f64;
        Some(Shell {
            size,
            mascot_x,
            mascot_y: size.1 - below - mascot.height() as f64,
        })
    }

    /// Put the chat on `side` of the mascot. When the chat is open, it replays its
    /// entrance on the new side instead of jumping across the mascot.
    pub fn apply_side(&self, side: HSide, chat_open: bool) {
        let left = side == HSide::Left;
        if left {
            self.root
                .reorder_child_after(&self.left_col, Some(&self.chat_panel));
        } else {
            self.root
                .reorder_child_after(&self.chat_panel, Some(&self.left_col));
        }
        set_class(&self.chat_panel, "left", left);

        if chat_open {
            self.chat_panel.add_css_class("flipping");
            let panel = self.chat_panel.clone();
            glib::timeout_add_local_once(Duration::from_millis(40), move || {
                panel.remove_css_class("flipping");
            });
        }
    }

    /// Rectangles that should catch the pointer: the whole surface while dragging (so
    /// the pointer can't escape the drag); otherwise the shell when the chat is open,
    /// or just the mascot, plus the cards that are up. Everything else is
    /// click-through.
    pub fn input_rects(&self, chat_open: bool, locked: bool, dragging: bool) -> Vec<IntRect> {
        if dragging {
            return vec![(0, 0, self.window.width(), self.window.height())];
        }
        let main: gtk4::Widget = if chat_open || locked {
            self.root.clone().upcast()
        } else {
            self.mascot.clone().upcast()
        };
        let mut rects: Vec<IntRect> = self.bounds_of(&main).into_iter().collect();
        for kind in CardKind::ALL {
            let card = self.card_shell(kind);
            if card.is_present() {
                rects.extend(self.bounds_of(card.root.upcast_ref()));
            }
        }
        if self.memory.is_shown() {
            rects.extend(self.bounds_of(self.memory.root.upcast_ref()));
        }
        rects
    }

    fn bounds_of(&self, widget: &gtk4::Widget) -> Option<IntRect> {
        widget.compute_bounds(&self.window).map(|b| {
            (
                b.x().floor() as i32,
                b.y().floor() as i32,
                b.width().ceil() as i32,
                b.height().ceil() as i32,
            )
        })
    }

    /// Restrict the Wayland input region so clicks outside it fall through to the desktop.
    pub fn set_input_region(&self, rects: &[IntRect]) {
        let Some(surface) = self.window.surface() else {
            return;
        };
        let region = Region::create();
        for &(x, y, w, h) in rects {
            let _ = region.union_rectangle(&RectangleInt::new(x, y, w, h));
        }
        surface.set_input_region(&region);
    }

    pub fn update_input_region(&self, chat_open: bool, locked: bool, dragging: bool) {
        self.set_input_region(&self.input_rects(chat_open, locked, dragging));
    }
}

/// The memory panel at its minimum width but natural height: at its minimum height the
/// note's diff (a scroller) would shrink to nothing.
fn natural_height_size(widget: &gtk4::Widget) -> (i32, i32) {
    let (min_w, _, _, _) = widget.measure(Orientation::Horizontal, -1);
    let (_, nat_h, _, _) = widget.measure(Orientation::Vertical, min_w);
    (min_w, nat_h)
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
