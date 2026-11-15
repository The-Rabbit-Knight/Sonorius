import os
import io
import base64
import hashlib
import html as html_lib
import ipaddress
import json
import re
import shutil
import sqlite3
import urllib.parse
import uuid
import requests
import pandas as pd
from PIL import Image

# Prefer system ffmpeg when available; only fall back to static_ffmpeg if missing
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    import static_ffmpeg
    static_ffmpeg.add_paths()

# Guard against decompression-bomb images (~25 megapixels max)
Image.MAX_IMAGE_PIXELS = 25_000_000

import streamlit as st
import streamlit.components.v1 as components
from pydub import AudioSegment

# ---------------------------------------------------------
# Anchor all relative paths (DB, images, session audio) to the app folder,
# so launching Streamlit from another directory never opens an empty vault.
# Stored image paths in the DB are relative ("images/..."), so this keeps them valid.
# ---------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# ---------------------------------------------------------
# Load Environment Variables from local .env if present
# ---------------------------------------------------------
ENV_FILE = os.path.join(BASE_DIR, ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ---------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------
DB_FILE = "interview_data.db"
IMAGES_DIR = "images"
AUDIO_OUTPUT_FILE = "current_session.mp3"
INTRO_AUDIO_FILE = os.path.join(os.path.dirname(__file__), "intro_audio.mp3")
KOKORO_TTS_URL = os.environ.get("KOKORO_TTS_URL", "http://localhost:8880/v1/audio/speech")
KOKORO_CA_BUNDLE = os.environ.get("KOKORO_CA_BUNDLE", "").strip()
ALLOW_INSECURE_HTTP_TTS = os.environ.get("ALLOW_INSECURE_HTTP_TTS", "false").strip().lower() in ("1", "true", "yes")
ALLOW_LAN_ACCESS = os.environ.get("ALLOW_LAN_ACCESS", "false").strip().lower() in ("1", "true", "yes")
DEFAULT_VOICE = "af_heart"
DEFAULT_Q_VOICE = "af_sarah"
DEFAULT_A_VOICE = "am_michael"

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
PIL_FORMAT_TO_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
MAX_QUESTION_LEN = 2000
MAX_ANSWER_LEN = 10000
MAX_PLAYLIST_NAME_LEN = 120

# Control characters except \t (\x09), \n (\x0a), \r (\x0d)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Spreadsheet DDE / command-execution formula prefixes
_DANGEROUS_FORMULA_RE = re.compile(r"^[=+\-@]\s*(?:cmd|powershell|wscript|cscript|mshta|rundll32|sh|bash)\b", re.IGNORECASE)

def sanitize_text_field(raw: str, field_name: str, max_len: int) -> str:
    """Strip control bytes, block DDE/formula command payloads, and enforce length limits."""
    cleaned = _CONTROL_CHARS_RE.sub("", str(raw or "")).strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be empty.")
    if len(cleaned) > max_len:
        raise ValueError(f"{field_name} exceeds the maximum length of {max_len:,} characters (got {len(cleaned):,}).")
    if _DANGEROUS_FORMULA_RE.match(cleaned):
        raise ValueError(f"{field_name} contains a blocked spreadsheet command pattern.")
    # Neutralize leading '=' or '@' formula triggers so exported CSVs are safe in Excel
    if cleaned[0] in ("=", "@"):
        cleaned = "'" + cleaned
    return cleaned

def _is_loopback_host(host: str) -> bool:
    """Return True if host is localhost or a loopback IP (127.0.0.0/8, ::1)."""
    if not host:
        return True
    h = host.strip().lower().strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False

def enforce_client_transport_security():
    """Block non-localhost browsers and unencrypted LAN HTTP requests inside the app itself.

    Why this is needed in addition to `server.address = "localhost"`:
    When a user runs Sonorius inside Docker, WSL2, a VM, or an IDE with port forwarding
    (like `socat` or VS Code port forwarding), the host OS may forward LAN traffic
    (e.g. `http://192.168.X.X:8501`) into the container's `127.0.0.1:8501`.
    Inspecting the HTTP `Host` / `Origin` / `X-Forwarded-Proto` headers stops
    second-machine LAN access and plain-HTTP LAN exposure even through port forwarders.
    """
    try:
        headers = st.context.headers or {}
        ctx_url = getattr(st.context, "url", "") or ""
    except Exception:
        return

    host_hdr = headers.get("Host") or headers.get("host") or ""
    origin_hdr = headers.get("Origin") or headers.get("origin") or ""
    fwd_proto = (headers.get("X-Forwarded-Proto") or headers.get("x-forwarded-proto") or "").lower()

    # Determine the hostname the browser actually typed into its address bar
    browser_host = ""
    if ctx_url:
        try:
            browser_host = urllib.parse.urlparse(ctx_url).hostname or ""
        except Exception:
            pass
    if not browser_host and host_hdr:
        try:
            browser_host = urllib.parse.urlparse(f"http://{host_hdr}").hostname or ""
        except Exception:
            browser_host = host_hdr.split(":")[0]

    if _is_loopback_host(browser_host):
        return  # Strictly local browser on the same machine

    # Determine whether the browser connection is encrypted (HTTPS)
    scheme = "http"
    if fwd_proto in ("https", "wss"):
        scheme = "https"
    elif fwd_proto not in ("http", "ws") and (
        ctx_url.lower().startswith("https://") or origin_hdr.lower().startswith("https://")
    ):
        scheme = "https"

    if not ALLOW_LAN_ACCESS:
        st.error(
            f":material/lock: **Network Access Blocked (`{browser_host}`)**\n\n"
            "Sonorius is locked to **`localhost` only** by default to protect your private Q&A vault "
            "(even when Docker, WSL, or IDE port-forwarding exposes port 8501 on your LAN).\n\n"
            "- **To use on this computer:** Open `http://localhost:8501` or `http://127.0.0.1:8501`.\n"
            "- **To allow another device on your network (over HTTPS only):** Start with `./run_app.sh --lan` "
            "(or set `ALLOW_LAN_ACCESS=true` in `.env` with HTTPS enabled)."
        )
        st.stop()

    if scheme != "https":
        st.error(
            f":material/shield_lock: **Unencrypted LAN Connection Refused (`http://{browser_host}`)**\n\n"
            "Accessing Sonorius from another machine over plain **HTTP** is blocked because your private "
            "questions, answers, and voice audio would travel unencrypted across Wi-Fi/LAN.\n\n"
            f"Please connect over **HTTPS** (`https://{host_hdr or browser_host}`) using `./run_app.sh --lan`."
        )
        st.stop()

def ensure_private_dir(path: str):
    """Create a directory and restrict permissions to the current user (0o700)."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass

def set_private_file(path: str):
    """Restrict file permissions to the current user (0o600)."""
    if path and os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

def _has_valid_image_signature(data: bytes, ext: str) -> bool:
    """Verify magic bytes match PNG, JPEG, or WebP before saving to disk."""
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False

def save_verified_image(raw_bytes: bytes, original_name: str) -> str:
    """Verify magic bytes, fully decode pixels via Pillow, and re-encode a clean image under a random UUID.

    Re-encoding from decoded pixel data strips any appended polyglot payloads (.exe/.zip),
    active scripts, or sensitive EXIF metadata before writing to disk with 0o600 permissions.
    """
    ext = os.path.splitext(os.path.basename(original_name))[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image extension '{ext}'. Allowed: PNG, JPG, WEBP.")
    if not _has_valid_image_signature(raw_bytes, ext):
        raise ValueError(f"File '{os.path.basename(original_name)}' is not a valid {ext.upper().lstrip('.')} image.")
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            fmt = (img.format or "").upper()
            if fmt not in PIL_FORMAT_TO_EXT:
                raise ValueError(f"Unsupported image format '{fmt}'.")
            img.load()  # Force full pixel decode to catch truncated/corrupt/polyglot files
            clean_ext = PIL_FORMAT_TO_EXT[fmt]
            out_buf = io.BytesIO()
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            save_kwargs = {"quality": 92} if fmt in ("JPEG", "WEBP") else {}
            img.save(out_buf, format=fmt, **save_kwargs)
            clean_bytes = out_buf.getvalue()
    except Image.DecompressionBombError:
        raise ValueError("Image dimensions are too large (exceeds 25 MP limit).")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Image verification failed for '{os.path.basename(original_name)}': {exc}")

    ensure_private_dir(IMAGES_DIR)
    target_path = os.path.join(IMAGES_DIR, f"{uuid.uuid4().hex}{clean_ext}")
    with open(target_path, "wb") as f_out:
        f_out.write(clean_bytes)
    set_private_file(target_path)
    return target_path

def inspect_tts_endpoint(url: str) -> dict:
    """Validate KOKORO_TTS_URL transport security for both local and remote setups.

    - Loopback (localhost / 127.0.0.1 / ::1) is safe over http or https.
    - Any remote or LAN host MUST use https:// by default so interview Q&As
      are never transmitted in cleartext across Wi-Fi or the internet.
    - Link-local cloud metadata addresses (169.254.0.0/16) are always blocked.
    """
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return {"ok": False, "mode": "invalid", "reason": "Malformed KOKORO_TTS_URL."}

    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return {
            "ok": False,
            "mode": "invalid",
            "reason": "KOKORO_TTS_URL must start with https:// (or http:// for localhost only).",
        }

    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_link_local:
                return {
                    "ok": False,
                    "mode": "blocked",
                    "reason": "Link-local addresses (169.254.x.x / fe80::) are blocked for security.",
                }
            is_loopback = ip.is_loopback
        except ValueError:
            is_loopback = False

    if scheme == "https":
        return {"ok": True, "mode": "local_https" if is_loopback else "remote_https", "reason": ""}

    if is_loopback:
        return {"ok": True, "mode": "local_loopback", "reason": ""}

    if ALLOW_INSECURE_HTTP_TTS:
        return {
            "ok": True,
            "mode": "remote_http_override",
            "reason": f"Unencrypted HTTP to remote host '{host}' allowed via ALLOW_INSECURE_HTTP_TTS=true.",
        }

    return {
        "ok": False,
        "mode": "remote_http_blocked",
        "reason": (
            f"Security block: KOKORO_TTS_URL points to a non-local host ('{host}') over unencrypted HTTP. "
            "Sending private questions and answers in cleartext over a network is disabled by default. "
            "Use https:// (with KOKORO_CA_BUNDLE for self-signed certs), an SSH tunnel to localhost, "
            "or set ALLOW_INSECURE_HTTP_TTS=true in .env only on a trusted encrypted tunnel."
        ),
    }

def get_intro_audio_b64() -> str:
    """Read fresh base64 encoded audio for the cinematic intro from disk."""
    if os.path.exists(INTRO_AUDIO_FILE):
        with open(INTRO_AUDIO_FILE, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    return ""

VOICE_OPTIONS = {
    # Solo Flagship Voices
    "af_heart": "Heart — Female (American, Flagship Grade-A)",
    "af_bella": "Bella — Female (American, Warm & Natural)",
    "af_sarah": "Sarah — Female (American, Professional & Clear)",
    "af_nicole": "Nicole — Female (American, Crisp & Direct)",
    "am_fenrir": "Fenrir — Male (American, Deep & Authoritative)",
    "am_michael": "Michael — Male (American, Friendly & Conversational)",
    "am_adam": "Adam — Male (American, Deep & Steady)",
    "bf_emma": "Emma — Female (British, Refined & Polished)",
    "bm_george": "George — Male (British, Authoritative & Formal)",
    # Blended Voice Presets
    "af_heart(0.6)+af_bella(0.4)": "Executive Blend: Heart + Bella (Natural & Warm)",
    "af_heart(0.6)+af_sarah(0.4)": "Confident Blend: Heart + Sarah (Clear & Assertive)",
    "am_fenrir(0.6)+am_michael(0.4)": "Anchor Blend: Fenrir + Michael (Balanced Deep Male)",
    "af_bella(0.5)+af_nicole(0.5)": "Dynamic Blend: Bella + Nicole (Conversational & Sharp)",
    "af_heart(0.5)+am_michael(0.5)": "Duo Harmony: Heart + Michael (Hybrid Cadence)"
}

ROUTE_PLAY = ":material/play_circle: Play Session"
ROUTE_ADD = ":material/add_circle: Add New Q&A"
ROUTE_BUILD = ":material/queue_music: Manage Playlists"

ensure_private_dir(IMAGES_DIR)

# ---------------------------------------------------------
# Streamlit UI Configuration
# ---------------------------------------------------------
st.set_page_config(
    page_title="Sonorius - Voice Prep",
    page_icon=":material/graphic_eq:",
    layout="wide"
)

# ---------------------------------------------------------
# Theme Styling
# ---------------------------------------------------------
THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,400,0,0&display=swap');

.material-symbols-rounded {
    font-family: 'Material Symbols Rounded' !important;
    font-weight: normal;
    font-style: normal;
    font-size: 1.35rem;
    line-height: 1;
    display: inline-block;
    vertical-align: middle;
}

/* Completely Hide Streamlit Top Header, Menu, Footer & Decorators */
#MainMenu { visibility: hidden !important; display: none !important; }
header, [data-testid="stHeader"] { 
    display: none !important; 
    visibility: hidden !important; 
    height: 0px !important; 
}
footer { visibility: hidden !important; display: none !important; }
[data-testid="stDecoration"] { display: none !important; }

/* Hide Enter instructions */
div[data-testid="InputInstructions"] { display: none !important; }

/* Default Base Theme Colors */
.stApp, [data-testid="stAppViewContainer"] {
    background-color: #0E1117 !important;
    font-family: 'Inter', sans-serif !important;
    color: #D1D5DB !important;
}

[data-testid="stSidebar"] {
    background-color: #262730 !important;
    border-right: 1px solid #374151 !important;
}

/* Softened Labels */
label, 
.stWidgetLabel p, 
label[data-testid="stWidgetLabel"] p, 
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] span {
    color: #E5E7EB !important;
    font-size: 0.88rem !important;
    font-weight: 500 !important;
}

/* Radio Navigation Styling */
div[data-testid="stRadio"] > div {
    background: #1A1D24;
    border: 1px solid #374151;
    border-radius: 6px;
    padding: 6px 10px;
}

/* Inputs & Textareas */
textarea, 
input, 
[data-baseweb="textarea"] textarea, 
[data-baseweb="input"] input,
div[data-testid="stTextInput"] input,
div[data-testid="stTextArea"] textarea {
    color: #E5E7EB !important;
    background-color: #1A1D24 !important;
    border: 1px solid #374151 !important;
    border-radius: 6px !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.92rem !important;
}

textarea:focus, input:focus {
    border-color: #3B82F6 !important;
    box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.4) !important;
}

/* Card Containers */
.prep-card {
    background: #1A1D24;
    border: 1px solid #374151;
    border-radius: 8px;
    padding: 1.1rem 1.3rem;
    margin-bottom: 1rem;
}

.prep-card-answer {
    max-height: 240px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: #4B5563 #1A1D24;
    padding-right: 6px;
}

.prep-card-answer::-webkit-scrollbar {
    width: 6px;
}
.prep-card-answer::-webkit-scrollbar-track {
    background: #1A1D24;
}
.prep-card-answer::-webkit-scrollbar-thumb {
    background: #4B5563;
    border-radius: 3px;
}

.prep-card-highlight {
    background: #1E232B;
    border: 1px solid #059669;
    border-radius: 8px;
    padding: 1.1rem 1.3rem;
    margin-bottom: 1rem;
}

/* Softened Badges */
.badge-green {
    background: rgba(16, 185, 129, 0.15);
    border: 1px solid rgba(16, 185, 129, 0.4);
    color: #34D399;
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 500;
}

.badge-blue {
    background: rgba(59, 130, 246, 0.15);
    border: 1px solid rgba(59, 130, 246, 0.4);
    color: #60A5FA;
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 500;
}

.badge-orange {
    background: rgba(245, 158, 11, 0.15);
    border: 1px solid rgba(245, 158, 11, 0.4);
    color: #FBBF24;
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 500;
}

/* Normalized Headers */
h1, h2, h3, h4 {
    font-family: 'Inter', sans-serif !important;
    color: #E5E7EB !important;
    font-weight: 600 !important;
}

/* Sonorius Brand Header - Blends naturally with sidebar, static appearance */
.sb-brand {
    padding: 0.2rem 0 0.8rem 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: default;
    user-select: none;
    background: transparent;
}

.sb-brand.sb-brand-locked {
    opacity: 0.7 !important;
}

.sb-logo {
    font-family: 'Inter', sans-serif;
    font-size: 1.4rem;
    font-weight: 700;
    color: #F3F4F6;
    letter-spacing: -0.04em;
    line-height: 1;
}

.sb-logo em {
    color: #FF9560;
    font-style: normal;
    font-weight: 800;
}

.sb-version {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: #9CA3AF;
    letter-spacing: 0.05em;
    display: flex;
    align-items: center;
    gap: 5px;
}

.sb-live-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #10B981;
    display: inline-block;
}

/* Force Dark Mode on native Streamlit Audio Player */
div[data-testid="stAudio"] {
    background-color: #1A1D24 !important;
    border: 1px solid #374151 !important;
    border-radius: 8px !important;
    padding: 8px 12px !important;
    margin-bottom: 12px !important;
    color-scheme: dark !important;
}

div[data-testid="stAudio"] audio {
    width: 100% !important;
    height: 40px !important;
    color-scheme: dark !important;
    accent-color: #FF9560 !important;
    background-color: transparent !important;
}

div[data-testid="stAudio"] audio::-webkit-media-controls-panel,
div[data-testid="stAudio"] audio::-webkit-media-controls-enclosure {
    background-color: #1A1D24 !important;
    color-scheme: dark !important;
}

/* ══ MAIN AREA TOPBAR & PRIVACY BADGE ═══════════════════════════ */
.wn-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.2rem 0 0.6rem;
    margin-top: -10px;
    animation: fadeIn 0.25s ease both;
}
.wn-topbar-title {
    font-family: 'Inter', sans-serif;
    font-size: 0.95rem;
    font-weight: 500;
    color: #9CA3AF;
}
.wn-topbar-title strong {
    color: #F3F4F6;
    font-weight: 700;
}
.wn-privacy-badge {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: #9CA3AF;
    letter-spacing: 0.05em;
    padding: 3px 9px;
    border: 1px solid #374151;
    border-radius: 5px;
    background: #1A1D24;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 6px;
}

div[data-testid="stAudio"] audio::-webkit-media-controls-current-time-display,
div[data-testid="stAudio"] audio::-webkit-media-controls-time-remaining-display {
    color: #D1D5DB !important;
    text-shadow: none !important;
}
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)
enforce_client_transport_security()

# ---------------------------------------------------------
# Database Initialization & Management
# ---------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def _compact_question_ids(cursor):
    """Renumber all remaining Questions contiguously (1..N), cascade updates to Playlist_Items,
    re-sequence Playlist_Items.play_order (1..M), and sync sqlite_sequence to N."""
    rows = cursor.execute("SELECT question_id FROM Questions ORDER BY question_id ASC").fetchall()
    for new_id, row in enumerate(rows, start=1):
        old_id = row["question_id"]
        if old_id != new_id:
            cursor.execute("UPDATE Questions SET question_id = ? WHERE question_id = ?", (new_id, old_id))

    # Re-sequence play_order (1..M) within each playlist after any question removals
    pl_rows = cursor.execute("SELECT DISTINCT playlist_id FROM Playlist_Items").fetchall()
    for pl in pl_rows:
        pl_id = pl["playlist_id"]
        items = cursor.execute(
            "SELECT id, play_order FROM Playlist_Items WHERE playlist_id = ? ORDER BY play_order ASC, id ASC",
            (pl_id,)
        ).fetchall()
        for new_order, item in enumerate(items, start=1):
            if item["play_order"] != new_order:
                cursor.execute("UPDATE Playlist_Items SET play_order = ? WHERE id = ?", (new_order, item["id"]))

    # Reset AUTOINCREMENT counter so the next inserted question is always N + 1 (or 1 if empty)
    cursor.execute("DELETE FROM sqlite_sequence WHERE name = 'Questions'")
    if rows:
        cursor.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('Questions', ?)", (len(rows),))


def init_db():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Questions (
                question_id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_text TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                image_path TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Playlists (
                playlist_id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_name TEXT NOT NULL UNIQUE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Playlist_Items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                play_order INTEGER NOT NULL,
                FOREIGN KEY (playlist_id) REFERENCES Playlists(playlist_id) ON DELETE CASCADE ON UPDATE CASCADE,
                FOREIGN KEY (question_id) REFERENCES Questions(question_id) ON DELETE CASCADE ON UPDATE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS App_Settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
        """)

        # Migrate existing Playlist_Items table if created before ON UPDATE CASCADE was added
        tbl_sql_row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='Playlist_Items'"
        ).fetchone()
        if tbl_sql_row and "ON UPDATE CASCADE" not in (tbl_sql_row["sql"] or "").upper():
            cursor.execute("ALTER TABLE Playlist_Items RENAME TO _Playlist_Items_old")
            cursor.execute("""
                CREATE TABLE Playlist_Items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    play_order INTEGER NOT NULL,
                    FOREIGN KEY (playlist_id) REFERENCES Playlists(playlist_id) ON DELETE CASCADE ON UPDATE CASCADE,
                    FOREIGN KEY (question_id) REFERENCES Questions(question_id) ON DELETE CASCADE ON UPDATE CASCADE
                )
            """)
            cursor.execute("""
                INSERT INTO Playlist_Items (id, playlist_id, question_id, play_order)
                SELECT id, playlist_id, question_id, play_order FROM _Playlist_Items_old
            """)
            cursor.execute("DROP TABLE _Playlist_Items_old")

        _compact_question_ids(cursor)
        conn.commit()
    finally:
        conn.close()

    if os.path.exists(DB_FILE):
        try:
            os.chmod(DB_FILE, 0o600)
        except OSError:
            pass

