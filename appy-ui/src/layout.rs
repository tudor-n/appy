//! Screen geometry: where the mascot sits, which side of it the chat opens on, and
//! where the weather card goes. Pure functions over measured sizes, so it's all
//! unit-testable without GTK.
//!
//! The mascot's position (`Placement::anchor`) is the source of truth; the shell
//! (mascot column + chat panel) is positioned around it. This lets the mascot reach
//! every screen edge while the chat is hidden, and lets the chat open on the mascot's
//! left when there's no room on the right, instead of the shell hitting the edge.

pub type Point = (f64, f64);
pub type Size = (f64, f64);

/// Shell's initial distance from the bottom-right corner of the screen.
const HOME_MARGIN_RIGHT: f64 = 24.0;
const HOME_MARGIN_BOTTOM: f64 = 56.0;
/// While dragging with the chat open, the chat moves to the left once it would stick
/// out this far past the right edge, and comes back as soon as the right side has
/// this much room to spare. The gap between the two keeps it from flickering at the
/// boundary.
const FLIP_MARGIN: f64 = 16.0;
/// Space between the mascot body and the weather card.
const CARD_GAP: f64 = 14.0;
/// Space between the chat panel and the memory panel.
pub const MEMORY_GAP: f64 = 10.0;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum HSide {
    /// Chat to the right of the mascot (preferred).
    #[default]
    Right,
    Left,
}

/// Which side of the chat the memory panel is on.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum VSide {
    /// Above the chat (preferred).
    #[default]
    Above,
    Below,
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

    fn intersects(&self, other: &Rect) -> bool {
        self.x < other.x + other.w
            && other.x < self.x + self.w
            && self.y < other.y + other.h
            && other.y < self.y + self.h
    }
}

/// How far [start, start + len] sticks out of [0, limit].
fn overflow(start: f64, len: f64, limit: f64) -> f64 {
    (-start).max(0.0) + (start + len - limit).max(0.0)
}

/// The shell's measured size and the mascot's offset inside it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Shell {
    pub size: Size,
    /// Mascot x offset with the chat on the [right, left].
    pub mascot_x: [f64; 2],
    pub mascot_y: f64,
}

impl Shell {
    pub fn offset(&self, h: HSide) -> Point {
        (self.mascot_x[h as usize], self.mascot_y)
    }
}

#[derive(Debug, Default)]
pub struct Placement {
    /// Top-left of the mascot's drawing area, in window coordinates. Dragging moves it.
    pub anchor: Point,
    /// Which side of the mascot the chat is on.
    pub side: HSide,
    initialized: bool,
}

impl Placement {
    pub fn is_initialized(&self) -> bool {
        self.initialized
    }

    /// First layout: the bottom-right corner, with the chat fitting to its right.
    pub fn home(&mut self, win: Size, shell: &Shell, body: Rect) {
        let (ox, oy) = shell.offset(HSide::Right);
        let anchor = (
            win.0 - shell.size.0 - HOME_MARGIN_RIGHT + ox,
            win.1 - shell.size.1 - HOME_MARGIN_BOTTOM + oy,
        );
        self.anchor = clamp_mascot(anchor, win, body);
        self.side = HSide::Right;
        self.initialized = true;
    }

    /// Re-pick the chat's side. Normally: the right if it fits, otherwise whichever
    /// side sticks out less. `sticky` (dragging with the chat open) adds a little
    /// hysteresis around the right edge so the chat doesn't flicker there. Returns true
    /// when the side changed.
    pub fn choose_side(&mut self, win: Size, shell: &Shell, sticky: bool) -> bool {
        let sticks_out = |h: HSide| {
            overflow(
                self.anchor.0 - shell.mascot_x[h as usize],
                shell.size.0,
                win.0,
            )
        };
        let (right, left) = (sticks_out(HSide::Right), sticks_out(HSide::Left));
        // How far the chat-on-the-right shell passes the screen's right edge
        // (negative: room to spare)
        let past_right_edge =
            self.anchor.0 - shell.mascot_x[HSide::Right as usize] + shell.size.0 - win.0;

        let side = if sticky {
            match self.side {
                HSide::Right if past_right_edge > FLIP_MARGIN && left < right => HSide::Left,
                HSide::Left if past_right_edge < -FLIP_MARGIN => HSide::Right,
                current => current,
            }
        } else if right <= left {
            HSide::Right
        } else {
            HSide::Left
        };
        let changed = side != self.side;
        self.side = side;
        changed
    }

