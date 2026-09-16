import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import config
from library import covers, db


class TestCovers(unittest.TestCase):
    def test_hash_bytes(self):
        sample = b"image data content"
        h = covers.hash_bytes(sample)
        self.assertEqual(len(h), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_is_valid_hash(self):
        self.assertTrue(covers.is_valid_hash("0123456789abcdef"))
        self.assertFalse(covers.is_valid_hash("0123456789abcde"))  # 15 chars
        self.assertFalse(covers.is_valid_hash("0123456789abcdefg"))  # 17 chars
        self.assertFalse(covers.is_valid_hash("0123456789abcdeZ"))  # invalid char
        self.assertFalse(covers.is_valid_hash("../../../etc/passwd"))  # path traversal

    def test_detect_image_ext(self):
        self.assertEqual(covers.detect_image_ext(b"\xff\xd8\xff\xe0"), "jpg")
        self.assertEqual(covers.detect_image_ext(b"\x89PNG\r\n\x1a\n"), "png")
        self.assertEqual(covers.detect_image_ext(b"GIF89a\x01\x00"), "gif")
        self.assertEqual(covers.detect_image_ext(b"BM\x00\x00"), "bmp")
        self.assertEqual(covers.detect_image_ext(b"RIFF\x00\x00\x00\x00WEBP"), "webp")

    def test_cache_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_cover_dir = Path(tmp) / "covers"
            with patch.object(config, "COVER_DIR", tmp_cover_dir):
                data = b"\xff\xd8\xff\xe0" + (b"samplejpegdata" * 10)
                h = covers.cache_cover(data)
                self.assertIsNotNone(h)
                self.assertEqual(len(h), 16)
                cached_file = tmp_cover_dir / f"{h}.jpg"
                self.assertTrue(cached_file.exists())
                self.assertEqual(cached_file.read_bytes(), data)


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
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

    def test_db_init_and_counts(self):
        with patch.object(config, "DB_PATH", self.db_path):
            db.init()
            counts = db.counts()
            self.assertEqual(counts["folders"], 0)
            self.assertEqual(counts["tracks"], 0)

    def test_folder_and_track_upsert_and_tree(self):
        with patch.object(config, "DB_PATH", self.db_path):
            db.init()
            folder_id = "f_test123"
            db.upsert_folder({
                "id": folder_id,
                "path": "Album A",
                "name": "Album A",
                "artist": "Artist One",
                "track_count": 1,
                "duration_s": 180,
                "cover_hash": "1234567890abcdef",
                "updated_at": 1000.0,
            })

            track_id = "t_test123"
            db.upsert_track({
                "id": track_id,
                "folder_id": folder_id,
                "path": "Album A/01. Song.mp3",
                "title": "Song",
                "artist": "Artist One",
                "album": "Album A",
                "year": "2024",
                "track_number": 1,
                "duration_s": 180,
                "cover_hash": "1234567890abcdef",
                "file_size": 10240,
                "mtime": 1000.0,
                "updated_at": 1000.0,
            })

            tree = db.library_tree()
            self.assertEqual(len(tree), 1)
            f = tree[0]
            self.assertEqual(f["name"], "Album A")
            self.assertEqual(f["artist"], "Artist One")
            self.assertEqual(f["cover"], "/api/cover/1234567890abcdef.jpg")
            self.assertEqual(len(f["tracks"]), 1)
            t = f["tracks"][0]
            self.assertEqual(t["title"], "Song")
            self.assertEqual(t["track_number"], 1)


if __name__ == "__main__":
    unittest.main()
