#[derive(Debug, Clone, serde::Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum BackendEvent {
    Wake,                                             // Pop Appy out of the tray
    Transcript { text: String, is_final: bool },      // Live Whisper stream -> top pill
    Status { state: AppyState, label: String },       // "booting...", "thinking...", "compacting..."
    Response { text: String },                        // Gemma reply -> bottom pill
    OpenTool { tool: String, payload: serde_json::Value }, // e.g. "tasks", "history"
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AppyState {
    Idle,
    Listening,
    Booting,
    Thinking,
    Compacting,
    Speaking,
}