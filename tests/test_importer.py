import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import importer


HASH1 = "c5c30def765f38a51d5f514c099bccc9ca6d076f"
HASH2 = "1dfc7ee3eafcd29f763c430b0d73846e30785d91"


def make_grab(torrent_hash=HASH1, history_id=1, title="Example Movie"):
    data = {
        "torrentInfoHash": torrent_hash,
        "downloadUrl": f"magnet:?xt=urn:btih:{torrent_hash}",
        "downloadId": f"download-{history_id}",
    }
    return importer.Grab(
        history_id=history_id,
        movie_id=123 + history_id,
        series_id=None,
        source_title=title,
        date="2026-10-19T12:00:00Z",
        data=data,
        quality={},
        languages=[],
        custom_formats=[],
        custom_format_score=0,
        download_id=f"download-{history_id}",
    )


def ready_record(torrent_hash, title="Example Movie"):
    return {
        "hash": torrent_hash,
        "title": title,
        "stat": 3,
        "stat_string": "Torrent working",
        "file_stats": [{"id": 1, "path": "Example.mkv", "length": 1024}],
    }


def make_config(root):
    return importer.Config(
        name="radarr-1080",
        arr_type="radarr",
        arr_url="http://127.0.0.1:7878",
        arr_api_key="test-key",
        torrserver_url="http://127.0.0.1:8092",
        category="movie",
        target_quality=1080,
        state_db=str(root / "state.db"),
        lock_file=str(root / "radarr-1080.lock"),
        resolve_download_url=False,
        verification_timeout_sec=10,
        verification_poll_interval_sec=0.2,
        retry_backoff_sec=1,
        max_add_attempts=3,
    )


class VerificationEvidenceTests(unittest.TestCase):
    def test_hash_echo_without_readiness_is_not_confirmed(self):
        record = {
            "hash": HASH1,
            "stat": 0,
            "stat_string": "Torrent added",
        }
        self.assertIsNone(importer.verification_evidence(record))

    def test_usable_file_stats_confirm_readiness(self):
        self.assertEqual(importer.verification_evidence(ready_record(HASH1)), "file-stats")

    def test_working_status_is_recognized(self):
        self.assertEqual(
            importer.verification_evidence(
                {"hash": HASH1, "stat": 3, "stat_string": "Torrent working"}
            ),
            "working-status",
        )

    def test_database_only_record_is_recognized(self):
        self.assertEqual(
            importer.verification_evidence(
                {"hash": HASH1, "stat": 5, "stat_string": "Torrent in db"}
            ),
            "database-presence",
        )

    def test_bad_file_stats_do_not_confirm(self):
        self.assertIsNone(
            importer.verification_evidence(
                {
                    "hash": HASH1,
                    "stat": 1,
                    "stat_string": "Torrent getting info",
                    "file_stats": [{"id": 0, "path": ""}],
                }
            )
        )


