//! Messages exchanged with the Python backend (WebSocket, port 8001) and the KDE panel
//! pill (HTTP, port 8002), plus the internal events the network thread hands to GTK.
//!
//! Keep these in sync with `ui_bridge.py` / `streaming.py` and `PROTOCOL.md`.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AppyState {
    Idle,
    Listening,
    Booting,
    Thinking,
    Compacting,
    Speaking,
}

impl AppyState {
    /// Busy states show the animated dots face and a live status pill in the chat.
    pub fn is_busy(self) -> bool {
        matches!(
            self,
            AppyState::Booting | AppyState::Thinking | AppyState::Compacting
        )
    }

    pub fn live_status_text(self) -> Option<&'static str> {
        match self {
            AppyState::Booting => Some("loading"),
            AppyState::Thinking => Some("thinking"),
            AppyState::Compacting => Some("compacting"),
            _ => None,
        }
    }

    pub fn done_status_text(self) -> &'static str {
        match self {
            AppyState::Booting => "loaded",
            AppyState::Thinking => "thought",
            AppyState::Compacting => "compacted",
            _ => "done",
        }
    }
}

/// Reasoning effort sent to the LLM backend (`reasoning_effort`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub enum Effort {
    #[default]
    #[serde(rename = "none")]
    Off,
    #[serde(rename = "low")]
    Low,
    #[serde(rename = "medium")]
    Medium,
    #[serde(rename = "high")]
    High,
}

impl Effort {
    pub const ALL: [Effort; 4] = [Effort::Off, Effort::Low, Effort::Medium, Effort::High];

    pub fn short(self) -> &'static str {
        match self {
            Effort::Off => "off",
            Effort::Low => "low",
            Effort::Medium => "med",
            Effort::High => "high",
        }
    }

    pub fn tooltip(self) -> &'static str {
        match self {
            Effort::Off => "No reasoning (fastest)",
            Effort::Low => "Low reasoning effort",
            Effort::Medium => "Medium reasoning effort",
            Effort::High => "High reasoning effort (slowest)",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Expression {
    #[default]
    Neutral,
    Happy,
    Excited,
    Curious,
    Surprised,
    Focused,
    Skeptical,
}

/// How Appy talks (tools.VALID_MODES on the Python side). Focus mode shows as the
/// focused face, which the mascot also rests on between replies.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    #[default]
    Banter,
    Focus,
}

impl Mode {
    /// The face the mascot relaxes to when nothing else is going on.
    pub fn resting_expression(self) -> Expression {
        match self {
            Mode::Banter => Expression::Neutral,
            Mode::Focus => Expression::Focused,
        }
    }
}

/// Backend -> UI.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum BackendEvent {
    /// The mic heard the start of speech.
    Wake,
    /// What the user said. `is_final: false` is reserved for live partial transcripts.
    Transcript {
        text: String,
        is_final: bool,
    },
    Status {
        state: AppyState,
        label: String,
    },
    Response {
        text: String,
        #[serde(default)]
        expression: Expression,
    },
    Effort {
        value: Effort,
    },
    /// Appy's mode; sent on connect and whenever it changes.
    Mode {
        value: Mode,
    },
    /// How much of the context the conversation uses; at `limit` it's compacted
    /// automatically. Drives the compact ring on the input pill.
    Context {
        tokens: u64,
        limit: u64,
    },
    /// A card for Appy to pop out next to himself.
    Widget(Widget),
    /// What the consolidator learned and is waiting for review (all of it; empty = none).
    MemoryProposals {
        items: Vec<Proposal>,
    },
    /// Something was just written to long-term memory (or undone): a chat chip.
    MemoryCommitted {
        kind: MemoryKind,
        summary: String,
        /// The sentences the change added and removed (older backends send neither).
        #[serde(default)]
        facts: Vec<String>,
        #[serde(default)]
        dropped: Vec<String>,
        /// The change's commit, so a pill's Undo can name exactly what it holds.
        #[serde(default)]
        commit: String,
    },
    /// A memory action failed (e.g. an undo): shown in the chat.
    MemoryError {
        text: String,
    },
    /// The memory service's settings (sent on connect and after every change).
    MemorySettings {
        auto_approve: bool,
    },
}

/// One note's pending update: the whole note before and after.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Proposal {
    pub slug: String,
    pub title: String,
    pub summary: String,
    pub before: String,
    pub after: String,
    /// The note changed since this was written; it's being redone against the new one.
    #[serde(default)]
    pub stale: bool,
    /// Why the last decision on it failed (e.g. an edit that broke the note's format).
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MemoryKind {
    Remember,
    Forget,
    Approved,
    Edited,
    Undo,
    /// Committed by the Mac with auto-approve on.
    Auto,
    /// A kind newer than this UI: still shown, without an Undo button.
    #[serde(other)]
    Other,
}

