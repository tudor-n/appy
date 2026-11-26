//! Screen geometry: where the mascot sits, which side of it the chat opens on, and
//! where the weather card goes. Pure functions over measured sizes, so it's all
//! unit-testable without GTK.
//!
//! The mascot's position (`Placement::anchor`) is the source of truth; the shell
//! (mascot column + chat panel) is positioned around it. This lets the mascot reach
//! every screen edge while the chat is hidden, and lets the chat swap sides when the
//! preferred side has no room, instead of the whole shell being blocked by the edges.

pub type Point = (f64, f64);
pub type Size = (f64, f64);

/// Shell's initial distance from the bottom-right corner of the screen.
const HOME_MARGIN_RIGHT: f64 = 24.0;
const HOME_MARGIN_BOTTOM: f64 = 56.0;
/// Space between the mascot body and the weather card.
const CARD_GAP: f64 = 14.0;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum HSide {
    /// Chat to the right of the mascot (preferred).
    #[default]
    Right,
    Left,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum VSide {
    /// Chat grows upward from the mascot's bottom edge (preferred).
    #[default]
    Up,
    /// Chat hangs downward from the mascot's top, for when Appy is near the top.
    Down,
}

/// Axis-aligned rectangle (x, y, w, h).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

impl Rect {
    pub fn center(&self) -> Point {
        (self.x + self.w * 0.5, self.y + self.h * 0.5)
    }

    /// How far the rect sticks out of a `win`-sized screen, summed over all edges.
    fn overflow(&self, win: Size) -> f64 {
        overflow(self.x, self.w, win.0) + overflow(self.y, self.h, win.1)
    }
}

/// How far [start, start + len] sticks out of [0, limit].
fn overflow(start: f64, len: f64, limit: f64) -> f64 {
    (-start).max(0.0) + (start + len - limit).max(0.0)
}

/// The shell's measured size and the mascot's offset inside it for each arrangement.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Shell {
    pub size: Size,
    pub mascot: Size,
    /// Mascot x offset with the chat on the [right, left].
    pub mascot_x: [f64; 2],
    /// Mascot y offset with the chat [above, below].
    pub mascot_y: [f64; 2],
}

impl Shell {
    pub fn offset(&self, h: HSide, v: VSide) -> Point {
        (self.mascot_x[h as usize], self.mascot_y[v as usize])
    }
}

#[derive(Debug, Default)]
pub struct Placement {
    /// Top-left of the mascot's drawing area, in window coordinates. Dragging moves it.
    pub anchor: Point,
    pub h: HSide,
    pub v: VSide,
    initialized: bool,
}

impl Placement {
    pub fn is_initialized(&self) -> bool {
        self.initialized
    }

    /// First layout: the bottom-right corner, with the chat fitting to its right.
    pub fn home(&mut self, win: Size, shell: &Shell, body: Rect) {
        let (ox, oy) = shell.offset(HSide::Right, VSide::Up);
        let anchor = (
            win.0 - shell.size.0 - HOME_MARGIN_RIGHT + ox,
            win.1 - shell.size.1 - HOME_MARGIN_BOTTOM + oy,
        );
        self.anchor = clamp_mascot(anchor, win, body);
        self.h = HSide::Right;
        self.v = VSide::Up;
        self.initialized = true;
    }

    /// Re-pick the chat's sides: right/above when they fit, otherwise whichever side
    /// sticks out less. Returns true when either side changed.
    pub fn choose_sides(&mut self, win: Size, shell: &Shell) -> bool {
        let x_overflow = |h: HSide| {
            overflow(
                self.anchor.0 - shell.mascot_x[h as usize],
                shell.size.0,
                win.0,
            )
        };
        let y_overflow = |v: VSide| {
            overflow(
                self.anchor.1 - shell.mascot_y[v as usize],
                shell.size.1,
                win.1,
            )
        };

        let h = if x_overflow(HSide::Right) <= x_overflow(HSide::Left) {
            HSide::Right
        } else {
            HSide::Left
        };
        let v = if y_overflow(VSide::Up) <= y_overflow(VSide::Down) {
            VSide::Up
        } else {
            VSide::Down
        };
        let changed = (h, v) != (self.h, self.v);
        (self.h, self.v) = (h, v);
        changed
    }