init_db()

# ---------------------------------------------------------
# Database Helper Functions
# ---------------------------------------------------------
def fetch_all_questions():
    conn = get_db_connection()
    try:
        return conn.execute("SELECT * FROM Questions ORDER BY question_id ASC").fetchall()
    finally:
        conn.close()

def fetch_all_playlists():
    conn = get_db_connection()
    try:
        return conn.execute("SELECT * FROM Playlists ORDER BY playlist_id ASC").fetchall()
    finally:
        conn.close()

def fetch_playlist_questions(playlist_id):
    conn = get_db_connection()
    try:
        query = """
            SELECT q.question_id, q.question_text, q.answer_text, q.image_path, pi.play_order
            FROM Playlist_Items pi
            JOIN Questions q ON pi.question_id = q.question_id
            WHERE pi.playlist_id = ?
            ORDER BY pi.play_order ASC
        """
        return conn.execute(query, (playlist_id,)).fetchall()
    finally:
        conn.close()

def add_question(question_text, answer_text, image_path=None):
    clean_q = sanitize_text_field(question_text, "Question Text", MAX_QUESTION_LEN)
    clean_a = sanitize_text_field(answer_text, "Answer Text", MAX_ANSWER_LEN)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO Questions (question_text, answer_text, image_path)
            VALUES (?, ?, ?)
        """, (clean_q, clean_a, image_path))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()

def bulk_add_questions(rows_to_insert):
    """Insert multiple (question_text, answer_text, image_path) tuples in one atomic transaction."""
    if not rows_to_insert:
        return 0
    sanitized = [
        (
            sanitize_text_field(q_t, "Question Text", MAX_QUESTION_LEN),
            sanitize_text_field(a_t, "Answer Text", MAX_ANSWER_LEN),
            img_p,
        )
        for q_t, a_t, img_p in rows_to_insert
    ]
    conn = get_db_connection()
    try:
        conn.executemany(
            "INSERT INTO Questions (question_text, answer_text, image_path) VALUES (?, ?, ?)",
            sanitized,
        )
        conn.commit()
    finally:
        conn.close()
    return len(sanitized)

def update_question(question_id, question_text, answer_text, new_image_path=None, remove_image=False):
    clean_q = sanitize_text_field(question_text, "Question Text", MAX_QUESTION_LEN)
    clean_a = sanitize_text_field(answer_text, "Answer Text", MAX_ANSWER_LEN)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        row = cursor.execute("SELECT image_path FROM Questions WHERE question_id = ?", (question_id,)).fetchone()
        current_img = row["image_path"] if row else None

        final_img_path = current_img
        if new_image_path:
            # New upload takes precedence even if "Remove current diagram" was also ticked
            if current_img and os.path.exists(current_img) and current_img != new_image_path:
                try:
                    os.remove(current_img)
                except OSError:
                    pass
            final_img_path = new_image_path
        elif remove_image:
            if current_img and os.path.exists(current_img):
                try:
                    os.remove(current_img)
                except OSError:
                    pass
            final_img_path = None

        cursor.execute("""
            UPDATE Questions
            SET question_text = ?, answer_text = ?, image_path = ?
            WHERE question_id = ?
        """, (clean_q, clean_a, final_img_path, question_id))
        conn.commit()
    finally:
        conn.close()
    clear_audio_cache()

def delete_questions(question_ids):
    """Delete one or more questions by ID, remove their diagram files, and compact remaining IDs to 1..N."""
    if not question_ids:
        return
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        for qid in question_ids:
            row = cursor.execute("SELECT image_path FROM Questions WHERE question_id = ?", (qid,)).fetchone()
            if row and row["image_path"] and os.path.exists(row["image_path"]):
                try:
                    os.remove(row["image_path"])
                except OSError:
                    pass
            cursor.execute("DELETE FROM Playlist_Items WHERE question_id = ?", (qid,))
            cursor.execute("DELETE FROM Questions WHERE question_id = ?", (qid,))
        _compact_question_ids(cursor)
        conn.commit()
    finally:
        conn.close()
    clear_audio_cache()


def delete_question(question_id):
    delete_questions([question_id])

def create_playlist(name, question_ids):
    clean_name = sanitize_text_field(name, "Playlist Name", MAX_PLAYLIST_NAME_LEN)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO Playlists (playlist_name) VALUES (?)", (clean_name,))
        playlist_id = cursor.lastrowid
        for idx, qid in enumerate(question_ids, start=1):
            cursor.execute("""
                INSERT INTO Playlist_Items (playlist_id, question_id, play_order)
                VALUES (?, ?, ?)
            """, (playlist_id, qid, idx))
        conn.commit()
        return playlist_id
    finally:
        conn.close()

def update_playlist(playlist_id, new_name, new_question_ids):
    clean_name = sanitize_text_field(new_name, "Playlist Name", MAX_PLAYLIST_NAME_LEN)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE Playlists SET playlist_name = ? WHERE playlist_id = ?", (clean_name, playlist_id))
        cursor.execute("DELETE FROM Playlist_Items WHERE playlist_id = ?", (playlist_id,))
        for idx, qid in enumerate(new_question_ids, start=1):
            cursor.execute("""
                INSERT INTO Playlist_Items (playlist_id, question_id, play_order)
                VALUES (?, ?, ?)
            """, (playlist_id, qid, idx))
        conn.commit()
    finally:
        conn.close()

def delete_playlist(playlist_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM Playlist_Items WHERE playlist_id = ?", (playlist_id,))
        conn.execute("DELETE FROM Playlists WHERE playlist_id = ?", (playlist_id,))
        conn.commit()
    finally:
        conn.close()

def get_app_setting(key: str, default: str = "") -> str:
    """Read a persistent setting from the database."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT setting_value FROM App_Settings WHERE setting_key = ?", (key,)).fetchone()
        return row["setting_value"] if row else default
    finally:
        conn.close()

