# Architecture & Technical Design

This document details the internal architecture, database schema, audio synthesis pipeline, frontend layout, and security controls of **Sonorius**.

---

## 1. System Architecture Overview

Sonorius addresses a common browser limitation in voice-assisted study tools: **web browsers enforce strict user-gesture rules that prevent scripts from sequentially autoplaying separate audio files.**

If a study session has 10 questions and 10 answers, triggering 20 separate audio plays in the browser will stall after the first file.

Sonorius solves this by assembling the entire session into a single composite audio track on the server before playback begins:

```text
[User Selects Playlist or Single Question]
          │
          ▼
[Fetch Questions & Answers from SQLite]
          │
          ▼
[SHA-256 Audio Cache Lookup in audio_cache/ -> On Miss: Kokoro-82M TTS]
          │
          ▼
[Pydub Audio Stitcher: Question -> Think Pause -> Answer -> Next Question Delay]
          │
          ▼
[Export Single Composite MP3 in Memory (io.BytesIO)]
          │
          ▼
[Single Autoplay Stream in HTML5 Audio Player]
```

---

## 2. SQLite Database Schema

The database is stored locally in `interview_data.db`. File permissions are set to `0600` (owner read/write only) on startup, and `PRAGMA foreign_keys = ON` is enabled on every connection.

```sql
-- 1. Question Vault
CREATE TABLE IF NOT EXISTS Questions (
    question_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    image_path TEXT
);

-- 2. Playlists
CREATE TABLE IF NOT EXISTS Playlists (
    playlist_id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_name TEXT NOT NULL UNIQUE
);

-- 3. Junction Table: Playlist Sequencing
CREATE TABLE IF NOT EXISTS Playlist_Items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    play_order INTEGER NOT NULL,
    FOREIGN KEY (playlist_id) REFERENCES Playlists(playlist_id) ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (question_id) REFERENCES Questions(question_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- 4. Persistent User Preferences
CREATE TABLE IF NOT EXISTS App_Settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
);
```

### Question Deletion, ID Compaction & Cache Cleanup
When one or more questions are deleted (`delete_questions()`):
1. **Image Cleanup:** Any associated diagram file on disk in `images/` is removed via `os.remove()`.
2. **Row & Reference Removal:** Matching rows in `Playlist_Items` and `Questions` are deleted.
3. **Contiguous ID Compaction (`_compact_question_ids()`):** Remaining `question_id` values in `Questions` are renumbered contiguously (`1..N`). Because `Playlist_Items` defines `ON UPDATE CASCADE`, foreign key references update automatically. Each playlist's `play_order` is then re-sequenced (`1..M`), and `sqlite_sequence` is reset so the next inserted question receives `N + 1`.
4. **Audio Cache Purge (`clear_audio_cache()`):** Editing or deleting any question removes all cached `.mp3` clips in `audio_cache/` so audio from edited or deleted questions never remains on disk. (Adding new questions preserves existing cached clips.)

---

## 3. Audio Synthesis & Composition Pipeline

### The Kokoro-82M Neural Engine
Sonorius connects to **Kokoro-82M** over an OpenAI-compatible `/v1/audio/speech` endpoint (default `http://localhost:8880/v1/audio/speech`).
- **Payload Format:**
  ```json
  {
    "model": "kokoro",
    "input": "Question 1. What is Sonorius and how does it help preparation?",
    "voice": "af_sarah",
    "speed": 1.0,
    "response_format": "mp3"
  }
  ```

### SHA-256 Chunk Caching & Server-Side Stitching
Each speech clip is cached in `audio_cache/<sha256>.mp3` (`0600` permissions using atomic `.tmp` + `os.replace()`), where the key is `sha256(f"{voice}|{float(speed):.2f}|{text}")`.

During `stitch_playlist_audio()`, Sonorius prepends spoken cues (`"Question {idx}."` for playlists, `"Question."` for a single question, and `"Answer."` before answers), retrieves or synthesizes each MP3 segment, and concatenates them in memory using `pydub.AudioSegment`:

