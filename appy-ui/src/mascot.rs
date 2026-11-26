//! Cairo drawing for the Appy mascot.

use std::f64::consts::{PI, TAU};

use gtk4::cairo::{Context, LineCap, Matrix};

use crate::protocol::{AppyState, Expression};

/// The mascot is drawn in a SIZE x SIZE box, centred horizontally and sitting
/// BOTTOM_PAD above the bottom of its drawing area. The headroom above is for
/// the talking hops.
const SIZE: f64 = 96.0;
const BOTTOM_PAD: f64 = 12.0;

const INK: (f64, f64, f64) = (20.0 / 255.0, 20.0 / 255.0, 24.0 / 255.0);

/// Everything that affects how the mascot looks on a given frame.
#[derive(Debug, Clone, Copy)]
pub struct Pose {
    pub state: AppyState,
    pub expression: Expression,
    /// Phase of the busy "dots" wave, radians.
    pub phase: f64,
    /// Phase of the talking bounce, radians.
    pub talk_phase: f64,
    pub wobble_x: f64,
    pub wobble_y: f64,
    /// 1.0 = eyes open, 0.0 = fully closed.
    pub blink_scale: f64,
}

/// True when (x, y) in drawing-area coordinates lies on the mascot body rather than
/// on the empty headroom / side padding around it.
pub fn body_contains(canvas_w: f64, canvas_h: f64, x: f64, y: f64) -> bool {
    let left = (canvas_w - SIZE) * 0.5;
    let top = canvas_h - SIZE - BOTTOM_PAD;
    x >= left && x <= left + SIZE && y >= top && y <= top + SIZE
}

pub fn draw(cr: &Context, canvas_w: f64, canvas_h: f64, pose: &Pose) {
    let _ = cr.save();

    let w = SIZE;
    let h = SIZE;
    let pad_x = (canvas_w - w) * 0.5;
    let pad_y = canvas_h - h - BOTTOM_PAD;
    let cx = canvas_w * 0.5;
    let cy_pivot = pad_y + h * 0.62;

    // Talking bounce synced to Kokoro audio playback
    let (talk_dy, talk_sx, talk_sy, talk_rot) = if pose.state == AppyState::Speaking {
        let bounce = pose.talk_phase.sin().abs();
        let cadence = bounce * (0.78 + 0.22 * (pose.talk_phase * 0.43).cos().abs());
        let dy = -cadence * 14.0;
        let sy = 1.0 + cadence * 0.11 - (1.0 - bounce) * 0.05;
        let sx = 1.0 - cadence * 0.07 + (1.0 - bounce) * 0.04;
        let rot = (pose.talk_phase * 0.5).sin() * 0.06;
        (dy, sx, sy, rot)
    } else {
        (0.0, 1.0, 1.0, 0.0)
    };

    let expr_tilt = if pose.expression == Expression::Curious && !pose.state.is_busy() {
        0.075
    } else {
        0.0
    };

    // KDE Wobbly Windows shear + tilt + squash/stretch + talking bounce
    let shear_x = (pose.wobble_x * 0.015).clamp(-0.30, 0.30);
    let rot = (pose.wobble_x * 0.005).clamp(-0.16, 0.16) + talk_rot + expr_tilt;
    let stretch_y = (pose.wobble_y * 0.009).clamp(-0.18, 0.18);
    let scale_x = (1.0 - stretch_y * 0.55) * talk_sx;
    let scale_y = (1.0 + stretch_y) * talk_sy;

    cr.translate(cx, cy_pivot + talk_dy);
    cr.rotate(rot);
    cr.transform(Matrix::new(scale_x, 0.0, shear_x, scale_y, 0.0, 0.0));
    cr.translate(-cx + pad_x, -cy_pivot + pad_y);

    draw_body(cr, w, h);
    draw_face(cr, w, h, pose);

    let _ = cr.restore();
}

fn draw_body(cr: &Context, w: f64, h: f64) {
    // Squircle body
    rounded_rect(cr, w * 0.10, h * 0.22, w * 0.80, h * 0.72, w * 0.24);
    cr.set_source_rgba(218.0 / 255.0, 216.0 / 255.0, 208.0 / 255.0, 0.94);
    let _ = cr.fill();

    // Blush-white leaf
    cr.move_to(w * 0.50, h * 0.23);
    cr.curve_to(w * 0.52, h * 0.06, w * 0.58, h * 0.05, w * 0.67, h * 0.04);
    cr.curve_to(w * 0.66, h * 0.16, w * 0.60, h * 0.21, w * 0.50, h * 0.23);
    cr.close_path();
    cr.set_source_rgba(251.0 / 255.0, 242.0 / 255.0, 242.0 / 255.0, 0.96);
    let _ = cr.fill();
}

