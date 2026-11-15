#!/usr/bin/env python3
"""
Automated Test Suite for Sonorius (Local Voice-Assisted Q&A & Interview Prep).

Covers:
1. Database Initialization, Seeding & File Permissions (0600)
2. Question Vault CRUD (Add, Bulk Add, Edit, Delete, Image Cascade Cleanup)
3. Playlist CRUD (Create, Update, Delete, Ordering, Cascade on Question Delete)
4. Persistent User Preferences (App_Settings table get/save/overwrite)
5. Input Sanitization & CSV/DDE Injection Guards
6. Image Verification, Magic-Byte Checks, Re-encoding & Polyglot Stripping
7. Network & Transport Security Guards (Loopback vs LAN/Remote HTTP/HTTPS, SSRF Link-Local Block)
8. TTS Audio Caching (SHA-256 cache hits/misses, cache invalidation) & Playlist Audio Stitching
9. Headless Streamlit UI Smoke Tests across all navigation routes
"""

import hashlib
import io
import os
import shutil
import sqlite3
import stat
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

# Ensure ffmpeg/ffprobe are on PATH before importing pydub (matches app.py behavior)
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    import static_ffmpeg
    static_ffmpeg.add_paths()

import pandas as pd  # Pre-import C-extensions (numpy/pandas) so sys.modules never reloads them
from PIL import Image
from pydub import AudioSegment


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_FILE = os.path.join(APP_DIR, "app.py")
_INITIAL_BOOT_TEMP = tempfile.mkdtemp(prefix="sonorius_boot_")


def _build_core_module() -> types.ModuleType:
    """
    Load the core functions and constants from app.py (lines 1 up to Session State UI execution)
    once per test process, using an isolated temporary directory so module load never touches
    the live database or cache.
    """
    with open(APP_FILE, "r", encoding="utf-8") as f:
        source = f.read()

    cutoff_marker = "# ---------------------------------------------------------\n# Session State Initialization"
    idx = source.find(cutoff_marker)
    if idx == -1:
        raise RuntimeError("Could not locate Session State Initialization marker in app.py")
    core_source = source[:idx]

    # Redirect initial module-level paths into the boot temp directory before exec
    core_source = core_source.replace(
        'DB_FILE = "interview_data.db"',
        f'DB_FILE = {os.path.join(_INITIAL_BOOT_TEMP, "boot.db")!r}',
    ).replace(
        'IMAGES_DIR = "images"',
        f'IMAGES_DIR = {os.path.join(_INITIAL_BOOT_TEMP, "images")!r}',
    )

    module = types.ModuleType("sonorius_core")
    module.__file__ = APP_FILE

    mock_st = MagicMock()
    mock_st.session_state = {}
    mock_st.progress.return_value = MagicMock()

    with patch.dict("sys.modules", {"streamlit": mock_st, "streamlit.components.v1": MagicMock()}):
        exec(compile(core_source, APP_FILE, "exec"), module.__dict__)

    shutil.rmtree(_INITIAL_BOOT_TEMP, ignore_errors=True)
    return module


_CORE_MODULE = _build_core_module()


def configure_isolated_core(temp_dir: str) -> types.ModuleType:
    """Re-bind the loaded core module's file paths and state to a fresh per-test directory."""
    core = _CORE_MODULE
    core.DB_FILE = os.path.join(temp_dir, "test_interview_data.db")
    core.IMAGES_DIR = os.path.join(temp_dir, "images")
    core.AUDIO_CACHE_DIR = os.path.join(temp_dir, "audio_cache")
    core.AUDIO_OUTPUT_FILE = os.path.join(temp_dir, "current_session.mp3")
    core.ALLOW_INSECURE_HTTP_TTS = False
    core.ALLOW_LAN_ACCESS = False
    core.st.session_state = {}
    core.ensure_private_dir(core.IMAGES_DIR)
    core.init_db()
    return core


def make_test_image_bytes(fmt: str = "PNG", size=(32, 32), color=(255, 149, 96), mode="RGB") -> bytes:
    """Generate a valid in-memory image in the requested format."""
    buf = io.BytesIO()
    img = Image.new(mode, size, color)
    img.save(buf, format=fmt)
    return buf.getvalue()


