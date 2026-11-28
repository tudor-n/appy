# DJ mode: Appy plays music through Riff

## Why
Tudor wants to ask Appy for music by voice: play a song, an artist or one of his playlists,
then pause, resume, skip and so on. Music already runs on this machine through Riff
(`dev.diegovsky.Riff`, a GTK Spotify client, flatpak, Premium account), so Appy should drive
Riff rather than bring his own player.

## Goals / non-goals / anti-goals
- Goal: search and play by name: a track, artist, album or playlist ("play Karma Police",
  "put on some Radiohead", "play my gym playlist", "play my liked songs").
- Goal: transport: pause, resume, next, previous, volume up/down/set, shuffle on/off,
  "what's playing?".
- Goal: add a track to the queue ("queue Paranoid Android next").
- Goal: if Riff isn't running, Appy starts it and then plays.
- Goal: Appy's own voice is audible over the music: Riff is turned down while Appy speaks
  and turned back up after.
- Goal: Appy stays hands-free while music plays (Tudor's pick over tap-to-talk), through a
  temporary, easy-to-delete hearing workaround (below) until the ReSpeaker arrives.
- Non-goal: Spotify-generated playlists (Discover Weekly, Daily Mixes, Release Radar,
  "This Is …"). New Web API apps can't read them; Tudor doesn't use them. If one is asked
  for, Appy says Spotify won't let him play it.
- Non-goal: a wake word.
- Non-goal: a "DJ persona" where Appy picks songs on his own or talks between tracks.
  "DJ mode" here means the music tools are always available, not a mode switch like focus.
- Non-goal: other players (Spotify official, browsers). Riff only.
- Anti-goal: any Spotify secret or token in the repo, the logs, or the LLM's context.
- Anti-goal: pause/skip feeling slow. Transport must not wait on Spotify's web API.

## Approaches
- **A. MPRIS for transport + Spotify Web API (user login, PKCE) for search and playback
  (picked by Tudor).** Pause/resume/skip/volume/now-playing go to Riff over the local
  session bus (`org.mpris.MediaPlayer2.dev.diegovsky.Riff`): instant, offline. Search, play
  and queue go through the Web API as Tudor, starting playback on Riff's Spotify Connect
  device. Also unlocks his own playlists, liked songs and the queue. Costs a one-time
  browser login and a stored refresh token.
- B. App-only search (client credentials) + Riff's MPRIS `OpenUri`. No personal login, but
  depends on `OpenUri` working in Riff (unverified; the method exists in the binary) and
  can't reach his playlists, liked songs or queue.
- Rejected: replacing Riff with librespot/spotify-player; Tudor uses Riff.

## Design

### Spotify setup (one time)
- Tudor registers a free app at developer.spotify.com with redirect URI
  `http://127.0.0.1:8898/callback` (Spotify only allows plain http for loopback IPs).
- `.env` gains `SPOTIFY_CLIENT_ID` (PKCE needs no client secret). `.env.example` documents it.
- `python spotify_login.py` (also `bin/appy spotify-login`) runs the PKCE flow: opens the
  browser, catches the callback on 127.0.0.1:8898, saves the refresh token to
  `~/.config/appy/spotify.json` with mode 0600. Scopes: `user-read-playback-state`,
  `user-modify-playback-state`, `user-read-currently-playing`, `playlist-read-private`,
  `user-library-read`.
- Without a client ID or token, the music tools still load; anything needing the web API
  returns an error telling Appy to say "run spotify-login first". Transport still works.

### Modules
- `riff.py`: MPRIS client for Riff using `jeepney` (pure Python, new dependency; the venv
  has no D-Bus library and `playerctl` isn't installed). Play/Pause/PlayPause/Next/
  Previous, Volume get/set, Shuffle, Metadata, PlaybackStatus. `ensure_running()`: if the
  bus name is absent, `flatpak run dev.diegovsky.Riff` (detached) and wait up to ~10 s for
  it to appear.
- `spotify.py`: Web API client on the existing `httpx` stack. Token refresh (access tokens
  last an hour; refresh on 401 or expiry, persist a rotated refresh token). Search,
  devices, play (`PUT /me/player/play` with `device_id`), queue, the user's playlists,
  liked songs.
- `tools.py`: two new schemas (each schema costs context on every request, so two, not
  eight):
  - `play_music(query, kind, queue=false)`: `kind` is `track | artist | album | playlist |
    liked`. `playlist` searches Tudor's own playlists by name first, then public ones.
    `liked` plays his saved tracks. `queue=true` adds a track to the queue instead of
    playing. Returns what actually started (title, artist, or playlist name), so Appy
    names the real thing rather than what he guessed.
  - `music_control(action, value?)`: `action` is `pause | resume | next | previous |
    volume_up | volume_down | set_volume | shuffle_on | shuffle_off | now_playing`.
    MPRIS only, never the web API.
- Playback target: find Riff in `GET /me/player/devices` (by its device name; check what
  name Riff registers). If it's missing, `ensure_running()` and poll devices for a few
  seconds. Pass its `device_id` to play/queue, which also moves playback to Riff.
- **First plan task is a live check** that Riff registers as a Connect device (Riff has
  Connect-controller code, so it may only control other devices). If it doesn't: play via
  MPRIS `OpenUri(spotify:…)` and queue via the Web API on the active device. If `OpenUri`
  doesn't work either, stop and go back to Tudor before building further.
- Liked songs: try `context_uri` `spotify:user:<id>:collection` (works, undocumented); if
  refused, play his newest 100 saved tracks with shuffle on, still called "your liked
  songs".
- Forced calls: Gemma sometimes answers without calling the tool (that's why
  `tools.forced_call` exists for weather). Add clear-phrase triggers for transport
  ("pause", "skip", "next song", "resume", "turn it up", "what's this song") and for
  "play …", so those turns always call the right tool. (Strict while the workaround is on;
  see below.)

### Ducking (permanent)
- While Appy's speaker is active (the `queued`/`playing` audio states `StatusTracker`
  already gets), if Riff is `Playing`, set its MPRIS volume to ~25% of the current level.
- Also duck the moment the ear hears speech start (`on_speech_start`), so Tudor's voice
  isn't fighting the song; un-duck if that speech is discarded as noise.
- Restore the saved level ~0.5 s after Appy's audio ends (or the turn is dropped). If
  Tudor changes volume through Appy while ducked, the new level is what gets restored.
- MPRIS only; skip quietly if Riff isn't running.

### Hearing workaround (temporary, until the ReSpeaker)
Today the mic and speakers are the laptop's own, so the song leaks into the mic: lyrics get
transcribed as Tudor, and vocals can hide the pause that ends his turn. Everything here is
throwaway and must be removable in minutes.
- **Echo cancellation:** `audio/appy-echo-cancel.conf`, a PipeWire
  `libpipewire-module-echo-cancel` (WebRTC AEC) config, plus `bin/appy-echo-cancel`
  that installs it into `~/.config/pipewire/pipewire.conf.d/`, restarts PipeWire and
  prints the new source name for `MIC_DEVICE`. Appy code doesn't change for this part.
- **One switch:** `APPY_MIC_HEARS_MUSIC=1` in `.env` turns on the code parts; unset means
  today's behaviour exactly. The code parts only act while Riff reports `Playing`.
  - Strict music triggers: forced calls for music fire only when the whole utterance is a
    short command (a handful of words, starting with the command or "Appy"), so a lyric
    like "don't stop, play it again" doesn't skip the song.
  - Utterance cap: force-close a turn after ~15 s of continuous "speech", so leaked vocals
    can't keep a turn open forever.
- **Findable:** every code spot carries a `HEARING-WORKAROUND(respeaker)` comment;
  `docs/hearing-workaround.md` lists each piece and the removal steps (delete the config
  and script, drop the flag and the tagged code, reset `MIC_DEVICE`); a new project
  `CLAUDE.md` points to that doc.
- **Kept after removal:** ducking (above) and everything else in this design.
- Hardware note for that doc: the ReSpeaker's echo cancellation only works on audio it
  plays itself, so the Pebble V3s must be fed from the ReSpeaker's 3.5 mm output (or the
  system output routed through it).

