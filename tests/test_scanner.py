import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from engine import config
from library import db, scanner


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.music_dir = Path(self.temp_dir.name) / "Music"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir.name) / "library.db"
        db._initialised = False
        if hasattr(db._local, "conn"):
            delattr(db._local, "conn")

    def tearDown(self):
        if hasattr(db._local, "conn"):
            db._local.conn.close()
            delattr(db._local, "conn")
        db._initialised = False
        self.temp_dir.cleanup()

    def test_parse_track_number(self):
        self.assertEqual(scanner._parse_track_number("3"), 3)
        self.assertEqual(scanner._parse_track_number("03"), 3)
        self.assertEqual(scanner._parse_track_number("3/12"), 3)
        self.assertEqual(scanner._parse_track_number("  7  "), 7)
        self.assertIsNone(scanner._parse_track_number("none"))
        self.assertIsNone(scanner._parse_track_number(""))

    def test_parse_year(self):
        self.assertEqual(scanner._parse_year("2024"), "2024")
        self.assertEqual(scanner._parse_year("2024-05-12"), "2024")
        self.assertEqual(scanner._parse_year("Recorded in 1999 at Electric Lady"), "1999")
        self.assertIsNone(scanner._parse_year("No date"))

    def test_scan_real_wav_files(self):
        # Create a playlist folder with a valid WAV file
        folder = self.music_dir / "Test Playlist - Artist"
        folder.mkdir(parents=True, exist_ok=True)
        wav_file = folder / "01. Test Track.wav"

        with wave.open(str(wav_file), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 44100)

        # Also create a non-audio file that should be ignored
        (folder / "cover.txt").write_text("not audio", encoding="utf-8")

        with patch.object(config, "DB_PATH", self.db_path), \
             patch.object(config, "music_dir", return_value=self.music_dir):
            db.init()
            summary = scanner.scan(force=True)
            self.assertEqual(summary["added"], 1)
            self.assertEqual(summary["removed"], 0)

            tree = db.library_tree()
            self.assertEqual(len(tree), 1)
            f = tree[0]
            self.assertEqual(f["name"], "Test Playlist - Artist")
            self.assertEqual(f["track_count"], 1)
            self.assertEqual(len(f["tracks"]), 1)
            self.assertEqual(f["tracks"][0]["title"], "01. Test Track")

    def test_scan_handles_corrupt_file(self):
        folder = self.music_dir / "Broken"
        folder.mkdir(parents=True, exist_ok=True)
        broken_file = folder / "corrupt.mp3"
        broken_file.write_bytes(b"garbage content not real mp3")

        with patch.object(config, "DB_PATH", self.db_path), \
             patch.object(config, "music_dir", return_value=self.music_dir):
            db.init()
            # scan must not crash on corrupt file
            summary = scanner.scan(force=True)
            self.assertEqual(summary["added"], 0)


if __name__ == "__main__":
    unittest.main()