/// What Tudor decided about a proposal.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Accept,
    Deny,
    Edit,
}

/// Cards the backend can ask the UI to show, tagged by `widget`.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "widget", rename_all = "snake_case")]
pub enum Widget {
    Weather(WeatherReport),
    Music(MusicReport),
}

/// What Riff is playing, for the now-playing card (music.rs).
#[derive(Debug, Clone, Deserialize)]
pub struct MusicReport {
    pub title: String,
    pub artist: String,
    pub state: PlaybackState,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaybackState {
    Playing,
    Paused,
    /// Anything newer than this UI: drawn like paused rather than dropping the card.
    #[serde(other)]
    Unknown,
}

/// One day's weather for the card: right now, or a forecast day (`when` set, with the
/// high in `temp_c` and the low in `low_c`).
#[derive(Debug, Clone, Deserialize)]
pub struct WeatherReport {
    pub city: String,
    pub temp_c: f64,
    #[serde(default)]
    pub low_c: Option<f64>,
    /// Short weekday ("Sat") for a forecast; absent or null for right now.
    #[serde(default)]
    pub when: Option<String>,
    pub condition: String,
    pub icon: WeatherIcon,
}

/// Monochrome weather icons the UI knows how to draw (weather.rs).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WeatherIcon {
    Clear,
    ClearNight,
    PartlyCloudy,
    PartlyCloudyNight,
    Cloudy,
    Fog,
    Drizzle,
    Rain,
    Snow,
    Thunder,
    /// Anything newer than this UI: drawn as a cloud rather than dropping the card.
    #[serde(other)]
    Unknown,
}

/// UI -> backend.
#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientEvent {
    UserText { text: String },
    SetEffort { effort: Effort },
    /// Fold the conversation so far into a summary now (the input pill's circle button).
    Compact,
    /// Accept, deny or edit (with the full edited note in `text`) one proposal.
    MemoryDecision {
        slug: String,
        decision: Decision,
        #[serde(skip_serializing_if = "Option::is_none")]
        text: Option<String>,
        /// Edit only: the note as the card showed it when editing started.
        #[serde(skip_serializing_if = "Option::is_none")]
        before: Option<String>,
    },
    /// Undo the newest memory change, or exactly `commits` (newest first; the Mac stops
    /// if something newer changed memory in between).
    MemoryUndo {
        #[serde(skip_serializing_if = "Vec::is_empty")]
        commits: Vec<String>,
    },
    /// Turn auto-approve on or off (the panel pill's menu).
    MemoryAutoApprove { on: bool },
}

/// Snapshot served to the panel pill at `GET /state`.
#[derive(Debug, Clone, Serialize)]
pub struct PillSyncState {
    pub state: AppyState,
    pub expression: Expression,
    pub msg_count: usize,
    pub minimized: bool,
    /// None until the brain says (or with memory off): the pill hides its toggle.
    pub auto_approve: Option<bool>,
}

impl Default for PillSyncState {
    fn default() -> Self {
        Self {
            state: AppyState::Idle,
            expression: Expression::Neutral,
            msg_count: 0,
            minimized: true,
            auto_approve: None,
        }
    }
}

/// Events delivered from the network thread to the GTK main loop.
#[derive(Debug)]
pub enum UiEvent {
    ToggleOverlay,
    Quit,
    /// The panel pill's "Auto-approve memories" item: flip the current setting.
    ToggleAutoApprove,
    Backend(BackendEvent),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resting_face_shows_the_mode() {
        assert_eq!(Mode::default(), Mode::Banter);
        assert_eq!(Mode::Banter.resting_expression(), Expression::Neutral);
        assert_eq!(Mode::Focus.resting_expression(), Expression::Focused);
    }