### Now-playing card
- New `widget` kind `music` with `title`, `artist` and `state` (`playing | paused`),
  sent when `play_music` starts something and on `music_control now_playing`. Drawn in
  the weather card's monochrome style (`appy-ui/src/music.rs`, a cairo note/pause glyph,
  same fly-out and fly-back), with no album art so it stays in the mascot's palette.
  Added to PROTOCOL.md and `protocol.rs`; `tests/test_protocol.py` keeps the two in sync.

### Prompt
- A few lines in `prompt.txt` (gitignored; the builder gives Tudor the text to paste):
  use the music tools for any music request, name what actually started, keep music
  replies to one short sentence, and never read out URIs or IDs.

### Errors
Each returns a JSON error that Appy turns into one sentence: Riff wouldn't start, Riff not
logged in, not logged in to Spotify / token revoked, no Premium (Spotify's 403), nothing
found for the query, network down. Nothing crashes the turn.

### Tests
Unit tests only, with a fake bus and a fake HTTP transport (no live Spotify or Riff in
CI): tool routing and argument validation, playlist-by-name preference, liked songs,
queue vs play, token refresh and rotation, device lookup plus auto-start, ducking
save/restore (including a volume change while ducked, and speech discarded as noise),
forced-call phrases strict vs normal, the utterance cap on and off, the flag off giving
today's behaviour, the card payload, and the Rust card parsing the new widget kind.

## Calls made by Claude (correct any)
- "DJ mode" = always-available tools, not a switchable mode or persona.
- `jeepney` for D-Bus; loopback port 8898; token file in `~/.config/appy/`.
- Two tool schemas, not one per action.
- Duck to ~25% while Appy talks and as soon as Tudor starts talking.
- Now-playing card without album art.
- Liked songs: collection first, newest-100 shuffled as fallback.
- Fallback to `OpenUri` if Riff isn't a Connect device.
- Workaround flag name, tag string, ~15 s utterance cap.

## Left to the builder
Exact ducking level and timing, forced-call phrase list and the strict-mode word limit,
Riff's Connect device name, how long to wait for Riff to start, echo-cancel tuning, card
layout details, prompt wording.

## Live findings (2026-11-28)
- Riff's MPRIS works for transport, Volume (read and set), Metadata; while idle it reports
  the title "Not playing" with no artist.
- Riff does not register as a Spotify Connect device (`/me/player/devices` is empty while
  it plays, also with its experimental Device Selector on). Its MPRIS `OpenUri` answers
  `NotSupported: Not implemented`; `SupportedUriSchemes` is empty. Its `spotify:` link
  handler (`org.gtk.Application.Open`) only opens the page; launching a second `riff`
  process to pass a link knocks the running Riff off MPRIS. Its exported GTK actions
  have nothing that plays a given URI. **Riff can't be told to play a specific song.**
- Search, `/me/playlists` (25) and `/me` work with the PKCE login.

## Amendment: Appy plays through his own background player (Tudor's pick)
Riff stays Tudor's app for hands-on listening; Appy gets a headless Spotify Connect
player, spotifyd, for playing things by name.

- **Player:** spotifyd 0.4.2 (official "full" release binary, sha512-checked), installed
  at `~/.local/bin/spotifyd` (outside the repo; README says how). Started by Appy at boot
  and stopped when he quits, so with Appy off nothing changes for Tudor:
  `spotifyd --no-daemon --use-mpris --device-name Appy --device-type computer
  --backend pulseaudio --bitrate 320 --volume-normalisation --disable-discovery
  --cache-path ~/.cache/appy/spotifyd`. Missing binary or login → Appy boots anyway, logs
  it once, and play_music says the music player isn't set up.
- **Login:** `appy spotify-login` runs the Web API login and then `spotifyd authenticate`
  with the same cache path (one-time browser approval each).
- **Playing by name** (track, artist, album, playlist, liked, queue): Web API on the
  Connect device named "Appy". Riff auto-launch and the Riff OpenUri fallback are dropped.
- **Transport, volume, now-playing, ducking, card, forced-call gating** follow the
  *active* player: whichever of Riff and spotifyd is Playing; if neither, the one that
  played last; spotifyd's MPRIS name is `org.mpris.MediaPlayer2.spotifyd.instance<PID>`
  (exposed once it is the playback device).
- Tudor moves Appy's music into Riff by picking "This Device" in Riff's Device Selector.
- Unchanged: search, playlists/liked/queue logic, tools' schemas, the card, the hearing
  workaround.