    /// Top-left of the shell that puts the mascot at the anchor.
    pub fn shell_origin(&self, shell: &Shell) -> Point {
        let (ox, oy) = shell.offset(self.h, self.v);
        (self.anchor.0 - ox, self.anchor.1 - oy)
    }
}

/// Keep the mascot's body on screen; its transparent padding may hang off the edge.
/// `body` is the body rect relative to the anchor.
pub fn clamp_mascot(anchor: Point, win: Size, body: Rect) -> Point {
    let clamp = |a: f64, off: f64, len: f64, limit: f64| {
        let (lo, hi) = (-off, limit - off - len);
        if hi < lo { lo } else { a.clamp(lo, hi) }
    };
    (
        clamp(anchor.0, body.x, body.w, win.0),
        clamp(anchor.1, body.y, body.h, win.1),
    )
}

/// Where the weather card should sit, given the mascot body in window coordinates.
///
/// Preference: beside the mascot on the side away from the chat, then above it, then
/// (if the chat is hidden) on the chat's side. The first spot that's fully on screen
/// wins; otherwise the one sticking out least.
pub fn card_slot(body: Rect, card: Size, win: Size, chat_side: HSide, chat_open: bool) -> Point {
    let (cw, ch) = card;
    let left = (body.x - CARD_GAP - cw, body.y + body.h - ch);
    let right = (body.x + body.w + CARD_GAP, body.y + body.h - ch);
    let above = (body.x + (body.w - cw) * 0.5, body.y - CARD_GAP - ch);

    let (away, toward) = match chat_side {
        HSide::Right => (left, right),
        HSide::Left => (right, left),
    };
    let mut candidates = vec![away, above];
    if !chat_open {
        candidates.push(toward);
    }

    let fit = |p: &Point| {
        Rect {
            x: p.0,
            y: p.1,
            w: cw,
            h: ch,
        }
        .overflow(win)
    };
    candidates
        .iter()
        .copied()
        .find(|p| fit(p) == 0.0)
        .unwrap_or_else(|| {
            candidates
                .iter()
                .copied()
                .min_by(|a, b| fit(a).total_cmp(&fit(b)))
                .expect("at least one candidate")
        })
}