fn draw_face(cr: &Context, w: f64, h: f64, pose: &Pose) {
    let cy = h * 0.59;
    set_ink(cr);

    if pose.state.is_busy() {
        // Three-dot wave
        let spacing = w * 0.14;
        let base_x = w * 0.50 - spacing;
        for i in 0..3 {
            let offset = (pose.phase - (i as f64 * 0.7)).sin();
            let y = cy - offset * (h * 0.045);
            let r = w * (0.042 + 0.008 * offset.max(0.0));
            cr.arc(base_x + i as f64 * spacing, y, r, 0.0, TAU);
            let _ = cr.fill();
        }
        return;
    }

    if pose.state == AppyState::Listening {
        arc_eyes(
            cr,
            w,
            h,
            cy,
            ArcEyes {
                half_w: 0.07,
                peak: 0.06,
                base: 0.03,
                line: 0.06,
            },
        );
        return;
    }

    let blink = pose.blink_scale;
    match pose.expression {
        Expression::Neutral => {
            let eye_h = (h * 0.24 * blink).max(h * 0.025);
            let eye_w = w * 0.072;
            for ex in [w * 0.37, w * 0.63] {
                rounded_rect(
                    cr,
                    ex - eye_w * 0.5,
                    cy - eye_h * 0.5,
                    eye_w,
                    eye_h,
                    eye_w * 0.5,
                );
                let _ = cr.fill();
            }
        }
        Expression::Happy => {
            arc_eyes(
                cr,
                w,
                h,
                cy,
                ArcEyes {
                    half_w: 0.07,
                    peak: 0.065,
                    base: 0.03,
                    line: 0.06,
                },
            );
        }
        Expression::Excited => {
            // Blush cheeks under big arcs
            cr.set_source_rgba(240.0 / 255.0, 128.0 / 255.0, 138.0 / 255.0, 0.42);
            for bx in [w * 0.27, w * 0.73] {
                cr.arc(bx, cy + h * 0.09, w * 0.055, 0.0, TAU);
                let _ = cr.fill();
            }
            set_ink(cr);
            arc_eyes(
                cr,
                w,
                h,
                cy,
                ArcEyes {
                    half_w: 0.075,
                    peak: 0.095,
                    base: 0.035,
                    line: 0.062,
                },
            );
        }
        Expression::Curious => {
            // One eye a little bigger and higher
            let left_h = (h * 0.21 * blink).max(h * 0.025);
            let right_h = (h * 0.27 * blink).max(h * 0.025);
            rounded_rect(
                cr,
                w * 0.37 - w * 0.034,
                cy - left_h * 0.5 + h * 0.015,
                w * 0.068,
                left_h,
                w * 0.034,
            );
            let _ = cr.fill();
            rounded_rect(
                cr,
                w * 0.63 - w * 0.042,
                cy - right_h * 0.5 - h * 0.02,
                w * 0.084,
                right_h,
                w * 0.042,
            );
            let _ = cr.fill();
        }
        Expression::Surprised => {
            let r = w * 0.066;
            for ex in [w * 0.37, w * 0.63] {
                cr.arc(ex, cy - h * 0.01, r, 0.0, TAU);
                let _ = cr.fill();
            }
        }
        Expression::Focused => {
            let eye_h = (h * 0.14 * blink).max(h * 0.025);
            let eye_w = w * 0.092;
            for ex in [w * 0.375, w * 0.625] {
                rounded_rect(
                    cr,
                    ex - eye_w * 0.5,
                    cy - eye_h * 0.5 + h * 0.01,
                    eye_w,
                    eye_h,
                    w * 0.035,
                );
                let _ = cr.fill();
            }
        }
        Expression::Skeptical => {
            // Flat left eye, raised open right eye
            rounded_rect(
                cr,
                w * 0.37 - w * 0.055,
                cy - h * 0.025,
                w * 0.11,
                h * 0.05,
                h * 0.025,
            );
            let _ = cr.fill();
            let eye_h = (h * 0.24 * blink).max(h * 0.025);
            rounded_rect(
                cr,
                w * 0.63 - w * 0.036,
                cy - eye_h * 0.5 - h * 0.02,
                w * 0.072,
                eye_h,
                w * 0.036,
            );
            let _ = cr.fill();
        }
    }
}

/// Upward "^ ^" arcs, all values as fractions of the mascot size.
struct ArcEyes {
    half_w: f64,
    peak: f64,
    base: f64,
    line: f64,
}

fn arc_eyes(cr: &Context, w: f64, h: f64, cy: f64, eyes: ArcEyes) {
    cr.set_line_width((w * eyes.line).max(1.6));
    cr.set_line_cap(LineCap::Round);
    for ex in [w * 0.36, w * 0.64] {
        cr.move_to(ex - w * eyes.half_w, cy + h * eyes.base);
        cr.curve_to(
            ex - w * 0.02,
            cy - h * eyes.peak,
            ex + w * 0.02,
            cy - h * eyes.peak,
            ex + w * eyes.half_w,
            cy + h * eyes.base,
        );
        let _ = cr.stroke();
    }
}

fn set_ink(cr: &Context) {
    cr.set_source_rgb(INK.0, INK.1, INK.2);
}

fn rounded_rect(cr: &Context, x: f64, y: f64, w: f64, h: f64, r: f64) {
    let r = r.min(w * 0.5).min(h * 0.5);
    cr.new_sub_path();
    cr.arc(x + w - r, y + r, r, -PI / 2.0, 0.0);
    cr.arc(x + w - r, y + h - r, r, 0.0, PI / 2.0);
    cr.arc(x + r, y + h - r, r, PI / 2.0, PI);
    cr.arc(x + r, y + r, r, PI, 3.0 * PI / 2.0);
    cr.close_path();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn body_hit_test_ignores_headroom() {
        // 120x136 drawing area: body spans x 12..108, y 28..124
        assert!(body_contains(120.0, 136.0, 60.0, 80.0));
        assert!(!body_contains(120.0, 136.0, 60.0, 10.0));
        assert!(!body_contains(120.0, 136.0, 5.0, 80.0));
        assert!(!body_contains(120.0, 136.0, 60.0, 130.0));
    }
}