```python
# Simplified structure of stitch_playlist_audio() in app.py
combined = AudioSegment.empty()
think_silence = AudioSegment.silent(duration=int(think_time_sec * 1000)) if think_time_sec > 0 else AudioSegment.empty()
transition_silence = AudioSegment.silent(duration=int(pause_between_sec * 1000)) if pause_between_sec > 0 else AudioSegment.empty()

for idx, q in enumerate(questions, start=1):
    # 1. Question clip (Interviewer voice)
    prefix = f"Question {idx}." if len(questions) > 1 else "Question."
    q_bytes = generate_kokoro_speech(f"{prefix} {q['question_text']}", voice=q_voice, speed=speed)
    combined += AudioSegment.from_file(io.BytesIO(q_bytes), format="mp3")

    # 2. Think Time pause (skipped when think_time_sec == 0)
    if len(think_silence) > 0:
        combined += think_silence

    # 3. Answer clip (Candidate voice)
    a_bytes = generate_kokoro_speech(f"Answer. {q['answer_text']}", voice=a_voice, speed=speed)
    combined += AudioSegment.from_file(io.BytesIO(a_bytes), format="mp3")

    # 4. Next Question Delay (inserted between questions only, not after the last item)
    if idx < len(questions) and len(transition_silence) > 0:
        combined += transition_silence

out_buf = io.BytesIO()
combined.export(out_buf, format="mp3")
st.session_state["session_audio_bytes"] = out_buf.getvalue()
```

---

## 4. Frontend Component Design

- **Left Sidebar Navigation:** Controlled via `st.sidebar.radio` with 3 routes (`ROUTE_PLAY`, `ROUTE_ADD`, `ROUTE_BUILD`).
- **Session Lockout Mechanism:** While a practice session is active (`st.session_state["is_session_playing"] = True`), navigation, preferences, and vault/playlist editing controls are disabled until the user clicks **Stop Practice** (or the session finishes).
- **Study Card View (`ROUTE_PLAY`):** Renders in vertical order:
  1. **Question Card** (with question position badge and `ID #`)
  2. **Answer Card** (scrollable container for long answers)
  3. **Previous / Next Navigation Bar**
  4. **Attached Diagram** (if `image_path` is present on disk)
- **Collapsible Management Hierarchy (`ROUTE_BUILD`):** Organized into two sections:
  - **Manage Questions:** Question Vault table with keyword search, Edit Question form, and Batch Question Deletion checklist.
  - **Manage Playlists:** Create New Playlist (checklist picker + numeric play-order table), Edit an Existing Playlist, and Delete Existing Playlists.
- **Interactive Intro Overlay:** Clicking the sidebar **Sonorius** brand header launches an HTML5 Canvas 3D dot-grid wave animation with warm orange voice/ripple highlights (`#FF9560`) and Web Audio API analyser reactivity synced to `intro_audio.mp3`.

---

## 5. Transport Security & Data Sanitization

- **Inbound Localhost Isolation (`enforce_client_transport_security()`):** By default, Sonorius binds to `localhost` and inspects client `Host`, `Origin`, and `X-Forwarded-Proto` headers so port-forwarding layers (Docker, WSL2, or IDE forwarders) cannot expose the app over plain HTTP to other devices on the network. When started with `./run_app.sh --lan`, remote devices are required to connect over `https://`.
- **Dual HTTP / HTTPS Socket Multiplexing (`run_app.sh` + `tls_proxy.py`):** When TLS is enabled (`--lan`, `--https`, or `auto` mode with `mkcert`/custom certs), `./run_app.sh` launches `tls_proxy.py` on port `8501`, which inspects the first byte (`0x16` for TLS handshake) to route both `http://localhost:8501` and `https://` (`wss://`) traffic to the internal Streamlit server. In default local mode without `mkcert` or custom certs, Streamlit runs directly on `http://localhost:8501`.
- **Outbound TTS Validation (`inspect_tts_endpoint()`):** Loopback TTS URLs (`localhost`, `127.0.0.1`, `::1`) are allowed over `http://` or `https://`. Any remote or LAN `KOKORO_TTS_URL` must use `https://` by default (unless `ALLOW_INSECURE_HTTP_TTS=true` is explicitly set in `.env`), and link-local metadata addresses (`169.254.0.0/16`, `fe80::/10`) are always blocked.
- **Text & Image Sanitization (`sanitize_text_field()`, `save_verified_image()`):** Text fields strip control bytes, enforce length limits (`2,000` chars for questions, `10,000` for answers, `120` for playlist names), and block spreadsheet formula/DDE command patterns. Uploaded images verify magic bytes (`.png`, `.jpg`, `.jpeg`, `.webp`), guard against decompression bombs (`25 MP` max), fully decode and re-encode pixel data via Pillow under a random UUID filename, and save with `0600` permissions.
