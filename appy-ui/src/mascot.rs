use iced::widget::canvas::{self, Frame, Geometry, Path, Stroke};
use iced::{Color, Point, Rectangle, Renderer, Size, Theme};

use crate::AppyState;

pub struct MascotCanvas {
    pub state: AppyState,
    pub phase: f32,
}

impl<Message> canvas::Program<Message> for MascotCanvas {
    type State = ();

    fn draw(
        &self,
        _state: &Self::State,
        renderer: &Renderer,
        _theme: &Theme,
        bounds: Rectangle,
        _cursor: iced::mouse::Cursor,
    ) -> Vec<Geometry> {
        let mut frame = Frame::new(renderer, bounds.size());
        let w = bounds.width;
        let h = bounds.height;

        // 1. Squircle body (semi-transparent putty gray)
        let body_rect = Rectangle {
            x: w * 0.10,
            y: h * 0.22,
            width: w * 0.80,
            height: h * 0.72,
        };
        let body = Path::rounded_rectangle(
            body_rect.position(),
            body_rect.size(),
            (w * 0.24).into(),
        );
        frame.fill(&body, Color::from_rgba8(218, 216, 208, 0.88));
        frame.stroke(
            &body,
            Stroke::default()
                .with_color(Color::from_rgba(1.0, 1.0, 1.0, 0.28))
                .with_width(1.5),
        );

        // 2. Blush-white leaf on top
        let leaf = Path::new(|b| {
            b.move_to(Point::new(w * 0.50, h * 0.23));
            b.quadratic_curve_to(Point::new(w * 0.52, h * 0.06), Point::new(w * 0.67, h * 0.04));
            b.quadratic_curve_to(Point::new(w * 0.66, h * 0.20), Point::new(w * 0.50, h * 0.23));
            b.close();
        });
        frame.fill(&leaf, Color::from_rgba8(251, 242, 242, 0.94));

        // 3. Face: 3-dot wave when busy, happy arcs when active, capsule eyes when idle
        let cy = h * 0.59;
        match self.state {
            AppyState::Booting | AppyState::Thinking | AppyState::Compacting => {
                let spacing = w * 0.14;
                let base_x = w * 0.50 - spacing;
                for i in 0..3 {
                    let offset = (self.phase - (i as f32 * 0.7)).sin();
                    let y = cy - offset * (h * 0.045);
                    let r = w * (0.038 + 0.008 * offset.max(0.0));
                    let dot = Path::circle(Point::new(base_x + i as f32 * spacing, y), r);
                    frame.fill(&dot, Color::from_rgb8(24, 24, 28));
                }
            }
            AppyState::Listening | AppyState::Speaking => {
                let eye_stroke = Stroke::default()
                    .with_color(Color::from_rgb8(20, 20, 24))
                    .with_width(w * 0.055)
                    .with_line_cap(canvas::LineCap::Round);
                for &ex in &[w * 0.36, w * 0.64] {
                    let arc = Path::new(|b| {
                        b.move_to(Point::new(ex - w * 0.07, cy + h * 0.03));
                        b.quadratic_curve_to(
                            Point::new(ex, cy - h * 0.07),
                            Point::new(ex + w * 0.07, cy + h * 0.03),
                        );
                    });
                    frame.stroke(&arc, eye_stroke);
                }
            }
            AppyState::Idle => {
                for &ex in &[w * 0.37, w * 0.63] {
                    let eye = Path::rounded_rectangle(
                        Point::new(ex - w * 0.035, cy - h * 0.08),
                        Size::new(w * 0.07, h * 0.16),
                        (w * 0.035).into(),
                    );
                    frame.fill(&eye, Color::from_rgb8(20, 20, 24));
                }
            }
        }

        vec![frame.into_geometry()]
    }
}