    /// Top-left of the shell that puts the mascot at the anchor.
    pub fn shell_origin(&self, shell: &Shell) -> Point {
        let (ox, oy) = shell.offset(self.side);
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

/// Where the weather card should sit. `body` is the mascot body in window coordinates;
/// the card stays clear of `clear_of` (the body itself, or the whole glass panel in
/// glass mode) and is bottom-aligned with the body.
///
/// Preference: beside it on the side away from the chat, then above it, then (if the
/// chat is hidden) on the chat's side. The first spot that's fully on screen (and clear
/// of `avoid`, the memory panel) wins; otherwise the one sticking out least.
pub fn card_slot(
    body: Rect,
    clear_of: Rect,
    card: Size,
    win: Size,
    chat_side: HSide,
    chat_open: bool,
    avoid: Option<Rect>,
) -> Point {
    let (cw, ch) = card;
    let bottom = body.y + body.h - ch;
    let left = (clear_of.x - CARD_GAP - cw, bottom);
    let right = (clear_of.x + clear_of.w + CARD_GAP, bottom);
    let above = (body.x + (body.w - cw) * 0.5, clear_of.y - CARD_GAP - ch);

    let (away, toward) = match chat_side {
        HSide::Right => (left, right),
        HSide::Left => (right, left),
    };
    let mut candidates = vec![away, above];
    if !chat_open {
        candidates.push(toward);
    }

    let fit = |p: &Point| {
        let r = Rect {
            x: p.0,
            y: p.1,
            w: cw,
            h: ch,
        };
        // Covering the memory panel counts like sticking out by the card's size
        let blocked = avoid.is_some_and(|a| r.intersects(&a));
        r.overflow(win) + if blocked { cw + ch } else { 0.0 }
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

/// Where the memory panel goes: above the chat (or below it when there's no room
/// above), with its outer edge (the one away from Appy) lined up with the chat's, kept
/// on screen. `sticky` (dragging) adds hysteresis like the chat's left/right flip.
pub fn memory_slot(
    chat: Rect,
    panel: Size,
    win: Size,
    side: HSide,
    current: VSide,
    sticky: bool,
) -> (Point, VSide) {
    let (pw, ph) = panel;
    let x = match side {
        HSide::Right => chat.x + chat.w - pw,
        HSide::Left => chat.x,
    };
    let x = if win.0 - pw < 0.0 { 0.0 } else { x.clamp(0.0, win.0 - pw) };
    let above = chat.y - MEMORY_GAP - ph;
    let below = chat.y + chat.h + MEMORY_GAP;
    let (up, down) = (overflow(above, ph, win.1), overflow(below, ph, win.1));
    let vside = if sticky {
        match current {
            // `above` is how much room there is above the panel (negative: sticks out)
            VSide::Above if -above > FLIP_MARGIN && down < up => VSide::Below,
            VSide::Below if above > FLIP_MARGIN => VSide::Above,
            current => current,
        }
    } else if up == 0.0 || up <= down {
        VSide::Above
    } else {
        VSide::Below
    };
    let y = match vside {
        VSide::Above => above,
        VSide::Below => below,
    };
    ((x, y), vside)
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

    /// Shaped like the real shell: 120px mascot column + 290px chat + padding/margins.
    fn shell() -> Shell {
        Shell {
            size: (490.0, 244.0),
            mascot_x: [34.0, 336.0],
            mascot_y: 78.0,
        }
    }

    fn placement_at(x: f64, side: HSide) -> Placement {
        Placement {
            anchor: (x, 700.0),
            side,
            initialized: true,
        }
    }

    #[test]
    fn homes_in_the_bottom_right_with_the_chat_on_screen() {
        let mut p = Placement::default();
        p.home(WIN, &shell(), BODY);
        assert_eq!(
            p.shell_origin(&shell()),
            (1920.0 - 490.0 - 24.0, 1080.0 - 244.0 - 56.0)
        );
        assert_eq!(p.side, HSide::Right);
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
    fn chat_prefers_the_right() {
        let mut p = placement_at(800.0, HSide::Right);
        assert!(!p.choose_side(WIN, &shell(), false));
        assert_eq!(p.side, HSide::Right);
    }

    #[test]
    fn chat_opens_left_near_the_right_edge() {
        let mut p = placement_at(1780.0, HSide::Right);
        assert!(p.choose_side(WIN, &shell(), false));
        assert_eq!(p.side, HSide::Left);
        // The mascot stays put; the shell extends to its left
        assert_eq!(p.shell_origin(&shell()).0, 1780.0 - 336.0);
    }

    #[test]
    fn hidden_chat_goes_back_to_the_right_when_there_is_room() {
        let mut p = placement_at(900.0, HSide::Left);
        assert!(p.choose_side(WIN, &shell(), false));
        assert_eq!(p.side, HSide::Right);
    }

    #[test]
    fn dragging_with_the_chat_open_switches_with_hysteresis() {
        // x where the chat-on-the-right shell exactly touches the right edge
        let edge = 1920.0 - 490.0 + 34.0;
        let side_after = |x: f64, from: HSide| {
            let mut p = placement_at(x, from);
            p.choose_side(WIN, &shell(), true);
            p.side
        };

        // Moving right: stays put until the chat would stick out more than 16px
        assert_eq!(side_after(edge + 10.0, HSide::Right), HSide::Right);
        assert_eq!(side_after(edge + 20.0, HSide::Right), HSide::Left);

        // Moving back left: returns as soon as the right side has 16px to spare,
        // not only once the left side runs out of room
        assert_eq!(side_after(edge - 10.0, HSide::Left), HSide::Left);
        assert_eq!(side_after(edge - 20.0, HSide::Left), HSide::Right);
        assert_eq!(side_after(900.0, HSide::Left), HSide::Right);
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
        let p = card_slot(body, body, card, WIN, HSide::Right, true, None);
        assert_eq!(p, (800.0 - 14.0 - 104.0, 600.0 + 96.0 - 104.0));
        let p = card_slot(body, body, card, WIN, HSide::Left, true, None);
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
        let p = card_slot(body, body, (104.0, 104.0), WIN, HSide::Right, true, None);
        assert_eq!(p, (-4.0, 600.0 - 14.0 - 104.0));
    }

    #[test]
    fn card_stays_clear_of_the_glass_panel() {
        let body = Rect {
            x: 800.0,
            y: 600.0,
            w: 96.0,
            h: 96.0,
        };
        // Glass panel around the mascot (chat on the right): starts 28px left of the body
        let glass = Rect {
            x: 772.0,
            y: 480.0,
            w: 454.0,
            h: 248.0,
        };
        let p = card_slot(body, glass, (104.0, 104.0), WIN, HSide::Right, true, None);
        assert_eq!(p, (772.0 - 14.0 - 104.0, 600.0 + 96.0 - 104.0));
        assert!(p.0 + 104.0 <= glass.x);
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
        let p = card_slot(body, body, (104.0, 104.0), WIN, HSide::Right, false, None);
        assert_eq!(p.0, 96.0 + 14.0);
    }

    #[test]
    fn card_slot_avoids_the_memory_panel() {
        let body = Rect {
            x: 800.0,
            y: 600.0,
            w: 96.0,
            h: 96.0,
        };
        // The memory panel covers the spot left of Appy: the card goes above him
        let panel = Rect {
            x: 600.0,
            y: 500.0,
            w: 190.0,
            h: 200.0,
        };
        let p = card_slot(body, body, (104.0, 104.0), WIN, HSide::Right, true, Some(panel));
        assert_eq!(p, (796.0, 600.0 - 14.0 - 104.0));
    }

    fn chat_at(x: f64, y: f64) -> Rect {
        Rect {
            x,
            y,
            w: 290.0,
            h: 300.0,
        }
    }

    const PANEL: Size = (360.0, 260.0);

    #[test]
    fn memory_slot_prefers_above() {
        let (p, v) = memory_slot(chat_at(1000.0, 500.0), PANEL, WIN, HSide::Right, VSide::Above, false);
        assert_eq!(v, VSide::Above);
        assert_eq!(p, (1290.0 - 360.0, 500.0 - 10.0 - 260.0));
    }

    #[test]
    fn memory_slot_flips_below_near_the_top() {
        let (p, v) = memory_slot(chat_at(1000.0, 100.0), PANEL, WIN, HSide::Right, VSide::Above, false);
        assert_eq!(v, VSide::Below);
        assert_eq!(p.1, 100.0 + 300.0 + 10.0);
        // and back above once there's room, when not dragging
        let (_, v) = memory_slot(chat_at(1000.0, 500.0), PANEL, WIN, HSide::Right, VSide::Below, false);
        assert_eq!(v, VSide::Above);
    }

    #[test]
    fn memory_slot_hysteresis() {
        let side = |y: f64, current: VSide| {
            memory_slot(chat_at(1000.0, y), PANEL, WIN, HSide::Right, current, true).1
        };
        // above sticks out by 10 (less than FLIP_MARGIN): stays above while dragging
        assert_eq!(side(260.0, VSide::Above), VSide::Above);
        assert_eq!(side(240.0, VSide::Above), VSide::Below); // by 30
        // below, above would have 10px spare: stays below; 20px spare: back above
        assert_eq!(side(280.0, VSide::Below), VSide::Below);
        assert_eq!(side(290.0, VSide::Below), VSide::Above);
        // not dragging: no hysteresis
        assert_eq!(
            memory_slot(chat_at(1000.0, 260.0), PANEL, WIN, HSide::Right, VSide::Above, false).1,
            VSide::Below
        );
    }

    #[test]
    fn memory_slot_aligns_outer_edge_and_clamps() {
        let (p, _) = memory_slot(chat_at(1000.0, 500.0), PANEL, WIN, HSide::Left, VSide::Above, false);
        assert_eq!(p.0, 1000.0);
        let (p, _) = memory_slot(chat_at(0.0, 500.0), PANEL, WIN, HSide::Right, VSide::Above, false);
        assert_eq!(p.0, 0.0);
        let (p, _) = memory_slot(chat_at(1700.0, 500.0), PANEL, WIN, HSide::Left, VSide::Above, false);
        assert_eq!(p.0, 1920.0 - 360.0);
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