def make_test_mp3_bytes(duration_ms: int = 120) -> bytes:
    """Generate a tiny valid MP3 byte stream for mocking TTS responses."""
    buf = io.BytesIO()
    AudioSegment.silent(duration=duration_ms).export(buf, format="mp3")
    return buf.getvalue()


class SonoriusCoreTestCase(unittest.TestCase):
    """Base test case that sets up a fresh isolated workspace and database per test."""

    def setUp(self):
        self.orig_cwd = os.getcwd()
        self.temp_dir = tempfile.mkdtemp(prefix="sonorius_test_")
        self.core = configure_isolated_core(self.temp_dir)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.temp_dir, ignore_errors=True)


# =====================================================================
# 1. Database Initialization & File Permissions
# =====================================================================
class TestDatabaseInitAndPermissions(SonoriusCoreTestCase):

    def test_tables_created_clean_and_empty(self):
        """Fresh DB should have all 4 tables and start with 0 questions and 0 playlists."""
        questions = self.core.fetch_all_questions()
        playlists = self.core.fetch_all_playlists()

        self.assertEqual(len(questions), 0)
        self.assertEqual(len(playlists), 0)

    def test_db_and_dirs_have_private_permissions(self):
        """Database file should be 0600 and images directory should be 0700."""
        db_mode = stat.S_IMODE(os.stat(self.core.DB_FILE).st_mode)
        img_dir_mode = stat.S_IMODE(os.stat(self.core.IMAGES_DIR).st_mode)
        self.assertEqual(db_mode, 0o600)
        self.assertEqual(img_dir_mode, 0o700)

    def test_reinit_is_idempotent(self):
        """Calling init_db() again on an existing DB must preserve existing rows without side effects."""
        self.core.add_question("Sample Q", "Sample A")
        self.core.init_db()
        self.assertEqual(len(self.core.fetch_all_questions()), 1)

    def test_deleting_all_questions_does_not_reseed_samples_on_rerun(self):
        """Deleting all questions and re-running init_db() must leave the vault empty (0 questions)."""
        self.core.add_question("Temp Q", "Temp A")
        self.core.delete_questions([q["question_id"] for q in self.core.fetch_all_questions()])
        self.assertEqual(len(self.core.fetch_all_questions()), 0)

        # Simulate Streamlit page rerun calling init_db()
        self.core.init_db()
        self.assertEqual(len(self.core.fetch_all_questions()), 0)