    #[test]
    fn parses_backend_events() {
        let ev: BackendEvent =
            serde_json::from_str(r#"{"type":"status","state":"thinking","label":"thinking"}"#)
                .unwrap();
        assert!(matches!(
            ev,
            BackendEvent::Status {
                state: AppyState::Thinking,
                ..
            }
        ));

        let ev: BackendEvent = serde_json::from_str(r#"{"type":"response","text":"hi"}"#).unwrap();
        assert!(matches!(
            ev,
            BackendEvent::Response {
                expression: Expression::Neutral,
                ..
            }
        ));

        let ev: BackendEvent = serde_json::from_str(r#"{"type":"effort","value":"none"}"#).unwrap();
        assert!(matches!(ev, BackendEvent::Effort { value: Effort::Off }));

        let ev: BackendEvent = serde_json::from_str(r#"{"type":"mode","value":"focus"}"#).unwrap();
        assert!(matches!(ev, BackendEvent::Mode { value: Mode::Focus }));

        let ev: BackendEvent =
            serde_json::from_str(r#"{"type":"context","tokens":4200,"limit":9500}"#).unwrap();
        assert!(matches!(
            ev,
            BackendEvent::Context {
                tokens: 4200,
                limit: 9500
            }
        ));
    }

    #[test]
    fn parses_weather_widget() {
        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"widget","widget":"weather","city":"Sibiu","temp_c":21.4,"low_c":null,"when":null,"condition":"clear sky","icon":"partly_cloudy_night"}"#,
        )
        .unwrap();
        let BackendEvent::Widget(Widget::Weather(report)) = ev else {
            panic!("wrong variant: {ev:?}");
        };
        assert_eq!(
            (
                report.city.as_str(),
                report.temp_c,
                report.icon,
                report.when
            ),
            ("Sibiu", 21.4, WeatherIcon::PartlyCloudyNight, None)
        );

        // A forecast day; optional fields may also be missing entirely
        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"widget","widget":"weather","city":"Sibiu","temp_c":5,"low_c":-0.4,"when":"Sat","condition":"light rain","icon":"rain"}"#,
        )
        .unwrap();
        let BackendEvent::Widget(Widget::Weather(report)) = ev else {
            panic!("wrong variant: {ev:?}");
        };
        assert_eq!(
            (report.low_c, report.when.as_deref()),
            (Some(-0.4), Some("Sat"))
        );

        // An icon this UI doesn't know yet still shows the card
        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"widget","widget":"weather","city":"X","temp_c":1,"condition":"c","icon":"tornado"}"#,
        )
        .unwrap();
        assert!(matches!(
            ev,
            BackendEvent::Widget(Widget::Weather(WeatherReport {
                icon: WeatherIcon::Unknown,
                ..
            }))
        ));
        // Unknown widget kinds are rejected (and logged) rather than misread
        assert!(
            serde_json::from_str::<BackendEvent>(r#"{"type":"widget","widget":"stocks"}"#).is_err()
        );
    }

