import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_file = Path(self.temp_dir.name) / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_defaults(self):
        with patch.object(config, "CONFIG_PATH", self.config_file):
            with patch.object(config, "_cache", None):
                cfg = config.load()
                self.assertIn("format", cfg)
                self.assertEqual(cfg["format"], "mp3")
                self.assertEqual(cfg["bitrate"], "320k")
                self.assertEqual(cfg["threads"], 2)
                self.assertIn("music_dir", cfg)

    def test_coerce_types(self):
        setting_int = config._BY_KEY["threads"]
        self.assertEqual(config._coerce(setting_int, "4"), 4)
        self.assertEqual(config._coerce(setting_int, 4), 4)
        self.assertEqual(config._coerce(setting_int, "invalid"), setting_int.default)

        setting_bool = config._BY_KEY["playlist_numbering"]
        self.assertIs(config._coerce(setting_bool, "true"), True)
        self.assertIs(config._coerce(setting_bool, "1"), True)
        self.assertIs(config._coerce(setting_bool, "false"), False)
        self.assertIs(config._coerce(setting_bool, "0"), False)

        setting_choice = config._BY_KEY["format"]
        self.assertEqual(config._coerce(setting_choice, "flac"), "flac")
        self.assertEqual(config._coerce(setting_choice, "invalid_fmt"), setting_choice.default)

    def test_save_and_reload(self):
        with patch.object(config, "CONFIG_PATH", self.config_file):
            with patch.object(config, "_cache", None):
                updated = config.save({"format": "opus", "threads": 3})
                self.assertEqual(updated["format"], "opus")
                self.assertEqual(updated["threads"], 3)

                # Reset cache and load from file directly
                config._cache = None
                loaded = config.load()
                self.assertEqual(loaded["format"], "opus")
                self.assertEqual(loaded["threads"], 3)

    def test_corrupt_config_fallback(self):
        self.config_file.write_text("NOT_VALID_JSON{{{", encoding="utf-8")
        with patch.object(config, "CONFIG_PATH", self.config_file):
            with patch.object(config, "_cache", None):
                loaded = config.load()
                # Should not raise exception and should return defaults
                self.assertEqual(loaded["format"], "mp3")

    def test_describe_schema(self):
        desc = config.describe()
        self.assertIsInstance(desc, list)
        keys = [d["key"] for d in desc]
        self.assertIn("music_dir", keys)
        self.assertIn("format", keys)
        self.assertIn("threads", keys)


if __name__ == "__main__":
    unittest.main()