# =====================================================================
# 2. Question Vault CRUD & Image Cleanup
# =====================================================================
class TestQuestionCRUD(SonoriusCoreTestCase):

    def test_add_and_fetch_question(self):
        qid = self.core.add_question("What is TCP?", "Transmission Control Protocol.")
        questions = self.core.fetch_all_questions()
        self.assertEqual(len(questions), 1)
        added = [q for q in questions if q["question_id"] == qid][0]
        self.assertEqual(added["question_text"], "What is TCP?")
        self.assertEqual(added["answer_text"], "Transmission Control Protocol.")
        self.assertIsNone(added["image_path"])

    def test_bulk_add_questions_atomic(self):
        rows = [
            ("Question A", "Answer A", None),
            ("Question B", "Answer B", None),
            ("Question C", "Answer C", None),
        ]
        inserted = self.core.bulk_add_questions(rows)
        self.assertEqual(inserted, 3)
        self.assertEqual(len(self.core.fetch_all_questions()), 3)
        self.assertEqual(self.core.bulk_add_questions([]), 0)

    def test_update_question_text_and_image_replacement(self):
        img1 = self.core.save_verified_image(make_test_image_bytes("PNG"), "diag1.png")
        qid = self.core.add_question("Initial Q", "Initial A", image_path=img1)
        self.assertTrue(os.path.exists(img1))

        # Replace diagram with a new image -> old image file must be deleted from disk
        img2 = self.core.save_verified_image(make_test_image_bytes("PNG"), "diag2.png")
        self.core.update_question(qid, "Updated Q", "Updated A", new_image_path=img2, remove_image=False)
        self.assertFalse(os.path.exists(img1))
        self.assertTrue(os.path.exists(img2))

        row = [q for q in self.core.fetch_all_questions() if q["question_id"] == qid][0]
        self.assertEqual(row["question_text"], "Updated Q")
        self.assertEqual(row["answer_text"], "Updated A")
        self.assertEqual(row["image_path"], img2)

        # Remove diagram without uploading a new one -> image file deleted and DB column set to None
        self.core.update_question(qid, "Updated Q", "Updated A", new_image_path=None, remove_image=True)
        self.assertFalse(os.path.exists(img2))
        row_after = [q for q in self.core.fetch_all_questions() if q["question_id"] == qid][0]
        self.assertIsNone(row_after["image_path"])

    def test_delete_question_removes_image_and_playlist_references(self):
        img = self.core.save_verified_image(make_test_image_bytes("PNG"), "to_delete.png")
        qid = self.core.add_question("Delete Me", "Answer", image_path=img)
        pl_id = self.core.create_playlist("Temp Playlist", [qid])

        self.assertTrue(os.path.exists(img))
        self.assertEqual(len(self.core.fetch_playlist_questions(pl_id)), 1)

        self.core.delete_question(qid)

        self.assertFalse(os.path.exists(img))
        self.assertEqual(len(self.core.fetch_playlist_questions(pl_id)), 0)
        remaining_ids = [q["question_id"] for q in self.core.fetch_all_questions()]
        self.assertNotIn(qid, remaining_ids)

    def test_compacts_question_ids_and_cascades_playlists_on_delete(self):
        """Deleting questions should compact remaining IDs to 1..N, update playlists, and start new inserts at N+1 (or 1 when empty)."""
        # Start by clearing the 2 seeded questions -> vault empty
        self.core.delete_questions([1, 2])
        self.assertEqual(len(self.core.fetch_all_questions()), 0)

        # Bulk-import 5 questions -> must be numbered #1..#5
        self.core.bulk_add_questions([
            (f"Question {i}", f"Answer {i}", None) for i in range(1, 6)
        ])
        qs = self.core.fetch_all_questions()
        self.assertEqual([q["question_id"] for q in qs], [1, 2, 3, 4, 5])

        # Create a playlist referencing [#5, #3, #1]
        pl_id = self.core.create_playlist("Compact Test Deck", [5, 3, 1])

        # Delete #2 and #4 -> remaining questions ("Question 1", "Question 3", "Question 5") must become #1, #2, #3
        self.core.delete_questions([2, 4])
        qs_after = self.core.fetch_all_questions()
        self.assertEqual([q["question_id"] for q in qs_after], [1, 2, 3])
        self.assertEqual([q["question_text"] for q in qs_after], ["Question 1", "Question 3", "Question 5"])

        # Playlist that pointed to old [#5, #3, #1] must now point to compacted [#3, #2, #1]
        pl_items = self.core.fetch_playlist_questions(pl_id)
        self.assertEqual([item["question_id"] for item in pl_items], [3, 2, 1])
        self.assertEqual([item["play_order"] for item in pl_items], [1, 2, 3])

        # Adding a new question now must get #4
        next_id = self.core.add_question("Question 6", "Answer 6")
        self.assertEqual(next_id, 4)


# =====================================================================
# 3. Playlist CRUD & Ordering
# =====================================================================
class TestPlaylistCRUD(SonoriusCoreTestCase):

    def test_create_update_and_delete_playlist_order(self):
        q1 = self.core.add_question("Q1", "A1")
        q2 = self.core.add_question("Q2", "A2")
        q3 = self.core.add_question("Q3", "A3")

        # Create playlist with specific custom order [q3, q1, q2]
        pl_id = self.core.create_playlist("Custom Order Deck", [q3, q1, q2])
        items = self.core.fetch_playlist_questions(pl_id)
        self.assertEqual([r["question_id"] for r in items], [q3, q1, q2])
        self.assertEqual([r["play_order"] for r in items], [1, 2, 3])

        # Update playlist name and reorder to [q2, q3]
        self.core.update_playlist(pl_id, "Renamed Deck", [q2, q3])
        pls = [p for p in self.core.fetch_all_playlists() if p["playlist_id"] == pl_id]
        self.assertEqual(pls[0]["playlist_name"], "Renamed Deck")

        updated_items = self.core.fetch_playlist_questions(pl_id)
        self.assertEqual([r["question_id"] for r in updated_items], [q2, q3])
        self.assertEqual([r["play_order"] for r in updated_items], [1, 2])

        # Duplicate playlist name should raise IntegrityError
        with self.assertRaises(sqlite3.IntegrityError):
            self.core.create_playlist("Renamed Deck", [q1])

        # Delete playlist removes playlist and its Playlist_Items without deleting the Questions
        self.core.delete_playlist(pl_id)
        self.assertEqual(len(self.core.fetch_playlist_questions(pl_id)), 0)
        all_qids = [q["question_id"] for q in self.core.fetch_all_questions()]
        self.assertIn(q2, all_qids)
        self.assertIn(q3, all_qids)