class PendingStateTests(unittest.TestCase):
    def test_pending_hash_is_not_imported_until_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = importer.StateDB(str(Path(tmp) / "state.db"))
            grab = make_grab()
            db.begin_pending(grab, "radarr-1080", {"imdbId": "tt123"}, 120)
            self.assertNotIn(HASH1, db.all_hashes())
            self.assertEqual(db.pending_rows()[0]["status"], "pending")

            columns = {
                row["name"]
                for row in db.conn.execute("PRAGMA table_info(pending_adds)").fetchall()
            }
            self.assertNotIn("downloadUrl", columns)
            self.assertNotIn("magnet:", repr(db.pending_rows()))

            db.confirm_pending(HASH1)
            self.assertIn(HASH1, db.all_hashes())
            self.assertEqual(db.pending_rows(), [])
            db.close()

    def test_second_instance_cannot_claim_same_pending_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = importer.StateDB(str(Path(tmp) / "state.db"))
            grab = make_grab()
            self.assertEqual(db.begin_pending(grab, "radarr-1080", {}, 120), 1)
            self.assertEqual(db.begin_pending(grab, "radarr-1080", {}, 120), 0)
            self.assertEqual(len(db.pending_rows()), 1)
            db.close()

    def test_failed_attempt_uses_backoff_and_attempt_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = importer.StateDB(str(Path(tmp) / "state.db"))
            grab = make_grab()
            self.assertEqual(db.begin_pending(grab, "radarr-1080", {}, 120), 1)
            self.assertEqual(db.fail_pending(HASH1, "timeout", 3, 1), "retry_wait")

            db.conn.execute(
                "UPDATE pending_adds SET next_retry_at = ? WHERE hash = ?",
                (importer.iso_now(), HASH1),
            )
            db.conn.commit()
            self.assertTrue(
                importer.pending_retry_due(
                    db.pending_rows()[0], "radarr-1080",
                    importer.datetime.now(importer.timezone.utc), 3,
                )
            )
            self.assertEqual(db.begin_pending(grab, "radarr-1080", {}, 120), 2)
            self.assertEqual(db.fail_pending(HASH1, "timeout again", 3, 1), "retry_wait")

            db.conn.execute(
                "UPDATE pending_adds SET next_retry_at = ? WHERE hash = ?",
                (importer.iso_now(), HASH1),
            )
            db.conn.commit()
            self.assertEqual(db.begin_pending(grab, "radarr-1080", {}, 120), 3)
            self.assertEqual(db.fail_pending(HASH1, "last timeout", 3, 1), "failed")
            self.assertFalse(
                importer.pending_retry_due(
                    db.pending_rows()[0], "radarr-1080",
                    importer.datetime.now(importer.timezone.utc), 3,
                )
            )
            db.close()


class BatchVerificationTests(unittest.TestCase):
    def test_batch_polling_confirms_fast_and_slow_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            grabs = [
                make_grab(HASH1, 1, "Fast Movie"),
                make_grab(HASH2, 2, "Slow Movie"),
            ]
            not_ready = {
                "hash": HASH2,
                "title": "Slow Movie",
                "stat": 1,
                "stat_string": "Torrent getting info",
            }
            list_results = [
                [],
                [ready_record(HASH1, "Fast Movie"), not_ready],
                [ready_record(HASH1, "Fast Movie"), ready_record(HASH2, "Slow Movie")],
            ]

            with (
                mock.patch.object(importer, "fetch_history", return_value=grabs),
                mock.patch.object(importer, "fetch_torrserver", side_effect=list_results) as list_mock,
                mock.patch.object(importer, "fetch_media", return_value={"imdbId": "tt123"}),
                mock.patch.object(
                    importer,
                    "add_to_torrserver",
                    side_effect=lambda _cfg, _ts, grab, _media, resolved_link=None: {
                        "hash": grab.hash,
                        "stat_string": "Torrent added",
                    },
                ) as add_mock,
            ):
                rc = importer.run(cfg)

            self.assertEqual(rc, 0)
            self.assertEqual(add_mock.call_count, 2)
            self.assertEqual(list_mock.call_count, 3)
            db = importer.StateDB(cfg.state_db)
            self.assertEqual(db.all_hashes(), {HASH1, HASH2})
            self.assertEqual(db.pending_rows(), [])
            db.close()

    def test_restart_reconciles_pending_hash_without_resending_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            grab = make_grab()
            db = importer.StateDB(cfg.state_db)
            db.begin_pending(grab, cfg.name, {"imdbId": "tt123"}, 120)
            db.close()

            with (
                mock.patch.object(importer, "fetch_history", return_value=[grab]),
                mock.patch.object(importer, "fetch_torrserver", return_value=[ready_record(HASH1)]),
                mock.patch.object(importer, "fetch_media", return_value={"imdbId": "tt123"}),
                mock.patch.object(importer, "add_to_torrserver") as add_mock,
            ):
                rc = importer.run(cfg)

            self.assertEqual(rc, 0)
            add_mock.assert_not_called()
            db = importer.StateDB(cfg.state_db)
            self.assertIn(HASH1, db.all_hashes())
            self.assertEqual(db.pending_rows(), [])
            db.close()


if __name__ == "__main__":
    unittest.main()