    #[test]
    fn parses_music_widget() {
        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"widget","widget":"music","title":"Karma Police","artist":"Radiohead","state":"playing"}"#,
        )
        .unwrap();
        let BackendEvent::Widget(Widget::Music(track)) = ev else {
            panic!("wrong variant: {ev:?}");
        };
        assert_eq!(
            (track.title.as_str(), track.artist.as_str(), track.state),
            ("Karma Police", "Radiohead", PlaybackState::Playing)
        );
    }

    #[test]
    fn unknown_playback_state_still_parses() {
        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"widget","widget":"music","title":"A","artist":"","state":"buffering"}"#,
        )
        .unwrap();
        assert!(matches!(
            ev,
            BackendEvent::Widget(Widget::Music(MusicReport {
                state: PlaybackState::Unknown,
                ..
            }))
        ));
    }

    #[test]
    fn unknown_events_fail_to_parse() {
        assert!(serde_json::from_str::<BackendEvent>(r#"{"type":"open_tool"}"#).is_err());
    }

    #[test]
    fn serializes_client_events() {
        let json = serde_json::to_string(&ClientEvent::SetEffort {
            effort: Effort::Medium,
        })
        .unwrap();
        assert_eq!(json, r#"{"type":"set_effort","effort":"medium"}"#);
        let json = serde_json::to_string(&ClientEvent::UserText { text: "yo".into() }).unwrap();
        assert_eq!(json, r#"{"type":"user_text","text":"yo"}"#);
        let json = serde_json::to_string(&ClientEvent::Compact).unwrap();
        assert_eq!(json, r#"{"type":"compact"}"#);
    }

    #[test]
    fn parses_memory_events() {
        let ev: BackendEvent = serde_json::from_str(
            r##"{"type":"memory_proposals","items":[{"slug":"lab","title":"Lab","summary":"added PSU","before":"# Lab\n","after":"# Lab\n- PSU\n","stale":true,"error":"nope"},{"slug":"core","title":"Core","summary":"s","before":"","after":"- a\n"}]}"##,
        )
        .unwrap();
        let BackendEvent::MemoryProposals { items } = ev else {
            panic!("wrong variant: {ev:?}");
        };
        assert_eq!(items.len(), 2);
        assert_eq!((items[0].slug.as_str(), items[0].stale), ("lab", true));
        assert_eq!(items[0].error.as_deref(), Some("nope"));
        assert_eq!((items[1].stale, items[1].error.is_none()), (false, true));

        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"memory_committed","kind":"approved","summary":"added the HUD"}"#,
        )
        .unwrap();
        assert!(matches!(
            ev,
            BackendEvent::MemoryCommitted {
                kind: MemoryKind::Approved,
                ..
            }
        ));
        let ev: BackendEvent =
            serde_json::from_str(r#"{"type":"memory_committed","kind":"teleported","summary":"x"}"#)
                .unwrap();
        assert!(matches!(
            ev,
            BackendEvent::MemoryCommitted {
                kind: MemoryKind::Other,
                ..
            }
        ));
    }

    #[test]
    fn serializes_memory_client_events() {
        let json = serde_json::to_string(&ClientEvent::MemoryDecision {
            slug: "lab".into(),
            decision: Decision::Accept,
            text: None,
            before: None,
        })
        .unwrap();
        assert_eq!(
            json,
            r#"{"type":"memory_decision","slug":"lab","decision":"accept"}"#
        );
        let json = serde_json::to_string(&ClientEvent::MemoryDecision {
            slug: "lab".into(),
            decision: Decision::Edit,
            text: Some("# Lab\n".into()),
            before: Some("# Old\n".into()),
        })
        .unwrap();
        assert_eq!(
            json,
            r##"{"type":"memory_decision","slug":"lab","decision":"edit","text":"# Lab\n","before":"# Old\n"}"##
        );
        let json = serde_json::to_string(&ClientEvent::MemoryUndo { commits: vec![] }).unwrap();
        assert_eq!(json, r#"{"type":"memory_undo"}"#);
        let json = serde_json::to_string(&ClientEvent::MemoryUndo {
            commits: vec!["c2".into(), "c1".into()],
        })
        .unwrap();
        assert_eq!(json, r#"{"type":"memory_undo","commits":["c2","c1"]}"#);
        let json = serde_json::to_string(&ClientEvent::MemoryAutoApprove { on: true }).unwrap();
        assert_eq!(json, r#"{"type":"memory_auto_approve","on":true}"#);
    }

    #[test]
    fn memory_committed_facts_are_optional() {
        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"memory_committed","kind":"approved","summary":"s","facts":["A."],"dropped":["B."]}"#,
        )
        .unwrap();
        let BackendEvent::MemoryCommitted { facts, dropped, .. } = ev else {
            panic!("wrong event")
        };
        assert_eq!((facts, dropped), (vec!["A.".to_string()], vec!["B.".to_string()]));
        let ev: BackendEvent =
            serde_json::from_str(r#"{"type":"memory_committed","kind":"undo","summary":"s"}"#)
                .unwrap();
        let BackendEvent::MemoryCommitted { facts, dropped, .. } = ev else {
            panic!("wrong event")
        };
        assert!(facts.is_empty() && dropped.is_empty());
        let ev: BackendEvent = serde_json::from_str(
            r#"{"type":"memory_committed","kind":"auto","summary":"s","commit":"abc"}"#,
        )
        .unwrap();
        assert!(matches!(ev, BackendEvent::MemoryCommitted { ref commit, .. } if commit == "abc"));
        let ev: BackendEvent =
            serde_json::from_str(r#"{"type":"memory_error","text":"Couldn't undo: x"}"#).unwrap();
        assert!(matches!(ev, BackendEvent::MemoryError { ref text } if text == "Couldn't undo: x"));
    }

    #[test]
    fn parses_memory_settings_and_auto_chips() {
        let ev: BackendEvent =
            serde_json::from_str(r#"{"type":"memory_settings","auto_approve":true}"#).unwrap();
        assert!(matches!(ev, BackendEvent::MemorySettings { auto_approve: true }));
        let ev: BackendEvent =
            serde_json::from_str(r#"{"type":"memory_committed","kind":"auto","summary":"x"}"#)
                .unwrap();
        assert!(matches!(
            ev,
            BackendEvent::MemoryCommitted {
                kind: MemoryKind::Auto,
                ..
            }
        ));
    }

    #[test]
    fn serializes_pill_state() {
        let json = serde_json::to_string(&PillSyncState::default()).unwrap();
        assert_eq!(
            json,
            r#"{"state":"idle","expression":"neutral","msg_count":0,"minimized":true,"auto_approve":null}"#
        );
    }
}