# =====================================================================
# 4. Persistent User Preferences (App_Settings)
# =====================================================================
class TestAppSettingsPersistence(SonoriusCoreTestCase):

    def test_get_and_save_app_settings(self):
        # Default fallback when key does not exist
        self.assertEqual(self.core.get_app_setting("pref_voice_q", "af_sarah"), "af_sarah")

        # Save settings
        self.core.save_app_settings({
            "pref_voice_q": "am_fenrir",
            "pref_voice_a": "bf_emma",
            "pref_speed": 1.25,
            "pref_think": 0,
            "pref_delay": 3,
        })
        self.assertEqual(self.core.get_app_setting("pref_voice_q"), "am_fenrir")
        self.assertEqual(self.core.get_app_setting("pref_voice_a"), "bf_emma")
        self.assertEqual(float(self.core.get_app_setting("pref_speed")), 1.25)
        self.assertEqual(int(self.core.get_app_setting("pref_think")), 0)
        self.assertEqual(int(self.core.get_app_setting("pref_delay")), 3)

        # Overwrite existing setting (UPSERT behavior)
        self.core.save_app_settings({"pref_voice_q": "af_heart"})
        self.assertEqual(self.core.get_app_setting("pref_voice_q"), "af_heart")


# =====================================================================
# 5. Input Sanitization & Formula/Control-Byte Guards
# =====================================================================
class TestInputSanitization(SonoriusCoreTestCase):

    def test_strips_null_and_control_bytes_while_keeping_newlines(self):
        raw = "Hello\x00\x07 World\nLine 2\tTabbed"
        cleaned = self.core.sanitize_text_field(raw, "Question Text", 2000)
        self.assertEqual(cleaned, "Hello World\nLine 2\tTabbed")

    def test_rejects_empty_or_whitespace_only_input(self):
        with self.assertRaises(ValueError):
            self.core.sanitize_text_field("   \n\t  ", "Question Text", 2000)

    def test_rejects_input_exceeding_max_length(self):
        with self.assertRaises(ValueError):
            self.core.sanitize_text_field("A" * 121, "Playlist Name", 120)

    def test_blocks_dangerous_spreadsheet_dde_formulas(self):
        dangerous_payloads = [
            "=cmd|'/C calc'!A0",
            "+powershell -NoP -W Hidden",
            "-sh -c 'whoami'",
            "@rundll32.exe javascript:...",
        ]
        for payload in dangerous_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.core.sanitize_text_field(payload, "Answer Text", 10000)

    def test_neutralizes_benign_leading_equals_or_at_symbol(self):
        self.assertEqual(
            self.core.sanitize_text_field("=SUM(A1:A5)", "Answer Text", 1000),
            "'=SUM(A1:A5)",
        )
        self.assertEqual(
            self.core.sanitize_text_field("@decorator in Python", "Question Text", 1000),
            "'@decorator in Python",
        )


# =====================================================================
# 6. Image Verification, Magic-Byte Validation & Polyglot Stripping
# =====================================================================
class TestImageVerification(SonoriusCoreTestCase):

    def test_accepts_valid_png_jpeg_and_webp(self):
        for fmt, ext in [("PNG", ".png"), ("JPEG", ".jpg"), ("WEBP", ".webp")]:
            with self.subTest(fmt=fmt):
                raw = make_test_image_bytes(fmt=fmt)
                saved_path = self.core.save_verified_image(raw, f"diagram{ext}")
                self.assertTrue(os.path.exists(saved_path))
                self.assertEqual(stat.S_IMODE(os.stat(saved_path).st_mode), 0o600)

    def test_rejects_unsupported_extension(self):
        raw = make_test_image_bytes("PNG")
        with self.assertRaises(ValueError):
            self.core.save_verified_image(raw, "malicious.svg")
        with self.assertRaises(ValueError):
            self.core.save_verified_image(raw, "script.html")

    def test_rejects_mismatched_magic_bytes_or_corrupt_payload(self):
        # Non-image bytes disguised as .png
        with self.assertRaises(ValueError):
            self.core.save_verified_image(b"#!/bin/bash\necho pwned", "fake.png")

        # Valid PNG magic header followed by corrupted pixel stream
        with self.assertRaises(ValueError):
            self.core.save_verified_image(b"\x89PNG\r\n\x1a\ncorrupted_chunks_here", "corrupt.png")

    def test_strips_appended_polyglot_payload_on_reencode(self):
        valid_png = make_test_image_bytes("PNG")
        polyglot_marker = b"APPENDED_ZIP_OR_SCRIPT_PAYLOAD_123456789"
        polyglot_bytes = valid_png + polyglot_marker

        saved_path = self.core.save_verified_image(polyglot_bytes, "polyglot.png")
        with open(saved_path, "rb") as f:
            cleaned_on_disk = f.read()

        self.assertNotIn(polyglot_marker, cleaned_on_disk)