/// One frame of easing toward a target point. Returns (new point, arrived).
pub fn ease_point(current: Point, target: Point, rate: f64) -> (Point, bool) {
    let (dx, dy) = (target.0 - current.0, target.1 - current.1);
    if dx.abs() < 0.5 && dy.abs() < 0.5 {
        (target, true)
    } else {
        ((current.0 + dx * rate, current.1 + dy * rate), false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WIN: Size = (1920.0, 1080.0);
    /// 120x136 mascot drawing area with a 96x96 body inset 12px from the sides and
    /// 28px from the top.
    const BODY: Rect = Rect {
        x: 12.0,
        y: 28.0,
        w: 96.0,
        h: 96.0,
    };

    /// A shell shaped like the real one: 120px mascot column + 300px chat + padding.
    fn shell() -> Shell {
        Shell {
            size: (490.0, 500.0),
            mascot: (120.0, 136.0),
            mascot_x: [34.0, 336.0],
            mascot_y: [334.0, 110.0],
        }
    }

    fn placement_at(x: f64, y: f64) -> Placement {
        Placement {
            anchor: (x, y),
            initialized: true,
            ..Default::default()
        }
    }

    #[test]
    fn homes_in_the_bottom_right_with_the_chat_on_screen() {
        let mut p = Placement::default();
        p.home(WIN, &shell(), BODY);
        let origin = p.shell_origin(&shell());
        assert_eq!(origin, (1920.0 - 490.0 - 24.0, 1080.0 - 500.0 - 56.0));
        assert_eq!((p.h, p.v), (HSide::Right, VSide::Up));
    }

    #[test]
    fn mascot_body_can_reach_every_edge() {
        // Body flush with the left/top edges: the transparent padding hangs off-screen
        assert_eq!(clamp_mascot((-500.0, -500.0), WIN, BODY), (-12.0, -28.0));
        // ...and with the right/bottom edges
        assert_eq!(
            clamp_mascot((5000.0, 5000.0), WIN, BODY),
            (1920.0 - 12.0 - 96.0, 1080.0 - 28.0 - 96.0)
        );
        // Anywhere in between is untouched
        assert_eq!(clamp_mascot((800.0, 400.0), WIN, BODY), (800.0, 400.0));
    }

    #[test]
    fn chat_prefers_right_and_above() {
        let mut p = placement_at(800.0, 700.0);
        assert!(!p.choose_sides(WIN, &shell()));
        assert_eq!((p.h, p.v), (HSide::Right, VSide::Up));
    }

    #[test]
    fn chat_flips_left_near_the_right_edge() {
        let mut p = placement_at(1900.0 - 120.0, 700.0);
        assert!(p.choose_sides(WIN, &shell()));
        assert_eq!(p.h, HSide::Left);
        // The mascot doesn't move; the chat lands fully on screen to its left
        let origin = p.shell_origin(&shell());
        assert_eq!(origin.0 + 336.0, 1780.0);
        assert!(origin.0 >= 0.0);
    }

    #[test]
    fn chat_flips_back_right_when_there_is_room_again() {
        let mut p = placement_at(1780.0, 700.0);
        p.choose_sides(WIN, &shell());
        assert_eq!(p.h, HSide::Left);
        p.anchor.0 = 900.0;
        assert!(p.choose_sides(WIN, &shell()));
        assert_eq!(p.h, HSide::Right);
    }

    #[test]
    fn chat_hangs_below_near_the_top_edge() {
        let mut p = placement_at(800.0, 20.0);
        p.choose_sides(WIN, &shell());
        assert_eq!(p.v, VSide::Down);
        let origin = p.shell_origin(&shell());
        assert!(origin.1 + 500.0 <= 1080.0);
    }

    #[test]
    fn corner_cases_pick_the_least_overflow() {
        // Top-right corner: chat goes left and down
        let mut p = placement_at(1800.0, 0.0);
        p.choose_sides(WIN, &shell());
        assert_eq!((p.h, p.v), (HSide::Left, VSide::Down));
        // Bottom-left corner: right and up
        let mut p = placement_at(0.0, 940.0);
        p.choose_sides(WIN, &shell());
        assert_eq!((p.h, p.v), (HSide::Right, VSide::Up));
    }

    #[test]
    fn card_sits_opposite_the_chat() {
        let body = Rect {
            x: 800.0,
            y: 600.0,
            w: 96.0,
            h: 96.0,
        };
        let card = (104.0, 104.0);
        let p = card_slot(body, card, WIN, HSide::Right, true);
        assert_eq!(p, (800.0 - 14.0 - 104.0, 600.0 + 96.0 - 104.0));
        let p = card_slot(body, card, WIN, HSide::Left, true);
        assert_eq!(p.0, 800.0 + 96.0 + 14.0);
    }

    #[test]
    fn card_goes_above_when_the_side_is_off_screen() {
        // Chat on the right, mascot at the left edge: no room on the left
        let body = Rect {
            x: 0.0,
            y: 600.0,
            w: 96.0,
            h: 96.0,
        };
        let p = card_slot(body, (104.0, 104.0), WIN, HSide::Right, true);
        assert_eq!(p, (-4.0, 600.0 - 14.0 - 104.0));
    }

    #[test]
    fn card_uses_the_chat_side_only_when_the_chat_is_hidden() {
        // Top-left corner: neither left nor above fit
        let body = Rect {
            x: 0.0,
            y: 0.0,
            w: 96.0,
            h: 96.0,
        };
        let p = card_slot(body, (104.0, 104.0), WIN, HSide::Right, false);
        assert_eq!(p.0, 96.0 + 14.0);
    }

    #[test]
    fn easing_arrives() {
        let (mut p, mut done) = ((0.0, 0.0), false);
        for _ in 0..200 {
            (p, done) = ease_point(p, (300.0, -120.0), 0.2);
            if done {
                break;
            }
        }
        assert!(done);
        assert_eq!(p, (300.0, -120.0));
    }
}
