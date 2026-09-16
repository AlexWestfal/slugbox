import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine import config
from library import db
from worker.download_worker import DownloadWorker


class TestWorker(unittest.TestCase):
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

    def test_enqueue_and_cancel_queued_job(self):
        with patch.object(config, "DB_PATH", self.db_path):
            db.init()
            broadcast_mock = MagicMock()
            nudge_mock = MagicMock()
            worker = DownloadWorker(broadcast=broadcast_mock, nudge_scanner=nudge_mock)

            preview = {
                "name": "Test Album",
                "kind": "album",
                "cover": "http://example.com/art.jpg",
                "track_count": 5,
            }

            # Enqueue
            job_id = worker.enqueue("https://open.spotify.com/album/test", preview)
            self.assertIsNotNone(job_id)
            broadcast_mock.assert_called_with({
                "event": "download_queued",
                "job_id": job_id,
                "name": "Test Album",
                "total": 5,
                "cover": "http://example.com/art.jpg",
            })

            # Check database row
            job_row = db.get_job(job_id)
            self.assertIsNotNone(job_row)
            self.assertEqual(job_row["status"], "queued")
            self.assertEqual(job_row["track_count"], 5)

            # Check status aggregation
            status = db.job_status()
            self.assertEqual(status["queued"], 1)
            self.assertIsNotNone(status["active"])
            self.assertEqual(status["active"]["job_id"], job_id)

            # Cancel the queued job
            cancelled = worker.cancel(job_id)
            self.assertTrue(cancelled)

            job_row_after = db.get_job(job_id)
            self.assertEqual(job_row_after["status"], "cancelled")

            # Cancel non-existent job
            self.assertFalse(worker.cancel("non-existent-id"))


if __name__ == "__main__":
    unittest.main()