# =====================================================================
# 7. Network & Transport Security Guards
# =====================================================================
class TestTransportSecurityGuards(SonoriusCoreTestCase):

    def test_is_loopback_host(self):
        self.assertTrue(self.core._is_loopback_host("localhost"))
        self.assertTrue(self.core._is_loopback_host("127.0.0.1"))
        self.assertTrue(self.core._is_loopback_host("127.0.0.42"))
        self.assertTrue(self.core._is_loopback_host("::1"))
        self.assertTrue(self.core._is_loopback_host("[::1]"))
        self.assertFalse(self.core._is_loopback_host("192.168.1.50"))
        self.assertFalse(self.core._is_loopback_host("example.com"))

    def test_inspect_tts_endpoint_policies(self):
        self.core.ALLOW_INSECURE_HTTP_TTS = False

        # Loopback HTTP is allowed
        res_local = self.core.inspect_tts_endpoint("http://localhost:8880/v1/audio/speech")
        self.assertTrue(res_local["ok"])
        self.assertEqual(res_local["mode"], "local_loopback")

        res_127 = self.core.inspect_tts_endpoint("http://127.0.0.1:8880/v1/audio/speech")
        self.assertTrue(res_127["ok"])

        # Remote HTTPS is allowed
        res_https = self.core.inspect_tts_endpoint("https://192.168.1.10:8880/v1/audio/speech")
        self.assertTrue(res_https["ok"])
        self.assertEqual(res_https["mode"], "remote_https")

        # Remote plain HTTP is blocked by default
        res_remote_http = self.core.inspect_tts_endpoint("http://192.168.1.10:8880/v1/audio/speech")
        self.assertFalse(res_remote_http["ok"])
        self.assertEqual(res_remote_http["mode"], "remote_http_blocked")

        # Remote plain HTTP allowed only when ALLOW_INSECURE_HTTP_TTS=True
        self.core.ALLOW_INSECURE_HTTP_TTS = True
        res_override = self.core.inspect_tts_endpoint("http://192.168.1.10:8880/v1/audio/speech")
        self.assertTrue(res_override["ok"])
        self.assertEqual(res_override["mode"], "remote_http_override")

        # Link-local metadata SSRF (169.254.169.254) is always blocked even over HTTPS
        res_ssrf = self.core.inspect_tts_endpoint("https://169.254.169.254/latest/meta-data")
        self.assertFalse(res_ssrf["ok"])
        self.assertEqual(res_ssrf["mode"], "blocked")


