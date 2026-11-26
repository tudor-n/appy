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
}

/// UI -> backend.
#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientEvent {
    UserText { text: String },
    SetEffort { effort: Effort },
}

/// Snapshot served to the panel pill at `GET /state`.
#[derive(Debug, Clone, Serialize)]
pub struct PillSyncState {
    pub state: AppyState,
    pub expression: Expression,
    pub msg_count: usize,
    pub minimized: bool,
}

impl Default for PillSyncState {
    fn default() -> Self {
        Self {
            state: AppyState::Idle,
            expression: Expression::Neutral,
            msg_count: 0,
            minimized: true,
        }
    }
}

/// Events delivered from the network thread to the GTK main loop.
#[derive(Debug)]
pub enum UiEvent {
    ToggleOverlay,
    Quit,
    Backend(BackendEvent),
}

#[cfg(test)]
mod tests {
    use super::*;

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
    }

    #[test]
    fn serializes_pill_state() {
        let json = serde_json::to_string(&PillSyncState::default()).unwrap();
        assert_eq!(
            json,
            r#"{"state":"idle","expression":"neutral","msg_count":0,"minimized":true}"#
        );
    }
}
