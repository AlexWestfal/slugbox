import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from engine import config
from library import covers, db
import server


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.music_dir = Path(self.temp_dir.name) / "Music"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.temp_dir.name) / "library.db"
        self.cover_dir = Path(self.temp_dir.name) / "covers"
        self.cover_dir.mkdir(parents=True, exist_ok=True)

        db._initialised = False
        if hasattr(db._local, "conn"):
            delattr(db._local, "conn")

        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        if hasattr(db._local, "conn"):
            db._local.conn.close()
            delattr(db._local, "conn")
        db._initialised = False
        self.temp_dir.cleanup()

    def test_static_routes(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"SLUGBOX", resp.data)

        resp = self.client.get("/favicon.ico")
        self.assertEqual(resp.status_code, 204)

        resp = self.client.get("/fonts.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"@font-face", resp.data)

    def test_api_health(self):
        with patch.object(config, "DB_PATH", self.db_path):
            db.init()
            resp = self.client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertIn("version", data)
            self.assertIn("library", data)

    def test_api_library_shape(self):
        with patch.object(config, "DB_PATH", self.db_path):
            db.init()
            resp = self.client.get("/api/library")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn("folders", data)
            self.assertIsInstance(data["folders"], list)

    def test_api_settings_get_and_post(self):
        test_cfg = Path(self.temp_dir.name) / "config.json"
        with patch.object(config, "CONFIG_PATH", test_cfg), \
             patch.object(config, "_cache", None):
            resp = self.client.get("/api/settings")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn("values", data)
            self.assertIn("schema", data)

            post_resp = self.client.post("/api/settings", json={"format": "flac", "threads": 1})
            self.assertEqual(post_resp.status_code, 200)
            updated = post_resp.get_json()["values"]
            self.assertEqual(updated["format"], "flac")
            self.assertEqual(updated["threads"], 1)

    def test_music_serving_and_range_requests(self):
        test_file = self.music_dir / "test.wav"
        with wave.open(str(test_file), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 1000)

        file_size = test_file.stat().st_size

        with patch.object(config, "music_dir", return_value=self.music_dir):
            # Full content GET
            resp = self.client.get("/music/test.wav")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(resp.data), file_size)

            # Range request GET (first 16 bytes)
            range_resp = self.client.get("/music/test.wav", headers={"Range": "bytes=0-15"})
            self.assertEqual(range_resp.status_code, 206)
            self.assertEqual(len(range_resp.data), 16)
            self.assertIn("Content-Range", range_resp.headers)
            self.assertTrue(range_resp.headers["Content-Range"].startswith(f"bytes 0-15/{file_size}"))

    def test_music_path_traversal_protection(self):
        with patch.object(config, "music_dir", return_value=self.music_dir):
            # Attempt to traverse outside music directory
            resp = self.client.get("/music/../config.json")
            self.assertIn(resp.status_code, (403, 404))

            resp2 = self.client.get("/music/..%2fconfig.json")
            self.assertIn(resp2.status_code, (403, 404))

    def test_cover_hash_validation(self):
        with patch.object(config, "COVER_DIR", self.cover_dir):
            # Path traversal attempt
            resp = self.client.get("/api/cover/../../etc/passwd.jpg")
            self.assertEqual(resp.status_code, 404)

            # Invalid hash length
            resp2 = self.client.get("/api/cover/short.jpg")
            self.assertEqual(resp2.status_code, 404)

            # Non-existent valid hash
            resp3 = self.client.get("/api/cover/0123456789abcdef.jpg")
            self.assertEqual(resp3.status_code, 404)

            # Existing valid cover
            (self.cover_dir / "0123456789abcdef.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
            resp4 = self.client.get("/api/cover/0123456789abcdef.jpg")
            self.assertEqual(resp4.status_code, 200)


if __name__ == "__main__":
    unittest.main()