# =====================================================================
# 8. TTS Audio Caching & Playlist Audio Stitching
# =====================================================================
class TestAudioCachingAndStitching(SonoriusCoreTestCase):

    def test_generate_kokoro_speech_caches_deterministically(self):
        fake_mp3 = make_test_mp3_bytes(150)
        with patch.object(self.core, "_fetch_kokoro_speech", return_value=fake_mp3) as mock_fetch:
            # First call -> cache miss -> invokes _fetch_kokoro_speech
            out1 = self.core.generate_kokoro_speech("Hello world", voice="af_sarah", speed=1.0)
            self.assertEqual(out1, fake_mp3)
            self.assertEqual(mock_fetch.call_count, 1)

            # Second identical call -> cache hit -> zero network requests
            out2 = self.core.generate_kokoro_speech("Hello world", voice="af_sarah", speed=1.0)
            self.assertEqual(out2, fake_mp3)
            self.assertEqual(mock_fetch.call_count, 1)

            # Changing voice or speed -> new cache key -> invokes _fetch_kokoro_speech
            self.core.generate_kokoro_speech("Hello world", voice="am_michael", speed=1.0)
            self.assertEqual(mock_fetch.call_count, 2)

            # Verify cache file has 0600 permissions
            expected_key = hashlib.sha256("af_sarah|1.00|Hello world".encode("utf-8")).hexdigest()
            cache_file = os.path.join(self.core.AUDIO_CACHE_DIR, f"{expected_key}.mp3")
            self.assertTrue(os.path.exists(cache_file))
            self.assertEqual(stat.S_IMODE(os.stat(cache_file).st_mode), 0o600)

        # Updating or deleting a question clears the audio cache
        self.core.clear_audio_cache()
        self.assertFalse(os.path.exists(cache_file))

    def test_stitch_playlist_audio_produces_combined_mp3_and_segments(self):
        fake_mp3 = make_test_mp3_bytes(200)
        self.core.add_question("Question 1", "Answer 1")
        self.core.add_question("Question 2", "Answer 2")
        questions = self.core.fetch_all_questions()

        with patch.object(self.core, "_fetch_kokoro_speech", return_value=fake_mp3):
            stitched_bytes = self.core.stitch_playlist_audio(
                questions=questions,
                think_time_sec=1,
                pause_between_sec=1,
                q_voice="af_sarah",
                a_voice="am_michael",
                speed=1.0,
            )

        self.assertIsInstance(stitched_bytes, bytes)
        self.assertGreater(len(stitched_bytes), 100)

        segments = self.core.st.session_state["audio_segments"]
        self.assertEqual(len(segments), len(questions))
        for seg in segments:
            self.assertIn("q_start", seg)
            self.assertIn("think_start", seg)
            self.assertIn("a_start", seg)
            self.assertIn("a_end", seg)
            self.assertGreaterEqual(seg["think_start"], seg["q_start"])
            self.assertGreaterEqual(seg["a_start"], seg["think_start"])
            self.assertGreaterEqual(seg["a_end"], seg["a_start"])


# =====================================================================
# 9. Headless Streamlit UI Smoke Test
# =====================================================================
class TestStreamlitUISmoke(unittest.TestCase):
    """Run app.py inside Streamlit's headless AppTest runner across all navigation routes."""

    def test_all_navigation_routes_render_without_exceptions(self):
        from streamlit.testing.v1 import AppTest

        # app.py anchors its DB/images/cache to its own folder (os.chdir(BASE_DIR)),
        # so run a COPY of app.py from a temporary directory. This guarantees the
        # smoke test never opens, modifies, or deletes the user's real interview_data.db.
        smoke_temp = tempfile.mkdtemp(prefix="sonorius_smoke_")
        orig_cwd = os.getcwd()
        try:
            smoke_app = os.path.join(smoke_temp, "app.py")
            shutil.copy2(APP_FILE, smoke_app)
            intro_src = os.path.join(os.path.dirname(APP_FILE), "intro_audio.mp3")
            if os.path.exists(intro_src):
                shutil.copy2(intro_src, os.path.join(smoke_temp, "intro_audio.mp3"))
            os.chdir(smoke_temp)
            at = AppTest.from_file(smoke_app, default_timeout=30)
            at.run()
            self.assertEqual(len(at.exception), 0, f"Exception on initial Play Session load: {at.exception}")

            # Navigate to "Add New Q&A"
            nav_radios = [r for r in at.radio if r.label == "Navigation Menu"]
            self.assertEqual(len(nav_radios), 1)
            nav_radios[0].set_value(":material/add_circle: Add New Q&A").run()
            self.assertEqual(len(at.exception), 0, f"Exception on Add New Q&A route: {at.exception}")

            # Navigate to "Manage Playlists"
            nav_radios = [r for r in at.radio if r.label == "Navigation Menu"]
            nav_radios[0].set_value(":material/queue_music: Manage Playlists").run()
            self.assertEqual(len(at.exception), 0, f"Exception on Manage Playlists route: {at.exception}")

            # Navigate back to "Play Session"
            nav_radios = [r for r in at.radio if r.label == "Navigation Menu"]
            nav_radios[0].set_value(":material/play_circle: Play Session").run()
            self.assertEqual(len(at.exception), 0, f"Exception returning to Play Session route: {at.exception}")
        finally:
            os.chdir(orig_cwd)
            shutil.rmtree(smoke_temp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