def save_app_settings(settings: dict):
    """Save user preferences to the database so they persist across page refreshes."""
    conn = get_db_connection()
    try:
        for k, v in settings.items():
            conn.execute("""
                INSERT INTO App_Settings (setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """, (k, str(v)))
        conn.commit()
    finally:
        conn.close()

# ---------------------------------------------------------
# TTS & Audio Generation Helpers
# ---------------------------------------------------------
AUDIO_CACHE_DIR = "audio_cache"

def clear_audio_cache():
    """Remove cached TTS files and stale session audio so deleted/edited answers never linger on disk."""
    if os.path.isdir(AUDIO_CACHE_DIR):
        for fname in os.listdir(AUDIO_CACHE_DIR):
            if fname.endswith((".mp3", ".tmp")):
                try:
                    os.remove(os.path.join(AUDIO_CACHE_DIR, fname))
                except OSError:
                    pass
    if os.path.exists(AUDIO_OUTPUT_FILE) and not st.session_state.get("is_session_playing"):
        try:
            os.remove(AUDIO_OUTPUT_FILE)
        except OSError:
            pass

def _fetch_kokoro_speech(text: str, voice: str, speed: float) -> bytes:
    tts_check = inspect_tts_endpoint(KOKORO_TTS_URL)
    if not tts_check["ok"]:
        raise RuntimeError(tts_check["reason"])

    payload = {
        "model": "kokoro",
        "input": text,
        "voice": voice,
        "speed": float(speed),
        "response_format": "mp3"
    }
    verify_arg = KOKORO_CA_BUNDLE if KOKORO_CA_BUNDLE else True
    response = requests.post(KOKORO_TTS_URL, json=payload, timeout=60, verify=verify_arg)
    response.raise_for_status()
    return response.content

def generate_kokoro_speech(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> bytes:
    """Synthesize speech, reusing a local on-disk cache keyed by (text, voice, speed).

    Replaying a playlist with unchanged text/voice/speed makes zero TTS calls.
    Edited questions get a new key automatically, so stale audio is never served.
    """
    key = hashlib.sha256(f"{voice}|{float(speed):.2f}|{text}".encode("utf-8")).hexdigest()
    cache_path = os.path.join(AUDIO_CACHE_DIR, f"{key}.mp3")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    audio_bytes = _fetch_kokoro_speech(text, voice, speed)
    try:
        ensure_private_dir(AUDIO_CACHE_DIR)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)
        set_private_file(tmp_path)
        os.replace(tmp_path, cache_path)  # atomic: never leaves a half-written cache file
        set_private_file(cache_path)
    except OSError:
        pass  # caching is best-effort; playback still works
    return audio_bytes

def stitch_playlist_audio(questions, think_time_sec: int, pause_between_sec: int, q_voice: str = DEFAULT_Q_VOICE, a_voice: str = DEFAULT_A_VOICE, speed: float = 1.0) -> bytes:
    """Build the session MP3 with conversational dual-voice dialogue.

    Interviewer Voice speaks the question, followed by an optional think pause
    (or 0s for continuous playback without pause), then Candidate Voice speaks your curated answer.
    """
    combined = AudioSegment.empty()
    think_silence = AudioSegment.silent(duration=int(think_time_sec * 1000)) if think_time_sec > 0 else AudioSegment.empty()
    transition_silence = AudioSegment.silent(duration=int(pause_between_sec * 1000)) if pause_between_sec > 0 else AudioSegment.empty()
    segments = []

    progress_bar = st.progress(0)
    total_steps = len(questions) * 2
    current_step = 0

    for idx, q in enumerate(questions, start=1):
        seg = {"q_start": len(combined) / 1000.0}

        # 1. Question (Interviewer Persona)
        prefix = f"Question {idx}." if len(questions) > 1 else "Question."
        q_text = f"{prefix} {q['question_text']}"
        q_bytes = generate_kokoro_speech(q_text, voice=q_voice, speed=speed)
        q_audio = AudioSegment.from_file(io.BytesIO(q_bytes), format="mp3")
        combined += q_audio
        seg["think_start"] = len(combined) / 1000.0

        current_step += 1
        progress_bar.progress(current_step / total_steps)

        # 2. Think Time (Skipped if 0s for Podcast Mode)
        if len(think_silence) > 0:
            combined += think_silence
        seg["a_start"] = len(combined) / 1000.0

        # 3. Answer (Candidate Persona)
        a_text = f"Answer. {q['answer_text']}"
        a_bytes = generate_kokoro_speech(a_text, voice=a_voice, speed=speed)
        a_audio = AudioSegment.from_file(io.BytesIO(a_bytes), format="mp3")
        combined += a_audio
        seg["a_end"] = len(combined) / 1000.0
        segments.append(seg)

        current_step += 1
        progress_bar.progress(current_step / total_steps)

        # 4. Next Question Delay
        if idx < len(questions) and len(transition_silence) > 0:
            combined += transition_silence

    progress_bar.empty()
    out_buf = io.BytesIO()
    combined.export(out_buf, format="mp3")
    st.session_state["session_audio_bytes"] = out_buf.getvalue()
    st.session_state["audio_segments"] = segments
    # Clean up any legacy shared session file on disk
    if os.path.exists(AUDIO_OUTPUT_FILE):
        try:
            os.remove(AUDIO_OUTPUT_FILE)
        except OSError:
            pass
    return st.session_state["session_audio_bytes"]

# ---------------------------------------------------------
# Session State Initialization
# ---------------------------------------------------------
if "active_questions" not in st.session_state:
    st.session_state["active_questions"] = []
if "card_index" not in st.session_state:
    st.session_state["card_index"] = 0
if "active_playlist_name" not in st.session_state:
    st.session_state["active_playlist_name"] = ""
if "is_session_playing" not in st.session_state:
    st.session_state["is_session_playing"] = False
if "has_audio_session" not in st.session_state:
    st.session_state["has_audio_session"] = False

is_playing = st.session_state.get("is_session_playing", False)

# =========================================================
# GLOBAL HEADER (Left Sidebar) & CINEMATIC INTRO
# =========================================================
intro_audio_b64 = get_intro_audio_b64()

if is_playing:
    brand_markup = """
<div class="sb-brand sb-brand-locked" id="sonorius-brand-trigger">
  <span class="sb-logo"><span class="material-symbols-rounded" style="color: #9CA3AF; margin-right: 6px;">lock</span><em>S</em>onorius</span>
</div>
"""
else:
    brand_markup = """
<div class="sb-brand" id="sonorius-brand-trigger">
  <span class="sb-logo"><span class="material-symbols-rounded" style="color: #FF9560; margin-right: 6px;">graphic_eq</span><em>S</em>onorius</span>
</div>
"""

INTRO_OVERLAY_HTML = """
__BRAND_MARKUP__

<script>
/* =====================================================================
   Sonorius Intro — "Dot Ocean" engine, v2
   v2 adds: brand-orange "S", warm voice ripples, letter reflections,
            soft glow on bright dots, camera drift, fly-to-logo ending.
   ===================================================================== */
window.SONORIUS_CFG = {
  // Scene
  horizon: 0.56,      // horizon line (fraction of screen height)
  textY: 0.36,        // vertical centre of the words
  camY: 1.0,          // camera height above water (world units)
  zNear: 1.0, zFar: 16, zRatio: 1.045,  // depth range & row spacing (geometric)
  xStep: 0.075,       // base horizontal spacing (scaled by sqrt(z))
  // Look
  dotR: 2.0,          // nearest dot radius (px)
  depthSize: true,    // false = uniform dot size, depth only via brightness
  crestLight: 1.0,    // how much wave crests brighten
  waveAmp: 1.0, waveSpeed: 1.0,
  audioReact: 1.0,    // how strongly the voice disturbs the water
  drift: 1.0,         // slow forward glide over the water
  reflect: 1.0,       // strength of the letters' reflection on the water
  glow: 0.55,         // soft bloom on the brightest dots
  // Colour (monochrome + one brand accent)
  accent: [255, 149, 96],   // #FF9560 — same as the sidebar logo
  brandS: true,       // the "S" of Sonorius turns accent as the light sweeps over it
  warmVoice: true,    // voice ripples glow warm, cooling to white as they spread
  // Ending
  flyToLogo: true,    // "Sonorius" shrinks into the sidebar logo (falls back to sinking)
  // Text
  textStep: 5, textR: 1.5,
  // Timeline (ms) — synced to intro_audio.mp3 (voice "I am" ≈ 404–1131, "Sonorius" ≈ 1690–2880)
  T: { iamRise: 230, iamFall: 1230, sonRise: 1520, shimmer: 2450, sonFall: 3300, fadeOut: 3700, end: 4400 }
};

(function () {
  const INTRO_B64 = "__INTRO_AUDIO_B64__";
  const AUDIO_SRC = (INTRO_B64 && INTRO_B64.length > 100) ? "data:audio/mp3;base64," + INTRO_B64 : "";
  const IS_PRACTICE_PLAYING = "__IS_PRACTICE_PLAYING__" === "true";

  const WAVES = [ // directional swell components (world units)
    { dx: 0.96, dz: 0.28, k: 0.55, w: 0.55, a: 0.130, p: 0.0 },
    { dx: 0.30, dz: 0.95, k: 0.95, w: 0.85, a: 0.080, p: 1.7 },
    { dx: -0.6, dz: 0.80, k: 1.80, w: 1.35, a: 0.040, p: 4.1 },
    { dx: 0.70, dz: -0.71, k: 3.20, w: 2.10, a: 0.018, p: 2.3 }
  ];
  const WAVE_MAX = WAVES.reduce((s, w) => s + w.a, 0);

  const clamp01 = v => v < 0 ? 0 : v > 1 ? 1 : v;
  const easeOutExpo = p => p >= 1 ? 1 : 1 - Math.pow(2, -10 * p);
  const easeInCubic = p => p * p * p;
  const easeInOut = p => p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
  const easeInOutExpo = p => p <= 0 ? 0 : p >= 1 ? 1 :
    p < 0.5 ? Math.pow(2, 20 * p - 10) / 2 : (2 - Math.pow(2, -20 * p + 10)) / 2;

  window.triggerSonoriusIntro = function () {
    if (IS_PRACTICE_PLAYING || window.__sonorius_intro_active) return;
    window.__sonorius_intro_active = true;
    const C = window.SONORIUS_CFG;
    const T = C.T;
    const AC = C.accent;
    const reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

    const old = document.getElementById("sonorius-cinematic-overlay");
    if (old) old.remove();

    // ---------- Overlay (input lockdown) + separate frosted-glass layer ----------
    const overlay = document.createElement("div");
    overlay.id = "sonorius-cinematic-overlay";
    Object.assign(overlay.style, {
      position: "fixed", inset: "0", zIndex: "99999999", overflow: "hidden",
      opacity: "0", transition: "opacity 0.3s ease-out",
      pointerEvents: "all", userSelect: "none", cursor: "default"
    });
    ["click", "mousedown", "mouseup"].forEach(ev =>
      overlay.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); }));
    overlay.addEventListener("touchstart", e => e.stopPropagation(), { passive: false });

    const glass = document.createElement("div"); // fades on its own so the logo can be revealed
    Object.assign(glass.style, {
      position: "absolute", inset: "0",
      backgroundColor: "rgba(8, 9, 12, 0.82)",
      backdropFilter: "blur(22px)", webkitBackdropFilter: "blur(22px)",
      transition: "opacity 0.6s ease-out"
    });
    overlay.appendChild(glass);

    const canvas = document.createElement("canvas");
    Object.assign(canvas.style, { position: "absolute", inset: "0", width: "100%", height: "100%" });
    overlay.appendChild(canvas);

    // Low-res bloom layer, blurred by the GPU via CSS and screened on top
    const glowCanvas = document.createElement("canvas");
    Object.assign(glowCanvas.style, {
      position: "absolute", inset: "0", width: "100%", height: "100%",
      filter: "blur(7px)", mixBlendMode: "screen", pointerEvents: "none"
    });
    overlay.appendChild(glowCanvas);

    const skipBtn = document.createElement("button");
    skipBtn.innerText = "ESC TO SKIP";
    Object.assign(skipBtn.style, {
      position: "absolute", top: "20px", right: "24px", zIndex: "10",
      background: "transparent", border: "1px solid rgba(255,255,255,0.14)", color: "rgba(255,255,255,0.45)",
      fontFamily: "'DM Mono', ui-monospace, monospace", fontSize: "10px", letterSpacing: "0.12em",
      padding: "6px 12px", borderRadius: "999px", cursor: "pointer", transition: "all 0.25s ease"
    });
    skipBtn.onmouseenter = () => { skipBtn.style.color = "#fff"; skipBtn.style.borderColor = "rgba(255,255,255,0.4)"; };
    skipBtn.onmouseleave = () => { skipBtn.style.color = "rgba(255,255,255,0.45)"; skipBtn.style.borderColor = "rgba(255,255,255,0.14)"; };
    overlay.appendChild(skipBtn);

    document.body.appendChild(overlay);
    requestAnimationFrame(() => { overlay.style.opacity = "1"; });

    // ---------- Canvases ----------
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const W = window.innerWidth, H = window.innerHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const GS = 1 / 3; // glow layer scale
    glowCanvas.width = Math.ceil(W * GS); glowCanvas.height = Math.ceil(H * GS);
    const gctx = glowCanvas.getContext("2d");

    // ---------- Camera / projection ----------
    const horizonY = H * C.horizon;
    const f = (H - horizonY) * C.zNear / C.camY * 1.06;
    const VZ = 5.0; // depth of the "voice" source in the water

    // ---------- Ocean grid (rows far → near so near dots paint on top) ----------
    const ocean = [];
    let rowIdx = 0;
    for (let z = C.zNear; z <= C.zFar; z *= C.zRatio, rowIdx++) {
      const half = (W / 2) / f * z * 1.08;
      const step = C.xStep * Math.sqrt(z);
      const off = (rowIdx % 2) * step * 0.5;
      const n = Math.ceil(half / step) + 1;
      for (let i = -n; i <= n; i++) ocean.push({ x: i * step + off, z: z });
    }
    ocean.sort((a, b) => b.z - a.z);
    const zSpan = C.zFar - C.zNear;

    // ---------- Fly-to-logo target ----------
    let logoTarget = null;
    if (C.flyToLogo) {
      const el = document.getElementById("sonorius-brand-trigger") || document.querySelector(".sb-brand");
      const logo = el && (el.querySelector(".sb-logo") || el);
      if (logo) {
        const lr = logo.getBoundingClientRect();
        const em = logo.querySelector("em");
        const er = em ? em.getBoundingClientRect() : lr;
        const left = em ? er.left : lr.left;
        if (lr.width > 4 && lr.right > 0 && lr.bottom > 0 && lr.left < W && lr.top < H) {
          logoTarget = { left, right: lr.right, cy: er.top + er.height / 2 };
        }
      }
    }
    const flyMode = !!logoTarget;
    const END = flyMode ? T.sonFall + 1350 : T.end;

    // ---------- Ripples (deterministic schedule, seconds) ----------
    const ripples = [{ t0: (T.iamFall + 380) / 1000, a: 0.16, x: 0, z: VZ }];
    if (!flyMode) ripples.push({ t0: (T.sonFall + 380) / 1000, a: 0.20, x: 0, z: VZ });

    let level = 0;   // smoothed voice level 0..1
    let warmOut = 0; // side-output of heightAt: how "warm" this water point is
    let ampRamp = 0;

    function heightAt(x, z, t) {
      const sp = C.waveSpeed;
      const zw = z + t * 0.45 * C.drift; // water flows toward the camera → forward glide
      let h = 0;
      for (let i = 0; i < 4; i++) {
        const w = WAVES[i];
        h += w.a * Math.sin((x * w.dx + zw * w.dz) * w.k + t * w.w * sp + w.p);
      }
      h *= C.waveAmp * ampRamp;
      let warm = 0;
      const dx = x, dz = z - VZ;
      const d = Math.sqrt(dx * dx + dz * dz);
      if (level > 0.002) {
        const env = level * Math.exp(-d * 0.42);
        const osc = Math.cos(d * 2.6 - t * 7.5);
        h += env * 0.22 * C.audioReact * osc;
        warm += clamp01(env * 2.2) * (0.5 + 0.5 * osc);
      }
      for (let i = 0; i < ripples.length; i++) {
        const r = ripples[i];
        const age = t - r.t0;
        if (age <= 0 || age > 4) continue;
        const rd = Math.sqrt((x - r.x) * (x - r.x) + (z - r.z) * (z - r.z));
        const u = rd - age * 2.6;
        const env = Math.exp(-u * u * 1.6);
        h += r.a * env * Math.exp(-age * 0.8) * Math.cos(u * 4.5);
        warm += env * Math.exp(-age * 1.4) * 0.9;
      }
      warmOut = C.warmVoice ? clamp01(warm) : 0;
      return h;
    }

    // ---------- Typography → dot targets (snapped to a crisp grid) ----------
    function sampleText(text, fontSize, step) {
      const off = document.createElement("canvas");
      off.width = W; off.height = H;
      const o = off.getContext("2d");
      o.fillStyle = "#fff";
      o.font = "700 " + fontSize + "px 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
      const ls = fontSize * 0.02;
      if ("letterSpacing" in o) o.letterSpacing = ls + "px";
      o.textAlign = "center"; o.textBaseline = "middle";
      o.fillText(text, W / 2, H * C.textY);
      const total = o.measureText(text).width;
      const firstEnd = W / 2 - total / 2 + o.measureText(text[0]).width - ls * 0.5; // right edge of 1st letter
      const data = o.getImageData(0, 0, W, H).data;
      const pts = [];
      let minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
      for (let y = 0; y < H; y += step)
        for (let x = 0; x < W; x += step)
          if (data[(y * W + x) * 4 + 3] > 120) {
            pts.push({ x, y });
            if (x < minX) minX = x; if (x > maxX) maxX = x;
            if (y < minY) minY = y; if (y > maxY) maxY = y;
          }
      return { pts, minX, maxX, minY, maxY, firstEnd };
    }

    function buildWord(text, fontSize, riseAt, fallAt, fallMode, isSon) {
      const s = sampleText(text, fontSize, C.textStep);
      const span = Math.max(1, s.maxX - s.minX);
      const midY = (s.minY + s.maxY) / 2;
      let scale = 1;
      if (isSon && flyMode) scale = (logoTarget.right - logoTarget.left) / span;
      const dots = s.pts.map(p => {
        const zs = 2.4 + Math.random() * 3.2;          // where in the water the dot comes from
        const nx = (p.x - s.minX) / span;               // 0..1 left→right
        const fromCentre = Math.abs(p.x - W / 2) / (span / 2);
        const d = {
          tx: p.x, ty: p.y, nx,
          sx: (p.x - W / 2) * zs / f, sz: zs,           // world source position
          riseAt: riseAt + nx * 380 + Math.random() * 90,
          fallAt: fallAt + (fallMode === "centre" ? fromCentre * 260 : nx * 220) + Math.random() * 70,
          isS: isSon && p.x <= s.firstEnd,
          tw: Math.random() * Math.PI * 2
        };
        if (isSon && flyMode) {
          d.lx = logoTarget.left + (p.x - s.minX) * scale;
          d.ly = logoTarget.cy + (p.y - midY) * scale;
          d.flyAt = T.sonFall + nx * 300 + Math.random() * 60;
        }
        return d;
      });
      dots.minX = s.minX; dots.maxX = s.maxX;
      return dots;
    }

    const fsSon = Math.min(Math.max(W * 0.11, 72), 150);
    const fsIam = fsSon * 0.72;
    const wordIam = buildWord("I am", fsIam, T.iamRise, T.iamFall, "lr", false);
    const wordSon = buildWord("Sonorius", fsSon, T.sonRise, T.sonFall, "centre", true);
    const sonMinX = wordSon.minX, sonMaxX = wordSon.maxX;

    // ---------- Audio (+ analyser when allowed, envelope fallback otherwise) ----------
    let audio = null, analyser = null, buf = null, actx = null;
    try {
      if (!AUDIO_SRC) throw new Error("no intro audio");
      audio = new Audio(AUDIO_SRC);
      const canAnalyse = location.protocol !== "file:" || AUDIO_SRC.startsWith("data:");
      if (canAnalyse && (window.AudioContext || window.webkitAudioContext)) {
        actx = new (window.AudioContext || window.webkitAudioContext)();
        const src = actx.createMediaElementSource(audio);
        analyser = actx.createAnalyser();
        analyser.fftSize = 512;
        buf = new Uint8Array(analyser.fftSize);
        src.connect(analyser); analyser.connect(actx.destination);
        if (actx.state === "suspended") actx.resume();
      }
      audio.play().catch(e => console.warn("Intro audio blocked:", e));
    } catch (e) { console.warn("Audio init failed:", e); }

    function envelope(ms) { // fallback "voice" shape when analyser isn't available
      const win = (a, b) => (ms > a && ms < b) ? Math.pow(Math.sin(Math.PI * (ms - a) / (b - a)), 0.6) : 0;
      const e = Math.max(win(404, 1131), win(1690, 2880));
      return e * (0.55 + 0.45 * Math.abs(Math.sin(ms * 0.021) * Math.sin(ms * 0.0073 + 1)));
    }

    function readLevel(ms) {
      let raw;
      if (analyser) {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
        raw = Math.min(1, Math.sqrt(sum / buf.length) * 4.5);
      } else raw = envelope(ms);
      level += (raw - level) * (raw > level ? 0.35 : 0.08);
    }

    // ---------- Lifecycle ----------
    let closed = false, rafId = null, glassFaded = false;
    const t0 = performance.now();

    function closeIntro() {
      if (closed) return;
      closed = true;
      overlay.style.transition = "opacity 0.35s ease-out";
      overlay.style.opacity = "0";
      setTimeout(() => {
        if (rafId) cancelAnimationFrame(rafId);
        if (audio) try { audio.pause(); } catch (e) {}
        if (actx) try { actx.close(); } catch (e) {}
        window.removeEventListener("keydown", onKey);
        overlay.remove();
        window.__sonorius_intro_active = false;
      }, 360);
    }
    function onKey(e) { if (e.key === "Escape") closeIntro(); }
    window.addEventListener("keydown", onKey);
    skipBtn.addEventListener("click", e => { e.stopPropagation(); closeIntro(); });

    // ---------- Draw helpers ----------
    let curFill = "";
    function setFill(m) { // m = 0 → white, 1 → accent
      const s = m < 0.03 ? "#fff" :
        "rgb(" + (255 + (AC[0] - 255) * m | 0) + "," + (255 + (AC[1] - 255) * m | 0) + "," + (255 + (AC[2] - 255) * m | 0) + ")";
      if (s !== curFill) { ctx.fillStyle = s; curFill = s; }
      return s;
    }
    function dot(x, y, r, a, m) {
      if (a <= 0.004) return;
      setFill(m);
      ctx.globalAlpha = a > 1 ? 1 : a;
      if (r < 1.0) { ctx.fillRect(x - r, y - r, r * 2, r * 2); return; }
      ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();
    }
    function glowDot(x, y, r, a, m) { // bright sources only, into the low-res bloom layer
      if (a < 0.05) return;
      gctx.globalAlpha = a > 1 ? 1 : a;
      gctx.fillStyle = m < 0.03 ? "#fff" :
        "rgb(" + (255 + (AC[0] - 255) * m | 0) + "," + (255 + (AC[1] - 255) * m | 0) + "," + (255 + (AC[2] - 255) * m | 0) + ")";
      const s = Math.max(1, r * GS * 2.4);
      gctx.fillRect(x * GS - s / 2, y * GS - s / 2, s, s);
    }

    const sample = { x: 0, y: 0, r: 0, a: 0, m: 0 };
    function oceanSample(x, z, t) { // projected screen pos, radius, alpha, warmth of a water point
      const h = heightAt(x, z, t);
      const inv = f / z;
      const depth = (z - C.zNear) / zSpan;              // 0 near → 1 far
      const fog = Math.pow(1 - depth, 1.5);
      const crest = clamp01(h / (WAVE_MAX * Math.max(C.waveAmp, 0.3)) * 0.5 + 0.5);
      const light = (1 - C.crestLight * 0.75) + C.crestLight * 0.75 * crest * crest;
      sample.x = W / 2 + x * inv;
      sample.y = horizonY + (C.camY - h) * inv;
      sample.r = C.depthSize
        ? Math.max(0.45, C.dotR * Math.pow(C.zNear / z, 0.75) * (0.85 + 0.3 * crest))
        : C.dotR * 0.6;
      sample.m = warmOut;
      sample.a = fog * (light + warmOut * 0.35) * 0.85;
      return sample;
    }

    function drawWord(word, ms, t, isSon, oceanAlpha) {
      for (let i = 0; i < word.length; i++) {
        const d = word[i];
        const pr = clamp01((ms - d.riseAt) / 760);
        if (pr <= 0) continue;
        const flying = isSon && flyMode;
        const pf = flying ? 0 : clamp01((ms - d.fallAt) / 520);
        if (pf >= 1) continue;
        const src = oceanSample(d.sx, d.sz, t);
        const sX = src.x, sY = src.y, sR = src.r, sA = src.a * oceanAlpha, sM = src.m;
        // rise (expo-out) then fall back into the water (cubic-in)
        let k = easeOutExpo(pr);
        if (pf > 0) k *= 1 - easeInCubic(pf);
        let x = sX + (d.tx - sX) * k;
        let y = sY + (d.ty - sY) * k;
        // the light of the water passes through the letters
        let a = 0.82 + 0.12 * Math.sin(d.tx * 0.012 - t * 2.2 + d.tw * 0.15);
        let r = C.textR;
        let m = 0;
        if (isSon) { // one clean light sweep across "Sonorius"; the "S" keeps the brand colour
          const sp = (ms - T.shimmer) / 900;
          if (sp > 0) {
            const bx = sonMinX - 120 + (sonMaxX - sonMinX + 240) * easeInOut(Math.min(sp, 1));
            if (sp < 1) {
              const g = Math.exp(-Math.pow((d.tx - bx) / 55, 2));
              a += g * 0.35; r += g * 0.55;
            }
            if (d.isS && C.brandS) m = clamp01((bx - d.tx + 30) / 60);
          }
        }
        r = sR + (r - sR) * k;
        a = sA + (a - sA) * k;
        m = sM + (m - sM) * k;

        // fly into the sidebar logo
        if (flying) {
          const pfly = clamp01((ms - d.flyAt) / 900);
          if (pfly >= 1) continue;
          if (pfly > 0) {
            const e = easeInOutExpo(pfly);
            x += (d.lx - x) * e; y += (d.ly - y) * e;
            r *= 1 - 0.55 * e;
            a *= 1 - clamp01((pfly - 0.75) / 0.25);
          }
        }

        dot(x, y, r, a, m);
        if (C.glow > 0) glowDot(x, y, r, a * k, m);

        // wavy reflection on the water
        if (C.reflect > 0 && k > 0.05 && y < horizonY) {
          const ry = horizonY + (horizonY - y) * 0.55 + 4;
          const fade = 1 - (ry - horizonY) / (H * 0.28);
          if (fade > 0) {
            const rx = x + Math.sin(ry * 0.09 + t * 3.2) * 2.2 * (0.4 + (ry - horizonY) / 60);
            dot(rx, ry, r * 0.85, a * k * C.reflect * 0.26 * fade * oceanAlpha, m);
          }
        }
      }
    }

    // ---------- Frame ----------
    function frame(now) {
      if (closed) return;
      const ms = now - t0, t = ms / 1000;

      if (flyMode) {
        if (ms >= T.sonFall && !glassFaded) {
          glassFaded = true;
          glass.style.opacity = "0";
          skipBtn.style.opacity = "0";
        }
      } else if (ms >= T.fadeOut && overlay.style.opacity !== "0") {
        overlay.style.transition = "opacity " + ((T.end - T.fadeOut) / 1000) + "s ease-out";
        overlay.style.opacity = "0";
      }
      if (ms >= END) { closeIntro(); return; }

      readLevel(ms);
      ampRamp = reduceMotion ? 0.15 : easeInOut(clamp01(ms / 900));
      let oceanAlpha = clamp01(ms / 500);
      if (flyMode) oceanAlpha *= 1 - easeInOut(clamp01((ms - T.sonFall) / 650));

      ctx.clearRect(0, 0, W, H);
      gctx.clearRect(0, 0, glowCanvas.width, glowCanvas.height);
      curFill = "";

      // Ocean
      if (oceanAlpha > 0.003) {
        for (let i = 0; i < ocean.length; i++) {
          const o = ocean[i];
          const s = oceanSample(o.x, o.z, t);
          if (s.x < -4 || s.x > W + 4 || s.y > H + 4) continue;
          const a = s.a * oceanAlpha;
          dot(s.x, s.y, s.r, a, s.m);
          if (C.glow > 0 && (s.m > 0.25 || a > 0.7)) glowDot(s.x, s.y, s.r, a * 0.6, s.m);
        }
      }

      // Words
      drawWord(wordIam, ms, t, false, oceanAlpha);
      drawWord(wordSon, ms, t, true, oceanAlpha);

      ctx.globalAlpha = 1;
      glowCanvas.style.opacity = String(C.glow);
      rafId = requestAnimationFrame(frame);
    }
    rafId = requestAnimationFrame(frame);
  };

  // ---------- Trigger binding ----------
  function attachTrigger() {
    const el = document.getElementById("sonorius-brand-trigger") || document.querySelector(".sb-brand");
    if (!el || IS_PRACTICE_PLAYING || el.__sonorius_bound) return;
    el.__sonorius_bound = true;
    el.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); window.triggerSonoriusIntro(); });
  }
  attachTrigger();
  if (!window.__sonorius_obs) {
    window.__sonorius_obs = true;
    new MutationObserver(attachTrigger).observe(document.body, { childList: true, subtree: true });
  }
})();
</script>
"""

rendered_intro_html = (
    INTRO_OVERLAY_HTML
    .replace("__BRAND_MARKUP__", brand_markup)
    .replace("__INTRO_AUDIO_B64__", intro_audio_b64)
    .replace("__IS_PRACTICE_PLAYING__", "true" if is_playing else "false")
)

st.sidebar.html(
    rendered_intro_html,
    unsafe_allow_javascript=True
)

if is_playing:
    st.sidebar.html("""
    <div style="background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.35); border-radius: 6px; padding: 6px 10px; margin-bottom: 8px; text-align: center;">
      <span class="material-symbols-rounded" style="color: #F87171; font-size: 15px; vertical-align: -2px; margin-right: 4px;">lock</span>
      <span style="font-family: 'DM Mono', monospace; font-size: 0.74rem; color: #FCA5A5; font-weight: 600; letter-spacing: 0.04em;">
        PRACTICE RUNNING · FROZEN
      </span>
    </div>
    """)

route = st.sidebar.radio(
    "Navigation Menu",
    options=[ROUTE_PLAY, ROUTE_ADD, ROUTE_BUILD],
    index=0,
    disabled=is_playing,
    label_visibility="collapsed"
)

st.sidebar.markdown("<hr style='border-color: #374151; margin-top: 10px; margin-bottom: 12px;'>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PANEL
# ═══════════════════════════════════════════════════════════════════════════════

tts_endpoint_status = inspect_tts_endpoint(KOKORO_TTS_URL)
if is_playing:
    top_status_badge = '<span class="sb-live-dot" style="background: #EF4444;"></span>practice · running'
elif not tts_endpoint_status["ok"]:
    top_status_badge = '<span class="sb-live-dot" style="background: #EF4444;"></span>insecure · http · blocked'
elif tts_endpoint_status["mode"] == "remote_https":
    top_status_badge = '<span class="sb-live-dot"></span>remote · https · tts'
elif tts_endpoint_status["mode"] == "remote_http_override":
    top_status_badge = '<span class="sb-live-dot" style="background: #F59E0B;"></span>remote · http (override)'
else:
    top_status_badge = '<span class="sb-live-dot"></span>local · tts'

st.markdown(f"""
<div class="wn-topbar">
  <div class="wn-topbar-title">
    <strong><span style="color:#FF9560">S</span>onorius</strong>
    <span style="color:#6B7280;font-weight:400"> · </span>Local Voice-Assisted Q&A & Interview Prep · by · <span style="color:#FF9560">R</span>abbit
     <span style="color:#FF9560">K</span>night
  </div>
  <span class="wn-privacy-badge">{top_status_badge}</span>
</div>
""", unsafe_allow_html=True)
if not tts_endpoint_status["ok"]:
    st.error(f":material/gpp_bad: {tts_endpoint_status['reason']}")
elif tts_endpoint_status["mode"] == "remote_http_override":
    st.warning(f":material/warning: {tts_endpoint_status['reason']}")
st.divider()

# =========================================================
# ROUTE A: "Play Session" Selected
# =========================================================
if route == ROUTE_PLAY:
    st.sidebar.markdown("**Playback Mode**")
    play_mode = st.sidebar.radio(
        "Play Mode Selector",
        options=["Playlist", "Single Question"],
        horizontal=True,
        disabled=is_playing,
        label_visibility="collapsed"
    )

    if is_playing:
        st.sidebar.html(f"""
        <div style="background: #1A1D24; border: 1px solid #374151; border-radius: 6px; padding: 10px; margin-top: 6px; margin-bottom: 12px;">
            <div style="font-size: 0.75rem; color: #9CA3AF; text-transform: uppercase; font-family: 'DM Mono', monospace; margin-bottom: 4px;">Now Practicing</div>
            <div style="font-size: 0.95rem; font-weight: 600; color: #F3F4F6;">{html_lib.escape(st.session_state.get('active_playlist_name', 'Session'))}</div>
        </div>
        """)
        if st.sidebar.button("Stop Practice (Unfreeze)", icon=":material/stop_circle:", type="primary", width="stretch"):
            st.session_state["is_session_playing"] = False
            st.session_state["has_audio_session"] = False
            st.session_state.pop("session_audio_bytes", None)
            st.rerun()

    elif play_mode == "Playlist":
        playlists = fetch_all_playlists()
        playlist_options = {p["playlist_name"]: p["playlist_id"] for p in playlists}

        selected_playlist = st.sidebar.selectbox(
            "Select Playlist",
            options=list(playlist_options.keys()) if playlist_options else ["(No playlists created)"],
            label_visibility="visible"
        )

        if st.sidebar.button("Play Playlist", icon=":material/play_arrow:", type="primary", width="stretch"):
            if selected_playlist and selected_playlist in playlist_options:
                pl_id = playlist_options[selected_playlist]
                loaded_qs = fetch_playlist_questions(pl_id)

                if not loaded_qs:
                    st.sidebar.warning("Selected playlist contains no questions.")
                else:
                    with st.spinner("Synthesizing playlist with Kokoro..."):
                        try:
                            voice_q = st.session_state.get("pref_voice_q", DEFAULT_Q_VOICE)
                            voice_a = st.session_state.get("pref_voice_a", DEFAULT_A_VOICE)
                            voice_speed = st.session_state.get("pref_speed", 1.0)
                            think_sec = st.session_state.get("pref_think", 3)
                            delay_sec = st.session_state.get("pref_delay", 5)

                            stitch_playlist_audio(
                                loaded_qs,
                                think_time_sec=think_sec,
                                pause_between_sec=delay_sec,
                                q_voice=voice_q,
                                a_voice=voice_a,
                                speed=voice_speed
                            )
                            st.session_state["audio_play_id"] = st.session_state.get("audio_play_id", 0) + 1
                            st.session_state["active_questions"] = loaded_qs
                            st.session_state["card_index"] = 0
                            st.session_state["active_playlist_name"] = selected_playlist
                            st.session_state["has_audio_session"] = True
                            st.session_state["is_session_playing"] = True
                            st.sidebar.success("Playlist audio ready!")
                            st.rerun()
                        except requests.RequestException as e:
                            st.sidebar.error(f"Failed to connect to Kokoro TTS at {KOKORO_TTS_URL}: {e}")
                        except Exception as ex:
                            st.sidebar.error(f"Error generating audio: {ex}")
            else:
                st.sidebar.warning("Please create a playlist first.")

    else:
        # Single Question Mode
        all_qs = fetch_all_questions()
        if not all_qs:
            st.sidebar.warning("No questions in vault yet.")
        else:
            q_map = {f"#{q['question_id']} — {q['question_text'][:38]}...": q for q in all_qs}
            chosen_q_label = st.sidebar.selectbox("Select Question", options=list(q_map.keys()))
            chosen_q = q_map[chosen_q_label]

            if st.sidebar.button("Play Single Question", icon=":material/play_arrow:", type="primary", width="stretch"):
                with st.spinner("Synthesizing question with Kokoro..."):
                    try:
                        voice_q = st.session_state.get("pref_voice_q", DEFAULT_Q_VOICE)
                        voice_a = st.session_state.get("pref_voice_a", DEFAULT_A_VOICE)
                        voice_speed = st.session_state.get("pref_speed", 1.0)
                        think_sec = st.session_state.get("pref_think", 3)
                        delay_sec = st.session_state.get("pref_delay", 5)

                        stitch_playlist_audio(
                            [chosen_q],
                            think_time_sec=think_sec,
                            pause_between_sec=delay_sec,
                            q_voice=voice_q,
                            a_voice=voice_a,
                            speed=voice_speed
                        )
                        st.session_state["audio_play_id"] = st.session_state.get("audio_play_id", 0) + 1
                        st.session_state["active_questions"] = [chosen_q]
                        st.session_state["card_index"] = 0
                        st.session_state["active_playlist_name"] = f"Question #{chosen_q['question_id']}"
                        st.session_state["has_audio_session"] = True
                        st.session_state["is_session_playing"] = True
                        st.sidebar.success("Question audio ready!")
                        st.rerun()
                    except requests.RequestException as e:
                        st.sidebar.error(f"Failed to connect to Kokoro TTS at {KOKORO_TTS_URL}: {e}")
                    except Exception as ex:
                        st.sidebar.error(f"Error generating audio: {ex}")

    st.sidebar.markdown("<hr style='border-color: #374151; margin-top: 12px; margin-bottom: 12px;'>", unsafe_allow_html=True)

    with st.sidebar.expander(":material/tune: Preferences", expanded=False):
        voice_keys = list(VOICE_OPTIONS.keys())

        # Load persisted settings from database if not already in session_state
        if "pref_voice_q" not in st.session_state:
            saved_vq = get_app_setting("pref_voice_q", DEFAULT_Q_VOICE)
            st.session_state["pref_voice_q"] = saved_vq if saved_vq in voice_keys else DEFAULT_Q_VOICE

        if "pref_voice_a" not in st.session_state:
            saved_va = get_app_setting("pref_voice_a", DEFAULT_A_VOICE)
            st.session_state["pref_voice_a"] = saved_va if saved_va in voice_keys else DEFAULT_A_VOICE

        if "pref_speed" not in st.session_state:
            try:
                st.session_state["pref_speed"] = float(get_app_setting("pref_speed", "1.00"))
            except ValueError:
                st.session_state["pref_speed"] = 1.00

        think_options = [0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 45, 60, 90, 120]
        if "pref_think" not in st.session_state:
            try:
                saved_think = int(get_app_setting("pref_think", "3"))
                st.session_state["pref_think"] = saved_think if saved_think in think_options else 3
            except ValueError:
                st.session_state["pref_think"] = 3

        def _format_think_label(sec: int) -> str:
            if sec == 0:
                return "0s (Continuous — No pause)"
            if sec == 60:
                return "60s (1 min)"
            if sec == 90:
                return "90s (1.5 min)"
            if sec == 120:
                return "120s (2 min)"
            return f"{sec}s"

        delay_options = [1, 2, 3, 5, 8, 10]
        if "pref_delay" not in st.session_state:
            try:
                saved_delay = int(get_app_setting("pref_delay", "3"))
                st.session_state["pref_delay"] = saved_delay if saved_delay in delay_options else 3
            except ValueError:
                st.session_state["pref_delay"] = 3

        st.caption(":material/record_voice_over: Session Voices:")
        st.selectbox(
            "Interviewer Voice (Questions):",
            options=voice_keys,
            format_func=lambda x: VOICE_OPTIONS.get(x, x),
            key="pref_voice_q",
            disabled=is_playing,
            help="Voice that reads the question prompts."
        )
        st.selectbox(
            "Candidate Voice (Answers):",
            options=voice_keys,
            format_func=lambda x: VOICE_OPTIONS.get(x, x),
            key="pref_voice_a",
            disabled=is_playing,
            help="Voice that articulates your curated answers."
        )
        st.slider(
            "Speech Speed (Rate):",
            min_value=0.75,
            max_value=1.35,
            step=0.05,
            format="%.2fx",
            key="pref_speed",
            disabled=is_playing
        )
        st.selectbox(
            "Think Time (Pause between Q & A):",
            options=think_options,
            format_func=_format_think_label,
            key="pref_think",
            disabled=is_playing,
            help="Choose 0s for continuous playback, or 1-120s pause to articulate your answer out loud."
        )
        st.selectbox(
            "Next Question Delay (Pause between items):",
            options=delay_options,
            format_func=lambda x: f"{x}s",
            key="pref_delay",
            disabled=is_playing
        )

        # Save Preferences Button
        if st.button("Save Preferences", icon=":material/save:", type="primary", key="btn_save_prefs", disabled=is_playing, width="stretch"):
            save_app_settings({
                "pref_voice_q": st.session_state.get("pref_voice_q", DEFAULT_Q_VOICE),
                "pref_voice_a": st.session_state.get("pref_voice_a", DEFAULT_A_VOICE),
                "pref_speed": st.session_state.get("pref_speed", 1.00),
                "pref_think": st.session_state.get("pref_think", 3),
                "pref_delay": st.session_state.get("pref_delay", 3),
            })
            st.toast("Preferences saved to database!", icon=":material/check_circle:")
            st.success("Preferences saved! Your settings will persist across reloads.")

        st.markdown("<div style='margin-top: 6px;'></div>", unsafe_allow_html=True)
        if st.button(
            "Clear Audio Cache",
            icon=":material/delete_sweep:",
            key="btn_clear_audio_cache",
            disabled=is_playing,
            width="stretch",
            help="Delete all cached TTS audio clips from disk.",
        ):
            clear_audio_cache()
            st.toast("Audio cache cleared from disk.", icon=":material/check_circle:")

    # --- Right Pane (Main Area) ---
    col_hdr, col_badge = st.columns([3, 2])
    with col_hdr:
        st.subheader(":material/headphones: Practice Session")
    with col_badge:
        if is_playing:
            col_b1, col_b2 = st.columns([1, 1.2])
            with col_b1:
                st.markdown("<span class='badge-green'>ACTIVE SESSION</span>", unsafe_allow_html=True)
            with col_b2:
                if st.button("Stop Practice", icon=":material/stop_circle:", type="secondary", key="btn_stop_top", width="stretch"):
                    st.session_state["is_session_playing"] = False
                    st.session_state["has_audio_session"] = False
                    st.session_state.pop("session_audio_bytes", None)
                    st.rerun()
        else:
            st.markdown("<span class='badge-blue'>READY</span>", unsafe_allow_html=True)

    # Shown once after auto-unfreeze at the end of a session
    if "flash_session_complete" in st.session_state:
        done_name = st.session_state.pop("flash_session_complete")
        st.success(f"Session complete: **{done_name}**. Editing is unlocked.", icon=":material/check_circle:")
        st.toast("Practice finished, app unlocked", icon=":material/lock_open:")

    # Now Playing Header & Native Dark Audio Player (served from in-memory session_state)
    session_audio_bytes = st.session_state.get("session_audio_bytes")
    if session_audio_bytes and st.session_state.get("has_audio_session"):
        active_title = html_lib.escape(st.session_state.get("active_playlist_name", "Voice Session"))
        st.markdown(
            f"<div style='font-family: Inter, sans-serif; font-size: 0.95rem; font-weight: 600; color: #F3F4F6; margin-bottom: 8px; display: flex; align-items: center; gap: 8px;'>"
            f"<span class='material-symbols-rounded' style='color: #FF9560; font-size: 18px;'>graphic_eq</span>"
            f"Now Playing: <span style='color: #FF9560; font-weight: 700;'>{active_title}</span></div>",
            unsafe_allow_html=True
        )
        st.audio(session_audio_bytes, format="audio/mp3", autoplay=True)
        st.html("""
        <script>
        (function() {
            setTimeout(function() {
                var audio = document.querySelector('div[data-testid="stAudio"] audio');
                if (audio) {
                    audio.currentTime = 0;
                    audio.play().catch(function() {});
                }
            }, 100);
        })();
        </script>
        """, unsafe_allow_javascript=True)

    # Now Playing Display
    active_questions = st.session_state["active_questions"]
    if not active_questions and play_mode == "Playlist" and selected_playlist and selected_playlist in playlist_options:
        active_questions = fetch_playlist_questions(playlist_options[selected_playlist])

    if active_questions:
        card_idx = st.session_state["card_index"]
        if card_idx >= len(active_questions):
            card_idx = 0
            st.session_state["card_index"] = 0

        curr_card = active_questions[card_idx]

        # 1. Question Card
        st.html(f"""
            <div class='prep-card'>
                <div style='margin-bottom: 8px;'>
                    <span class='badge-orange'>QUESTION {card_idx + 1} OF {len(active_questions)}</span>
                    <span class='badge-blue'>ID #{curr_card['question_id']}</span>
                </div>
                <h3 style='margin: 0; font-size: 1.25rem; font-weight: 600; color: #F3F4F6; line-height: 1.5;'>
                    {html_lib.escape(curr_card['question_text'])}
                </h3>
            </div>
        """)

        # 2. Answer Card (with max-height scrollbox for long answers)
        st.html(f"""
            <div class='prep-card'>
                <div style='margin-bottom: 8px;'>
                    <span class='badge-green'>POSSIBLE ANSWER</span>
                </div>
                <div class='prep-card-answer'>
                    <p style='font-size: 1rem; line-height: 1.65; margin: 0; color: #D1D5DB; white-space: pre-wrap;'>
                        {html_lib.escape(curr_card['answer_text'])}
                    </p>
                </div>
            </div>
        """)

        # Navigation Bar: Previous / Next Card (Placed directly under Answer)
        c_prev, c_center, c_next = st.columns([1, 2, 1])
        with c_prev:
            if st.button("Previous", icon=":material/skip_previous:", disabled=(card_idx == 0), width="stretch", key="btn_nav_prev"):
                st.session_state["card_index"] -= 1
                st.rerun()

        with c_center:
            st.markdown(
                f"<div style='text-align: center; line-height: 2.2rem; font-family: \"DM Mono\", monospace; font-size: 0.85rem; color: #9CA3AF;'>"
                f"Card {card_idx + 1} of {len(active_questions)}</div>",
                unsafe_allow_html=True
            )

        with c_next:
            if st.button("Next", icon=":material/skip_next:", disabled=(card_idx >= len(active_questions) - 1), width="stretch", key="btn_nav_next"):
                st.session_state["card_index"] += 1
                st.rerun()

        # 3. Picture (Diagram / Visual Aid) - rendered below navigation
        if curr_card["image_path"] and os.path.exists(curr_card["image_path"]):
            st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
            with st.container(border=True):
                st.caption(f":material/image: Diagram for Question #{curr_card['question_id']}")
                st.image(curr_card["image_path"], width="stretch")
    else:
        st.info("No questions to display. Select a playlist or question on the left.")

# =========================================================
# ROUTE B: "Add New Q&A" Selected
# =========================================================
elif route == ROUTE_ADD:
    st.subheader(":material/add_circle: Add New Q&A")
    if is_playing:
        st.warning(":material/lock: All editing and additions are frozen while practice audio playback is active. Stop the active session to unfreeze.")
        st.stop()
    st.caption("Add a question, answer, and optional image or diagram.")

    # Persistent notification banner if questions were recently imported
    if "bulk_import_result" in st.session_state:
        res = st.session_state["bulk_import_result"]
        st.success(f":material/celebration: **CSV Import Successful!** Added **{res['count']} questions** to your vault ({res['diagrams']} with diagrams).")
        st.toast(f":material/celebration: Successfully imported {res['count']} questions into your vault!", icon="✅")

    with st.form("add_qa_form", clear_on_submit=True):
        new_q_text = st.text_area("Question Text:", placeholder="Type your question here...", height=100)
        new_a_text = st.text_area("Answer Text:", placeholder="Type a possible answer here...", height=150)
        uploaded_img = st.file_uploader("Upload Diagram or Visual Aid (Optional):", type=["png", "jpg", "jpeg", "webp"])

        submit_save = st.form_submit_button("Save Question", icon=":material/save:", type="primary")
        if submit_save:
            if not new_q_text.strip() or not new_a_text.strip():
                st.error("Both Question Text and Answer Text are required.")
            else:
                try:
                    saved_img_path = None
                    if uploaded_img is not None:
                        saved_img_path = save_verified_image(uploaded_img.getvalue(), uploaded_img.name)

                    new_qid = add_question(new_q_text, new_a_text, saved_img_path)
                    st.success(f"Successfully saved Question #{new_qid} to your vault.")
                except ValueError as ve:
                    st.error(f"Image validation failed: {ve}")

    st.markdown("<hr style='border-color: #374151; margin-top: 25px; margin-bottom: 20px;'>", unsafe_allow_html=True)

    # Bulk Import Questions via CSV
    bulk_result = st.session_state.get("bulk_import_result")
    expander_expanded = bool(bulk_result or st.session_state.get("bulk_expander_open", False))

    with st.expander(":material/upload_file: Bulk Import Questions via CSV", expanded=expander_expanded):
        if bulk_result:
            st.success(f":material/celebration: **Import Complete:** Successfully imported **{bulk_result['count']} questions** into your vault ({bulk_result['diagrams']} with diagrams)!")
            if bulk_result.get("missing"):
                st.warning(f":material/warning: {len(bulk_result['missing'])} diagram file(s) were not found and saved without images: {', '.join(bulk_result['missing'][:5])}")
            if st.button("Clear / Import Another CSV", icon=":material/refresh:", key="btn_clear_bulk"):
                st.session_state.pop("bulk_import_result", None)
                st.session_state["bulk_expander_open"] = False
                st.rerun()

        st.markdown("""
        <div style="background: #1A1D24; border: 1px solid #374151; border-radius: 8px; padding: 12px 16px; margin-bottom: 14px;">
            <div style="font-weight: 600; font-size: 0.92rem; color: #F3F4F6; margin-bottom: 6px;">
                CSV Format Requirements:
            </div>
            <div style="font-size: 0.84rem; color: #9CA3AF; line-height: 1.55;">
                To import successfully, your CSV must contain 3 columns:
                <br>• <strong>Question</strong>: The question or prompt text (Required).
                <br>• <strong>Answer</strong>: A possible scripted answer (Required).
                <br>• <strong>Image</strong>: Optional filename of an image or diagram (e.g. <code>Sonorius_image.jpeg</code>). Leave blank if none.
                <br><br>
                <em>Images:</em> Place image files in the local <code>bulk_import/images/</code> folder, or select them in the uploader below.
            </div>
        </div>
        """, unsafe_allow_html=True)

        sample_csv_path = os.path.join(os.path.dirname(__file__), "bulk_import", "questions_template.csv")
        sample_csv_data = ""
        if os.path.exists(sample_csv_path):
            with open(sample_csv_path, "r", encoding="utf-8") as f:
                sample_csv_data = f.read()
        else:
            sample_csv_data = 'Question,Answer,Image\n"Sample Question 1","Sample possible answer",\n'

        st.download_button(
            label="Download Template CSV",
            data=sample_csv_data,
            file_name="questions_template.csv",
            mime="text/csv",
            icon=":material/download:",
            help="Download a clean CSV pre-configured with the required columns and examples."
        )

        st.markdown("<div style='margin-top: 14px;'></div>", unsafe_allow_html=True)

        uploader_ver = st.session_state.get("bulk_uploader_version", 0)
        col_csv, col_imgs = st.columns([1, 1])
        with col_csv:
            csv_file = st.file_uploader(
                "1. Upload Filled Questions CSV:",
                type=["csv"],
                key=f"bulk_csv_upload_{uploader_ver}"
            )
        with col_imgs:
            attached_imgs = st.file_uploader(
                "2. Upload Referenced Diagrams (Optional):",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=True,
                key=f"bulk_img_upload_{uploader_ver}"
            )

        if csv_file is not None:
            try:
                try:
                    df_import = pd.read_csv(csv_file, encoding="utf-8")
                except UnicodeDecodeError:
                    csv_file.seek(0)
                    df_import = pd.read_csv(csv_file, encoding="latin1")

                col_map = {c.lower().strip(): c for c in df_import.columns}
                q_col = next((col_map[k] for k in ["question", "q", "question_text"] if k in col_map), None)
                a_col = next((col_map[k] for k in ["answer", "a", "answer_text"] if k in col_map), None)
                img_col = next((col_map[k] for k in ["image", "picture", "image_name", "image_path", "img"] if k in col_map), None)

                if not q_col or not a_col:
                    st.error(
                        f"❌ Missing required columns! Found columns: {list(df_import.columns)}. "
                        "Your CSV must have at least 'Question' and 'Answer' columns. "
                        "Please click 'Download Template CSV' above for the correct format."
                    )
                else:
                    df_valid = df_import.dropna(subset=[q_col, a_col], how="all").copy()
                    df_valid[q_col] = df_valid[q_col].fillna("").astype(str).str.strip()
                    df_valid[a_col] = df_valid[a_col].fillna("").astype(str).str.strip()
                    df_valid = df_valid[(df_valid[q_col] != "") & (df_valid[a_col] != "")]

                    if df_valid.empty:
                        st.warning(":material/warning: No valid questions found in this CSV. All rows appeared to have empty Question or Answer text.")
                    else:
                        existing_qs_norm = {
                            " ".join(q["question_text"].lower().split())
                            for q in fetch_all_questions()
                        }
                        dup_mask = df_valid[q_col].apply(lambda s: " ".join(str(s).lower().split()) in existing_qs_norm)
                        dup_count = int(dup_mask.sum())

                        st.info(
                            f"✓ Valid CSV: Detected **{len(df_valid)} questions** ready to import"
                            + (f" (**{dup_count}** already exist in your vault)." if dup_count else ".")
                        )
                        skip_duplicates = True
                        if dup_count > 0:
                            skip_duplicates = st.checkbox(
                                f"Skip {dup_count} duplicate question(s) already in vault",
                                value=True,
                                key=f"chk_skip_dup_{uploader_ver}",
                            )

                        df_to_import = df_valid[~dup_mask] if (dup_count > 0 and skip_duplicates) else df_valid

                        preview_data = []
                        for _, r in df_to_import.head(5).iterrows():
                            img_val = str(r[img_col]).strip() if img_col and pd.notna(r[img_col]) else "—"
                            if img_val.lower() == "nan":
                                img_val = "—"
                            preview_data.append({
                                "Question Preview": (r[q_col][:60] + "...") if len(r[q_col]) > 60 else r[q_col],
                                "Answer Preview": (r[a_col][:60] + "...") if len(r[a_col]) > 60 else r[a_col],
                                "Diagram": img_val
                            })
                        if df_to_import.empty:
                            st.warning("All questions in this CSV are already in your vault. Uncheck 'Skip duplicate questions' above if you want to import them anyway.")
                        else:
                            st.caption("Preview of first rows to be imported:")
                            preview_rows_html = "".join(
                                f"<tr style='border-bottom: 1px solid #374151;'>"
                                f"<td style='padding: 8px 12px; vertical-align: top; color: #F3F4F6;'>{html_lib.escape(item['Question Preview'])}</td>"
                                f"<td style='padding: 8px 12px; vertical-align: top; color: #D1D5DB;'>{html_lib.escape(item['Answer Preview'])}</td>"
                                f"<td style='padding: 8px 12px; vertical-align: top; color: #9CA3AF; font-family: monospace; white-space: nowrap;'>{html_lib.escape(item['Diagram'])}</td>"
                                f"</tr>"
                                for item in preview_data
                            )
                            st.markdown(
                                f"""<div style='overflow-x: auto; margin-bottom: 1rem; border: 1px solid #374151; border-radius: 6px;'>
                                    <table style='width: 100%; border-collapse: collapse; background: #1A1D24; font-size: 0.88rem; text-align: left;'>
                                        <thead>
                                            <tr style='background: #111827; border-bottom: 2px solid #374151;'>
                                                <th style='padding: 10px 12px; color: #9CA3AF; font-weight: 600;'>Question Preview</th>
                                                <th style='padding: 10px 12px; color: #9CA3AF; font-weight: 600;'>Answer Preview</th>
                                                <th style='padding: 10px 12px; color: #9CA3AF; font-weight: 600; width: 140px;'>Diagram</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {preview_rows_html}
                                        </tbody>
                                    </table>
                                </div>""",
                                unsafe_allow_html=True
                            )

                            if st.button("Import Questions to Vault", icon=":material/save:", type="primary", key="btn_confirm_bulk"):
                                uploaded_img_dict = {f.name.lower().strip(): f for f in attached_imgs} if attached_imgs else {}
                                local_bulk_img_dir = os.path.join(os.path.dirname(__file__), "bulk_import", "images")

                                diagrams_count = 0
                                missing_images = []
                                rows_for_db = []

                                for _, row in df_to_import.iterrows():
                                    q_text = row[q_col]
                                    a_text = row[a_col]
                                    saved_img_path = None

                                    if img_col and pd.notna(row[img_col]):
                                        raw_img_name = str(row[img_col]).strip()
                                        if raw_img_name and raw_img_name.lower() != "nan":
                                            clean_key = os.path.basename(raw_img_name).lower().strip()

                                            if clean_key in uploaded_img_dict:
                                                f_buf = uploaded_img_dict[clean_key]
                                                try:
                                                    saved_img_path = save_verified_image(f_buf.getvalue(), f_buf.name)
                                                    diagrams_count += 1
                                                except ValueError:
                                                    missing_images.append(f"{raw_img_name} (invalid image)")
                                            elif os.path.exists(os.path.join(local_bulk_img_dir, os.path.basename(raw_img_name))):
                                                src_path = os.path.join(local_bulk_img_dir, os.path.basename(raw_img_name))
                                                try:
                                                    with open(src_path, "rb") as f_src:
                                                        saved_img_path = save_verified_image(f_src.read(), raw_img_name)
                                                    diagrams_count += 1
                                                except ValueError:
                                                    missing_images.append(f"{raw_img_name} (invalid image)")
                                            else:
                                                missing_images.append(raw_img_name)

                                    rows_for_db.append((q_text, a_text, saved_img_path))

                                imported_count = bulk_add_questions(rows_for_db)
                                st.session_state["bulk_import_result"] = {
                                    "count": imported_count,
                                    "diagrams": diagrams_count,
                                    "missing": missing_images
                                }
                                st.session_state["bulk_expander_open"] = True
                                st.session_state["bulk_uploader_version"] = uploader_ver + 1
                                st.rerun()

            except Exception as ex:
                st.error(f":material/error: Failed to parse CSV: {ex}. Please check that the file is not corrupted or formatted incorrectly.")

# =========================================================
# ROUTE C: "Manage Playlists" Selected
# =========================================================
elif route == ROUTE_BUILD:
    st.subheader(":material/queue_music: Manage Playlists")
    if is_playing:
        st.warning(":material/lock: Question and playlist management are frozen while practice audio playback is active. Stop the active session to unfreeze.")
        st.stop()
    st.caption("Search, edit, or delete questions, and configure sequenced playlists.")

    # Persistent flash notification
    if "flash_manage_msg" in st.session_state:
        flash_m = st.session_state.pop("flash_manage_msg")
        st.toast(flash_m, icon=":material/check_circle:")

    all_qs = fetch_all_questions()
    q_labels = {
        q["question_id"]: f"#{q['question_id']} — {q['question_text'][:70]}{'…' if len(q['question_text']) > 70 else ''}"
        for q in all_qs
    }

    def playlist_order_editor(picked_ids, key_prefix):
        """Show the picked questions as a numbered list whose order can be edited.

        Returns the question IDs sorted by the 'Order' column (ties keep pick order).
        The editor resets whenever the selection changes, so numbers always match the list.
        """
        if not picked_ids:
            return []
        st.caption("Play order: questions play in the order you pick them. Change the **Order** numbers to rearrange.")
        order_df = pd.DataFrame({
            "Order": list(range(1, len(picked_ids) + 1)),
            "ID": picked_ids,
            "Question": [
                q_labels[qid].split(" — ", 1)[1] if (qid in q_labels and " — " in q_labels[qid]) else str(qid)
                for qid in picked_ids
            ],
        })
        sel_hash = hashlib.md5(",".join(map(str, picked_ids)).encode()).hexdigest()[:10]
        edited = st.data_editor(
            order_df,
            key=f"{key_prefix}_{sel_hash}",
            hide_index=True,
            width="stretch",
            disabled=["ID", "Question"],
            column_config={
                "Order": st.column_config.NumberColumn("Order", min_value=1, step=1, width="small"),
                "ID": st.column_config.NumberColumn("ID", format="#%d", width="small"),
                "Question": st.column_config.TextColumn("Question", width="large"),
            },
        )
        ranked = sorted(
            range(len(picked_ids)),
            key=lambda i: (edited["Order"].iloc[i] if pd.notna(edited["Order"].iloc[i]) else 1e9, i),
        )
        return [int(edited["ID"].iloc[i]) for i in ranked]

    def render_question_checklist_picker(all_qs, prefix, default_ids=None):
        """Render a consistent, scrollable checkbox list for picking questions (matching Delete Questions from Vault)."""
        init_key = f"{prefix}_init"
        if init_key not in st.session_state:
            st.session_state[init_key] = True
            initial = list(default_ids) if default_ids else []
            st.session_state[f"{prefix}_order_list"] = initial

        active_order = list(st.session_state.get(f"{prefix}_order_list", []))

        filter_val = st.text_input(
            "Filter questions to add:",
            placeholder="Type to filter questions by title or answer...",
            key=f"{prefix}_filter"
        )

        qs_to_display = all_qs
        if filter_val.strip():
            fv = filter_val.lower()
            qs_to_display = [
                q for q in all_qs
                if fv in q["question_text"].lower() or fv in q["answer_text"].lower()
            ]

        col_b1, col_b2, _ = st.columns([1, 1, 2])
        with col_b1:
            if st.button("Select All", icon=":material/select_all:", key=f"{prefix}_btn_sel_all", width="stretch"):
                for q in qs_to_display:
                    qid = q["question_id"]
                    if qid not in active_order:
                        active_order.append(qid)
                st.session_state[f"{prefix}_order_list"] = active_order
                st.rerun()
        with col_b2:
            if st.button("Deselect All", icon=":material/deselect:", key=f"{prefix}_btn_desel_all", width="stretch"):
                st.session_state[f"{prefix}_order_list"] = []
                st.rerun()

        # Track checkbox toggles and update order_list incrementally
        def _on_tick_toggle(qid, chk_key):
            is_checked = st.session_state.get(chk_key, False)
            order_copy = list(st.session_state.get(f"{prefix}_order_list", []))
            if is_checked and qid not in order_copy:
                order_copy.append(qid)
            elif not is_checked and qid in order_copy:
                order_copy.remove(qid)
            st.session_state[f"{prefix}_order_list"] = order_copy

        with st.container(height=260, border=True):
            if not qs_to_display:
                st.info("No questions match the filter.")
            else:
                for q in qs_to_display:
                    qid = q["question_id"]
                    chk_key = f"{prefix}_chk_{qid}"
                    has_diagram = " [Has Diagram]" if q["image_path"] else ""
                    st.checkbox(
                        f"#{qid} — {q['question_text']}{has_diagram}",
                        value=(qid in active_order),
                        key=chk_key,
                        on_change=_on_tick_toggle,
                        args=(qid, chk_key),
                        help=f"Answer: {q['answer_text'][:120]}..."
                    )

        active_order = list(st.session_state.get(f"{prefix}_order_list", []))

        if active_order:
            st.markdown(
                f"<div style='margin-top: 4px; margin-bottom: 8px; color: #2EC96A; font-family: \"DM Mono\", monospace; font-size: 0.82rem;'>"
                f"<strong>{len(active_order)}</strong> question{'s' if len(active_order) > 1 else ''} selected</div>",
                unsafe_allow_html=True
            )
        else:
            st.caption("No questions selected yet. Tick questions in the list above to include them.")

        return active_order

    # ═════════════════════════════════════════════════════════════
    # SECTION 1: MANAGE QUESTIONS
    # ═════════════════════════════════════════════════════════════
    st.markdown("### :material/quiz: Manage Questions")

    if all_qs:
        # Collapsible 1: Question Vault
        with st.expander(f":material/inventory_2: Question Vault ({len(all_qs)} questions)", expanded=False):
            search_kw = st.text_input("Search Questions:", placeholder="Filter by keyword...", key="vault_search_kw")

            table_data = []
            for q in all_qs:
                table_data.append({
                    "ID": q["question_id"],
                    "Question": q["question_text"],
                    "Answer": q["answer_text"],  # full text, used for search
                    "Answer Preview": (q["answer_text"][:55] + "...") if len(q["answer_text"]) > 55 else q["answer_text"],
                    "Has Image": "Yes" if q["image_path"] else "—"
                })
            df = pd.DataFrame(table_data)

            if search_kw.strip():
                kw = search_kw.strip()
                df = df[
                    df["Question"].str.contains(kw, case=False, na=False, regex=False)
                    | df["Answer"].str.contains(kw, case=False, na=False, regex=False)
                ]

            table_html = """
<style>
.vault-scroll-box {
    max-height: 290px;
    overflow-y: auto;
    overflow-x: auto;
    margin-top: 8px;
    margin-bottom: 12px;
    border-radius: 6px;
    border: 1px solid #374151;
    background: #1A1D24;
    scrollbar-width: thin;
    scrollbar-color: #4B5563 #1A1D24;
}
.vault-scroll-box::-webkit-scrollbar {
    width: 6px;
    height: 6px;
}
.vault-scroll-box::-webkit-scrollbar-track {
    background: #1A1D24;
}
.vault-scroll-box::-webkit-scrollbar-thumb {
    background: #4B5563;
    border-radius: 3px;
}
.vault-scroll-box::-webkit-scrollbar-thumb:hover {
    background: #6B7280;
}
.vault-table th {
    position: sticky;
    top: 0;
    z-index: 2;
    background: #262730;
    box-shadow: 0 1px 0 #374151;
}
</style>
<div class="vault-scroll-box">
<table class="vault-table" style="width: 100%; border-collapse: collapse; font-family: 'Inter', sans-serif; font-size: 0.88rem; text-align: left;">
<thead>
<tr style="background: #262730; color: #9CA3AF; font-family: 'DM Mono', monospace; font-size: 0.75rem; text-transform: uppercase;">
<th style="padding: 10px 12px; width: 70px;">ID</th>
<th style="padding: 10px 12px;">Question</th>
<th style="padding: 10px 12px;">Answer Preview</th>
<th style="padding: 10px 12px; width: 90px;">Diagram</th>
</tr>
</thead>
<tbody>"""
            if df.empty:
                table_html += """<tr><td colspan="4" style="padding: 20px 12px; text-align: center; color: #9CA3AF; font-size: 0.86rem;">No questions match your search filter.</td></tr>"""
            else:
                for _, row in df.iterrows():
                    table_html += f"""<tr style="border-bottom: 1px solid #262730; background: #1A1D24;">
<td style="padding: 8px 12px; font-family: 'DM Mono', monospace; color: #60A5FA; font-weight: 500;">#{row['ID']}</td>
<td style="padding: 8px 12px; color: #E5E7EB;">{html_lib.escape(row['Question'])}</td>
<td style="padding: 8px 12px; color: #9CA3AF;">{html_lib.escape(row['Answer Preview'])}</td>
<td style="padding: 8px 12px; color: #FBBF24;">{row['Has Image']}</td>
</tr>"""
            table_html += "</tbody></table></div>"
            st.html(table_html)

        # Action: Edit Question in Vault
        with st.expander(":material/edit: Edit an Existing Question", expanded=False):
            edit_q_map = {f"#{q['question_id']} — {q['question_text'][:55]}": q for q in all_qs}
            selected_edit_label = st.selectbox("Select question to edit:", options=list(edit_q_map.keys()), key="select_edit_q")
            target_q = edit_q_map[selected_edit_label]

            with st.form(key=f"form_edit_q_{target_q['question_id']}"):
                edited_q_text = st.text_area("Question Text:", value=target_q["question_text"], height=100)
                edited_a_text = st.text_area("Possible Answer Text:", value=target_q["answer_text"], height=150)

                remove_img = False
                if target_q["image_path"] and os.path.exists(target_q["image_path"]):
                    st.caption("Current Diagram / Visual Aid:")
                    st.image(target_q["image_path"], width=280)
                    remove_img = st.checkbox("Remove current diagram from this question", value=False)

                new_upload = st.file_uploader("Replace / Upload New Diagram (Optional):", type=["png", "jpg", "jpeg", "webp"], key=f"edit_upload_{target_q['question_id']}")

                save_edit_btn = st.form_submit_button("Save Question Changes", icon=":material/save:", type="primary")
                if save_edit_btn:
                    if not edited_q_text.strip() or not edited_a_text.strip():
                        st.error("Both Question and Answer text are required.")
                    else:
                        try:
                            new_img_path = None
                            if new_upload is not None:
                                new_img_path = save_verified_image(new_upload.getvalue(), new_upload.name)

                            update_question(
                                question_id=target_q["question_id"],
                                question_text=edited_q_text,
                                answer_text=edited_a_text,
                                new_image_path=new_img_path,
                                remove_image=remove_img
                            )
                            st.session_state["flash_manage_msg"] = f"Successfully updated Question #{target_q['question_id']}!"
                            st.rerun()
                        except ValueError as ve:
                            st.error(f"Image validation failed: {ve}")

        # Action: Delete Questions from Vault (Batch / Multi-select with scroll)
        with st.expander(":material/delete: Delete Questions from Vault", expanded=False):
            st.caption("Tick one or more questions to permanently remove them from your database and all playlists.")

            del_filter = st.text_input("Filter questions to delete:", placeholder="Type to filter...", key="del_filter_input")

            qs_to_display = all_qs
            if del_filter.strip():
                qs_to_display = [
                    q for q in all_qs 
                    if del_filter.lower() in q["question_text"].lower() or del_filter.lower() in q["answer_text"].lower()
                ]

            col_btn1, col_btn2, _ = st.columns([1, 1, 2])
            with col_btn1:
                if st.button("Select All", icon=":material/select_all:", key="btn_sel_all_del", width="stretch"):
                    for q in qs_to_display:
                        st.session_state[f"del_tick_{q['question_id']}"] = True
                    st.rerun()
            with col_btn2:
                if st.button("Deselect All", icon=":material/deselect:", key="btn_desel_all_del", width="stretch"):
                    for q in all_qs:
                        st.session_state[f"del_tick_{q['question_id']}"] = False
                    st.rerun()

            with st.container(height=260, border=True):
                if not qs_to_display:
                    st.info("No questions match the filter.")
                else:
                    for q in qs_to_display:
                        qid = q["question_id"]
                        chk_key = f"del_tick_{qid}"
                        has_diagram = " [Has Diagram]" if q["image_path"] else ""
                        st.checkbox(
                            f"#{qid} — {q['question_text']}{has_diagram}",
                            key=chk_key,
                            help=f"Answer: {q['answer_text'][:120]}..."
                        )

            # Collect all checked questions
            ticked_ids = [q["question_id"] for q in all_qs if st.session_state.get(f"del_tick_{q['question_id']}", False)]

            c_del_btn, c_del_count = st.columns([2, 1])
            with c_del_btn:
                if ticked_ids:
                    btn_label = f"Permanently Delete {len(ticked_ids)} Question{'s' if len(ticked_ids) > 1 else ''}"
                    if st.button(btn_label, icon=":material/delete_forever:", type="primary", key="btn_confirm_batch_del"):
                        delete_questions(ticked_ids)
                        for k in list(st.session_state.keys()):
                            if k.startswith(("del_tick_", "create_pl_tick_", "edit_pl_tick_")):
                                st.session_state.pop(k, None)
                        st.session_state.pop("create_pl_order_list", None)
                        st.session_state.pop("edit_pl_order_list", None)
                        st.session_state.pop("edit_pl_initialized", None)
                        st.session_state["flash_manage_msg"] = f"Successfully deleted {len(ticked_ids)} question(s), compacted IDs (1..N), and updated all playlists."
                        st.rerun()
                else:
                    st.button("Select questions above to delete", icon=":material/delete:", disabled=True, key="btn_disabled_del")
            with c_del_count:
                if ticked_ids:
                    st.markdown(
                        f"<div style='line-height: 2.4rem; color: #EF4444; font-family: \"DM Mono\", monospace; font-size: 0.82rem;'>"
                        f"<strong>{len(ticked_ids)}</strong> selected</div>",
                        unsafe_allow_html=True
                    )
    else:
        st.info("No questions in your database yet. Go to 'Add New Q&A' to create some.")

    st.markdown("<hr style='border-color: #374151; margin-top: 25px; margin-bottom: 20px;'>", unsafe_allow_html=True)

    # ═════════════════════════════════════════════════════════════
    # SECTION 2: MANAGE PLAYLISTS
    # ═════════════════════════════════════════════════════════════
    st.markdown("### :material/playlist_play: Manage Playlists")

    current_pls = fetch_all_playlists()

    # 2. Collapsible: Create New Playlist
    with st.expander(":material/playlist_add: Create New Playlist", expanded=False):
        if not all_qs:
            st.caption("Add some questions to the vault first, then build a playlist from them.")
        else:
            create_ver = st.session_state.get("pl_create_ver", 0)
            pl_name_input = st.text_input(
                "Playlist Name:",
                placeholder="e.g., Exam Review, Presentation Prep, Study Deck",
                key=f"pl_new_name_{create_ver}",
            )
            st.caption("Choose questions for this playlist:")
            picked_new = render_question_checklist_picker(all_qs, f"pl_new_{create_ver}")
            ordered_new = playlist_order_editor(picked_new, f"pl_new_order_{create_ver}")

            if st.button("Save Playlist", icon=":material/save:", type="primary", key="btn_save_new_pl"):
                if not pl_name_input.strip():
                    st.error("Please enter a playlist name.")
                elif not ordered_new:
                    st.error("Please pick at least one question.")
                else:
                    try:
                        create_playlist(pl_name_input, ordered_new)
                        st.session_state["pl_create_ver"] = create_ver + 1  # resets the builder widgets
                        st.session_state["flash_manage_msg"] = f"Playlist '{pl_name_input.strip()}' created with {len(ordered_new)} questions."
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error(f"A playlist named '{pl_name_input.strip()}' already exists.")

    # 3. Collapsible: Edit an Existing Playlist
    with st.expander(":material/edit: Edit an Existing Playlist", expanded=False):
        if not current_pls:
            st.caption("No playlists created yet. Create a playlist above first.")
        else:
            pl_edit_map = {pl["playlist_name"]: pl["playlist_id"] for pl in current_pls}
            chosen_edit_name = st.selectbox("Select playlist to edit:", options=list(pl_edit_map.keys()), key="select_edit_playlist")
            chosen_edit_id = pl_edit_map[chosen_edit_name]

            curr_items = fetch_playlist_questions(chosen_edit_id)
            current_ids = []
            for it in curr_items:
                if it["question_id"] not in current_ids and it["question_id"] in q_labels:
                    current_ids.append(it["question_id"])

            # Detect if user switched to a different playlist in the dropdown
            last_sel_id = st.session_state.get("last_selected_edit_pl_id")
            if last_sel_id != chosen_edit_id:
                st.session_state["last_selected_edit_pl_id"] = chosen_edit_id
                st.session_state["pl_edit_ver"] = st.session_state.get("pl_edit_ver", 0) + 1

            edit_ver = st.session_state.get("pl_edit_ver", 0)
            edit_key = f"{chosen_edit_id}_{edit_ver}"
            new_pl_name = st.text_input("Playlist Name:", value=chosen_edit_name, key=f"pl_edit_name_{edit_key}")

            # Badge summary of current playlist contents
            items_badge = ", ".join([f"Q#{it['question_id']}" for it in curr_items]) if curr_items else "Empty"
            st.markdown(
                f"<div style='background: #1A1D24; border: 1px solid #374151; border-radius: 6px; padding: 8px 12px; margin-bottom: 10px;'>"
                f"<span style='color: #9CA3AF; font-size: 0.82rem;'>Currently in this playlist: </span>"
                f"<strong style='color: #F3F4F6;'>{len(curr_items)} questions</strong> "
                f"(<span style='font-family: \"DM Mono\", monospace; color: #FBBF24;'>{items_badge}</span>)</div>",
                unsafe_allow_html=True
            )

            st.caption("Add or remove questions (checked questions are currently in this playlist):")
            picked_edit = render_question_checklist_picker(all_qs, f"pl_edit_{edit_key}", default_ids=current_ids)
            ordered_edit = playlist_order_editor(picked_edit, f"pl_edit_order_{edit_key}")

            if st.button("Update Playlist", icon=":material/save:", type="primary", key=f"btn_update_pl_{edit_key}"):
                if not new_pl_name.strip():
                    st.error("Playlist name cannot be empty.")
                elif not ordered_edit:
                    st.error("Pick at least one question.")
                else:
                    try:
                        update_playlist(chosen_edit_id, new_pl_name, ordered_edit)
                        st.session_state["pl_edit_ver"] = edit_ver + 1
                        st.session_state["flash_manage_msg"] = f"Playlist '{new_pl_name.strip()}' updated successfully!"
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error(f"Another playlist named '{new_pl_name.strip()}' already exists.")

    # 4. Collapsible: Delete Existing Playlists (Batch / Checkboxes matching Delete Questions)
    with st.expander(":material/delete: Delete Existing Playlists", expanded=False):
        if not current_pls:
            st.caption("No playlists created yet.")
        else:
            st.caption("Tick one or more playlists to permanently delete them. Questions in the vault will not be deleted.")

            pl_del_filter = st.text_input("Filter playlists to delete:", placeholder="Type to filter playlists...", key="del_pl_filter_input")

            pls_to_display = current_pls
            if pl_del_filter.strip():
                pdf = pl_del_filter.lower()
                pls_to_display = [pl for pl in current_pls if pdf in pl["playlist_name"].lower()]

            col_pl_btn1, col_pl_btn2, _ = st.columns([1, 1, 2])
            with col_pl_btn1:
                if st.button("Select All", icon=":material/select_all:", key="btn_sel_all_del_pl", width="stretch"):
                    for pl in pls_to_display:
                        st.session_state[f"del_pl_tick_{pl['playlist_id']}"] = True
                    st.rerun()
            with col_pl_btn2:
                if st.button("Deselect All", icon=":material/deselect:", key="btn_desel_all_del_pl", width="stretch"):
                    for pl in current_pls:
                        st.session_state[f"del_pl_tick_{pl['playlist_id']}"] = False
                    st.rerun()

            with st.container(height=240, border=True):
                if not pls_to_display:
                    st.info("No playlists match the filter.")
                else:
                    for pl in pls_to_display:
                        pl_id = pl["playlist_id"]
                        chk_key = f"del_pl_tick_{pl_id}"
                        items = fetch_playlist_questions(pl_id)
                        items_str = ", ".join([f"Q{it['question_id']}" for it in items])
                        item_label = f" ({len(items)} questions: {items_str})" if items else " (0 questions)"
                        st.checkbox(
                            f"{pl['playlist_name']}{item_label}",
                            key=chk_key,
                            help=f"Playlist ID #{pl_id}"
                        )

            # Collect all checked playlists
            ticked_pl_ids = [pl["playlist_id"] for pl in current_pls if st.session_state.get(f"del_pl_tick_{pl['playlist_id']}", False)]

            c_del_pl_btn, c_del_pl_count = st.columns([2, 1])
            with c_del_pl_btn:
                if ticked_pl_ids:
                    btn_label = f"Permanently Delete {len(ticked_pl_ids)} Playlist{'s' if len(ticked_pl_ids) > 1 else ''}"
                    if st.button(btn_label, icon=":material/delete_forever:", type="primary", key="btn_confirm_batch_del_pl"):
                        for pl_id in ticked_pl_ids:
                            delete_playlist(pl_id)
                            st.session_state.pop(f"del_pl_tick_{pl_id}", None)
                        st.session_state["flash_manage_msg"] = f"Successfully deleted {len(ticked_pl_ids)} playlist(s)."
                        st.rerun()
                else:
                    st.button("Select playlists above to delete", icon=":material/delete:", disabled=True, key="btn_disabled_del_pl")
            with c_del_pl_count:
                if ticked_pl_ids:
                    st.markdown(
                        f"<div style='line-height: 2.4rem; color: #EF4444; font-family: \"DM Mono\", monospace; font-size: 0.82rem;'>"
                        f"<strong>{len(ticked_pl_ids)}</strong> selected</div>",
                        unsafe_allow_html=True
                    )
