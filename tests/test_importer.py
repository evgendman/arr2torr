import json
import sys
import threading
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

    def test_verified_import_cannot_be_claimed_again_from_a_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = importer.StateDB(str(Path(tmp) / "state.db"))
            grab = make_grab()
            db.add(grab, "radarr-1080", {"imdbId": "tt123"})
            self.assertEqual(db.begin_pending(grab, "radarr-1080", {}, 120), 0)
            self.assertEqual(db.pending_rows(), [])
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


    def test_acknowledged_or_uncertain_add_timeout_never_becomes_import_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            grab = make_grab()

            with (
                mock.patch.object(importer, "fetch_history", return_value=[grab]),
                mock.patch.object(importer, "fetch_torrserver", side_effect=[[], []]),
                mock.patch.object(importer, "fetch_media", return_value={"imdbId": "tt123"}),
                mock.patch.object(
                    importer, "add_to_torrserver",
                    side_effect=importer.ImporterError("simulated uncertain add response"),
                ),
                mock.patch.object(
                    importer, "pending_deadline",
                    return_value=importer.datetime.now(importer.timezone.utc) - importer.timedelta(seconds=1),
                ),
            ):
                rc = importer.run(cfg)

            self.assertEqual(rc, 1)
            db = importer.StateDB(cfg.state_db)
            self.assertNotIn(HASH1, db.all_hashes())
            pending = db.pending_rows()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["status"], "retry_wait")
            self.assertIn("simulated uncertain add response", pending[0]["last_error"])
            db.close()




class StateMigrationTests(unittest.TestCase):
    def test_concurrent_initializers_serialize_schema_migrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            conn = importer.sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE imports (
                    hash TEXT PRIMARY KEY, imported_at TEXT NOT NULL, source TEXT NOT NULL,
                    arr_history_id INTEGER, arr_media_id INTEGER, download_id TEXT, title TEXT,
                    imdb_id TEXT, tmdb_id TEXT, tvdb_id TEXT
                );
                CREATE INDEX idx_imports_source ON imports(source);
                CREATE TABLE pending_adds (
                    hash TEXT PRIMARY KEY, source TEXT NOT NULL, arr_history_id INTEGER,
                    arr_media_id INTEGER, download_id TEXT, title TEXT, imdb_id TEXT,
                    tmdb_id TEXT, tvdb_id TEXT, requested_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending', last_checked_at TEXT,
                    last_status TEXT, last_error TEXT, next_retry_at TEXT,
                    notification_attempted_at TEXT
                );
                CREATE INDEX idx_pending_source_status ON pending_adds(source, status);
                CREATE TABLE notification_state (
                    state_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE terminal_events (
                    event_key TEXT PRIMARY KEY, source TEXT NOT NULL, arr_type TEXT NOT NULL,
                    category TEXT NOT NULL, arr_media_id INTEGER, media_title TEXT,
                    release_title TEXT, title_slug TEXT, stage TEXT NOT NULL,
                    attempt_count INTEGER, max_attempts INTEGER, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL, notification_attempted_at TEXT
                );
                CREATE INDEX idx_terminal_events_source
                    ON terminal_events(source, notification_attempted_at);
                """
            )
            conn.close()

            start = threading.Barrier(2)
            initialized_columns = []
            errors = []

            def initialize():
                try:
                    start.wait(timeout=5)
                    db = importer.StateDB(str(db_path))
                    columns = {
                        row["name"]
                        for row in db.conn.execute("PRAGMA table_info(pending_adds)").fetchall()
                    }
                    db.close()
                    initialized_columns.append(columns)
                except Exception as exc:  # collect thread failures for the assertion below
                    errors.append(exc)

            threads = [threading.Thread(target=initialize) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(initialized_columns), 2)
            self.assertIn("seerr_watchlist_status", initialized_columns[0])
            self.assertIn("seerr_watchlist_warning_notified_at", initialized_columns[0])
            self.assertEqual(initialized_columns[0], initialized_columns[1])

    def test_v11_database_migrates_in_place_and_baselines_old_failed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            conn = importer.sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE imports (
                    hash TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    arr_history_id INTEGER,
                    arr_media_id INTEGER,
                    download_id TEXT,
                    title TEXT,
                    imdb_id TEXT,
                    tmdb_id TEXT,
                    tvdb_id TEXT
                );
                CREATE INDEX idx_imports_source ON imports(source);
                CREATE TABLE pending_adds (
                    hash TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    arr_history_id INTEGER,
                    arr_media_id INTEGER,
                    download_id TEXT,
                    title TEXT,
                    imdb_id TEXT,
                    tmdb_id TEXT,
                    tvdb_id TEXT,
                    requested_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_checked_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    next_retry_at TEXT
                );
                CREATE INDEX idx_pending_source_status ON pending_adds(source, status);
                INSERT INTO pending_adds
                  (hash, source, title, requested_at, deadline_at, attempt_count, status, last_error)
                VALUES
                  ('c5c30def765f38a51d5f514c099bccc9ca6d076f',
                   'radarr-1080', 'Old.Release.1080p',
                   '2026-10-20T12:00:00+00:00', '2026-10-20T12:02:00+00:00',
                   3, 'failed', 'old terminal failure');
                """
            )
            conn.commit()
            conn.close()

            db = importer.StateDB(str(db_path))
            row = db.pending_rows("radarr-1080")[0]
            self.assertEqual(row["title"], "Old.Release.1080p")
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempt_count"], 3)
            self.assertIsNotNone(row["notification_attempted_at"])
            self.assertEqual(row["radarr_blocklist_status"], "legacy_skipped")
            self.assertIn("media_title", {
                item["name"] for item in db.conn.execute("PRAGMA table_info(pending_adds)")
            })
            self.assertEqual(db.unnotified_terminal_failures("radarr-1080"), [])
            self.assertIsNotNone(
                db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_state'"
                ).fetchone()
            )
            self.assertIsNotNone(
                db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='terminal_events'"
                ).fetchone()
            )
            db.close()


class SeerrWatchlistTests(unittest.TestCase):
    def make_seerr_config(self, root, *, arr_type="radarr"):
        cfg = make_config(root)
        if arr_type == "sonarr":
            cfg = importer.replace(
                cfg,
                name="sonarr-1080",
                arr_type="sonarr",
                category="tv",
                lock_file=str(Path(root) / "sonarr-1080.lock"),
            )
        return importer.replace(
            cfg,
            notifications_enabled=True,
            apprise_url="http://192.168.0.14:8000",
            apprise_config_key="arr-stack",
            service_name=f"arr2torr / {arr_type} test",
            notification_tag_critical="critical",
            notification_tag_warning="warnings",
            seerr_watchlist_enabled=True,
            seerr_url="http://192.168.0.14:5055",
            seerr_api_key="test-seerr-key",
        ).normalized()

    def add_failed_pending(self, db, cfg, *, torrent_hash=HASH1, tmdb_id=12345,
                           media_title="Example Media", release_title="Example.Release"):
        grab = make_grab(torrent_hash, 1, release_title)
        db.begin_pending(
            grab, cfg.name,
            {"title": media_title, "tmdbId": tmdb_id, "tvdbId": 54321},
            120,
        )
        self.assertEqual(db.fail_pending(torrent_hash, "TorrServer verification failed", 1, 1), "failed")

    def fake_client_factory(self, actions, calls):
        class FakeClient:
            def __init__(self, timeout, user_agent, api_key=None, **_kwargs):
                self.api_key = api_key

            def request_json(self, method, url, payload=None, api_key=False,
                             retry_count=0, retry_delay=1.0):
                calls.append({
                    "method": method,
                    "url": url,
                    "payload": payload,
                    "api_key_header": api_key,
                    "api_key": self.api_key,
                })
                if not actions:
                    raise AssertionError(f"Unexpected HTTP call: {method} {url}")
                action = actions.pop(0)
                if isinstance(action, Exception):
                    raise action
                return action

        return FakeClient

    def test_feature_is_off_by_default_and_enabled_requires_real_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp)).normalized()
            self.assertFalse(cfg.seerr_watchlist_enabled)
            with self.assertRaisesRegex(ValueError, "seerr_url"):
                importer.replace(cfg, seerr_watchlist_enabled=True).normalized()
            valid = importer.replace(
                cfg,
                notifications_enabled=True,
                apprise_url="http://192.168.0.14:8000",
                seerr_watchlist_enabled=True,
                seerr_url="http://192.168.0.14:5055",
                seerr_api_key="test-seerr-key",
            ).normalized()
            self.assertEqual(valid.seerr_url, "http://192.168.0.14:5055")
            with self.assertRaisesRegex(ValueError, "real key"):
                importer.replace(valid, seerr_api_key="PUT_SEERR_API_KEY_HERE").normalized()

    def test_existing_terminal_events_schema_migrates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            conn = importer.sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE terminal_events (
                    event_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    arr_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    arr_media_id INTEGER,
                    media_title TEXT,
                    release_title TEXT,
                    title_slug TEXT,
                    stage TEXT NOT NULL,
                    attempt_count INTEGER,
                    max_attempts INTEGER,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    notification_attempted_at TEXT
                );
                INSERT INTO terminal_events
                  (event_key, source, arr_type, category, media_title, release_title,
                   stage, reason, created_at)
                VALUES ('legacy-event', 'radarr-1080', 'radarr', 'movie',
                        'Legacy Movie', 'Legacy.Release', 'prepare', 'old failure',
                        '2026-10-20T12:00:00+00:00');
                """
            )
            conn.commit()
            conn.close()

            db = importer.StateDB(str(db_path))
            columns = {
                row["name"]
                for row in db.conn.execute("PRAGMA table_info(terminal_events)").fetchall()
            }
            for column in (
                "tmdb_id", "tvdb_id", "seerr_watchlist_status",
                "seerr_watchlist_error_fingerprint",
                "seerr_watchlist_error_notified_fingerprint",
                "seerr_watchlist_warning_notified_at",
            ):
                self.assertIn(column, columns)
            legacy = db.conn.execute(
                "SELECT media_title, reason FROM terminal_events WHERE event_key='legacy-event'"
            ).fetchone()
            self.assertEqual(legacy["media_title"], "Legacy Movie")
            self.assertEqual(legacy["reason"], "old failure")
            db.close()

    def test_movie_post_adds_once_and_sends_warning_to_warnings_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_seerr_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            self.add_failed_pending(db, cfg, media_title="The Example Movie")
            calls, sent = [], []
            FakeClient = self.fake_client_factory([{"id": 99}], calls)
            with (
                mock.patch.object(importer, "HttpClient", FakeClient),
                mock.patch.object(importer, "apprise_notify",
                                  side_effect=lambda _cfg, title, body, typ, tag: sent.append(
                                      {"title": title, "body": body, "type": typ, "tag": tag}
                                  ) or True),
            ):
                importer.process_seerr_watchlist(cfg, db)
                importer.process_seerr_watchlist(cfg, db)

            posts = [call for call in calls if call["method"] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["url"], "http://192.168.0.14:5055/api/v1/watchlist")
            self.assertEqual(posts[0]["payload"], {"tmdbId": 12345, "mediaType": "movie"})
            self.assertTrue(posts[0]["api_key_header"])
            self.assertEqual(posts[0]["api_key"], "test-seerr-key")
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["type"], "warning")
            self.assertEqual(sent[0]["tag"], "warnings")
            self.assertIn("не означает, что медиа уже доступно", sent[0]["body"])
            self.assertIn("The Example Movie", sent[0]["body"])
            row = db.pending_rows(cfg.name)[0]
            self.assertEqual(row["seerr_watchlist_status"], "added")
            self.assertIsNotNone(row["seerr_watchlist_warning_notified_at"])
            db.close()

    def test_series_post_requests_the_whole_series_at_title_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_seerr_config(Path(tmp), arr_type="sonarr")
            db = importer.StateDB(cfg.state_db)
            grab = importer.Grab(
                history_id=1, movie_id=None, series_id=44,
                source_title="Example.Series.S02E03", date=None,
                data={"torrentInfoHash": HASH1, "downloadUrl": f"magnet:?xt=urn:btih:{HASH1}"},
                quality={}, languages=[], custom_formats=[], custom_format_score=None, download_id=None,
            )
            db.begin_pending(grab, cfg.name, {"title": "Example Series", "tmdbId": 9876, "tvdbId": 54321}, 120)
            db.fail_pending(HASH1, "TorrServer verification failed", 1, 1)
            calls, sent = [], []
            FakeClient = self.fake_client_factory([{"id": 100}], calls)
            with (
                mock.patch.object(importer, "HttpClient", FakeClient),
                mock.patch.object(importer, "apprise_notify",
                                  side_effect=lambda *_args: sent.append(_args) or True),
            ):
                importer.process_seerr_watchlist(cfg, db)
            post = next(call for call in calls if call["method"] == "POST")
            self.assertEqual(post["payload"], {"tmdbId": 9876, "mediaType": "tv"})
            self.assertNotIn("seasons", post["payload"])
            self.assertNotIn("episodes", post["payload"])
            self.assertEqual(sent[0][3], "warning")
            self.assertEqual(sent[0][4], "warnings")
            db.close()

    def test_duplicate_409_is_accepted_only_after_local_watchlist_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_seerr_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            self.add_failed_pending(db, cfg)
            calls, sent = [], []
            duplicate = importer.HttpStatusError(
                409, "POST", "http://192.168.0.14:5055/api/v1/watchlist",
                '{"message":"Request for this media already exists."}',
            )
            FakeClient = self.fake_client_factory([
                duplicate,
                {"page": 1, "totalPages": 1, "results": [
                    {"tmdbId": 12345, "mediaType": "movie", "requestedBy": {"id": 1}}
                ]},
            ], calls)
            with (
                mock.patch.object(importer, "HttpClient", FakeClient),
                mock.patch.object(importer, "apprise_notify",
                                  side_effect=lambda _cfg, title, body, typ, tag: sent.append(
                                      {"title": title, "body": body, "type": typ, "tag": tag}
                                  ) or True),
            ):
                importer.process_seerr_watchlist(cfg, db)
            self.assertEqual([x["method"] for x in calls], ["POST", "GET"])
            self.assertIn("/api/v1/user/1/watchlist?page=1", calls[1]["url"])
            row = db.pending_rows(cfg.name)[0]
            self.assertEqual(row["seerr_watchlist_status"], "already_present")
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["type"], "warning")
            db.close()

    def test_409_without_local_requester_confirmation_is_a_critical_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_seerr_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            self.add_failed_pending(db, cfg)
            calls, sent = [], []
            duplicate = importer.HttpStatusError(409, "POST", "http://192.168.0.14:5055/api/v1/watchlist", "Something wrong")
            FakeClient = self.fake_client_factory([
                duplicate,
                {"page": 1, "totalPages": 1, "results": [
                    {"tmdbId": 12345, "mediaType": "movie"}  # Plex fallback, not a local Seerr entry
                ]},
                duplicate,
                {"page": 1, "totalPages": 1, "results": [
                    {"tmdbId": 12345, "mediaType": "movie"}  # still not a local Seerr entry
                ]},
            ], calls)
            with (
                mock.patch.object(importer, "HttpClient", FakeClient),
                mock.patch.object(importer, "apprise_notify",
                                  side_effect=lambda _cfg, title, body, typ, tag: sent.append(
                                      {"title": title, "body": body, "type": typ, "tag": tag}
                                  ) or True),
            ):
                importer.process_seerr_watchlist(cfg, db)
                importer.process_seerr_watchlist(cfg, db)
            row = db.pending_rows(cfg.name)[0]
            self.assertEqual(row["seerr_watchlist_status"], "error")
            errors = [item for item in sent if item["tag"] == "critical" and item["type"] == "failure"]
            self.assertEqual(len(errors), 1)  # unchanged error is not notified every timer run
            self.assertIn("not found", errors[0]["body"])
            self.assertNotIn("test-seerr-key", repr(sent))
            db.close()

    def test_missing_tmdb_id_skips_api_and_sends_one_critical_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_seerr_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            self.add_failed_pending(db, cfg, tmdb_id=None, media_title="Unknown Movie")
            sent = []
            with (
                mock.patch.object(importer, "HttpClient", self.fake_client_factory([], [])),
                mock.patch.object(importer, "apprise_notify",
                                  side_effect=lambda _cfg, title, body, typ, tag: sent.append(
                                      {"title": title, "body": body, "type": typ, "tag": tag}
                                  ) or True),
            ):
                importer.process_seerr_watchlist(cfg, db)
                importer.process_seerr_watchlist(cfg, db)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["tag"], "critical")
            self.assertEqual(sent[0]["type"], "failure")
            self.assertIn("TMDb ID is missing", sent[0]["body"])
            self.assertEqual(db.pending_rows(cfg.name)[0]["seerr_watchlist_status"], "missing_tmdb_id")
            db.close()

    def test_permanent_prepare_failure_persists_tmdb_id_and_is_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_seerr_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            db.record_terminal_event(
                event_key=f"{cfg.name}:{HASH1}:prepare",
                source=cfg.name, arr_type="radarr", category="movie",
                arr_media_id=111, media_title="Prepare Failure",
                release_title="Prepare.Failure.Release", title_slug="prepare-failure",
                stage="prepare", reason="HTTP 410 resolving download URL", tmdb_id=4567, tvdb_id=None,
            )
            calls, sent = [], []
            FakeClient = self.fake_client_factory([{"id": 101}], calls)
            with (
                mock.patch.object(importer, "HttpClient", FakeClient),
                mock.patch.object(importer, "apprise_notify",
                                  side_effect=lambda _cfg, title, body, typ, tag: sent.append(
                                      {"title": title, "body": body, "type": typ, "tag": tag}
                                  ) or True),
            ):
                importer.process_seerr_watchlist(cfg, db)
            self.assertEqual(calls[0]["payload"], {"tmdbId": 4567, "mediaType": "movie"})
            row = db.conn.execute(
                "SELECT tmdb_id, seerr_watchlist_status FROM terminal_events WHERE event_key = ?",
                (f"{cfg.name}:{HASH1}:prepare",),
            ).fetchone()
            self.assertEqual(row["tmdb_id"], "4567")
            self.assertEqual(row["seerr_watchlist_status"], "added")
            self.assertEqual(sent[0]["tag"], "warnings")
            db.close()

    def test_only_final_failures_for_current_arr_instance_are_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_seerr_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            self.add_failed_pending(db, cfg, torrent_hash=HASH1, tmdb_id=111)
            retry = make_grab(HASH2, 2, "Retry.Release")
            db.begin_pending(retry, cfg.name, {"title": "Retry Movie", "tmdbId": 222}, 120)
            db.fail_pending(HASH2, "temporary", 3, 1)
            # This unrelated terminal item belongs to Sonarr and must not be selected by the Radarr instance.
            db.record_terminal_event(
                event_key="sonarr-1080:unrelated:prepare", source="sonarr-1080",
                arr_type="sonarr", category="tv", arr_media_id=9,
                media_title="Unrelated", release_title="Unrelated.Release", title_slug=None,
                stage="prepare", reason="permanent", tmdb_id=333,
            )
            rows = db.seerr_watchlist_candidates(cfg.name)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["record_key"], HASH1)
            db.close()


class RadarrBlocklistTests(unittest.TestCase):
    def make_failed(self, root, *, arr_type="radarr", name="radarr-1080"):
        cfg = make_config(root)
        if arr_type == "sonarr":
            cfg = importer.replace(
                cfg, name=name, arr_type="sonarr", category="tv",
                radarr_blocklist_enabled=True,
            ).normalized()
        db = importer.StateDB(cfg.state_db)
        grab = make_grab(HASH1, 73, "Blocklist.Test.Release.1080p.WEB-DL")
        media = {
            "title": "Blocklist Test Movie",
            "titleSlug": "blocklist-test-movie",
        }
        db.begin_pending(grab, cfg.name, media, cfg.verification_timeout_sec)
        self.assertEqual(
            db.fail_pending(HASH1, "TorrServer metadata timeout", 1, 1),
            "failed",
        )
        return cfg, db

    def fake_arr(self, responses):
        calls = []

        class FakeArr:
            def request_json(_self, method, url, payload=None, api_key=False,
                             retry_count=0, retry_delay=1.0):
                calls.append({
                    "method": method,
                    "url": url,
                    "payload": payload,
                    "api_key": api_key,
                    "retry_count": retry_count,
                    "retry_delay": retry_delay,
                })
                if not responses:
                    raise AssertionError(f"Unexpected Arr API call: {method} {url}")
                value = responses.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        return FakeArr(), calls

    def test_blocklist_is_enabled_by_default(self):
        self.assertTrue(make_config(Path("/tmp")).radarr_blocklist_enabled)

    def test_toml_omission_defaults_radarr_blocklist_to_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "radarr.toml"
            config_path.write_text(
                f"""
name = "radarr-1080"
arr_type = "radarr"
arr_url = "http://127.0.0.1:7878"
arr_api_key = "test-key"
torrserver_url = "http://127.0.0.1:8092"
category = "movie"
target_quality = 1080
state_db = "{Path(tmp) / 'state.db'}"
""",
                encoding="utf-8",
            )
            self.assertTrue(importer.config_from_file(str(config_path)).radarr_blocklist_enabled)

    def test_terminal_failure_uses_native_radarr_failed_history_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp))
            arr, calls = self.fake_arr([None])
            try:
                importer.process_radarr_blocklist(cfg, arr, db)
                self.assertEqual(len(calls), 1)
                call = calls[0]
                self.assertEqual(call["method"], "POST")
                self.assertTrue(call["url"].endswith("/api/v3/history/failed/73"))
                self.assertTrue(call["api_key"])
                self.assertEqual(call["retry_count"], 0)
                row = db.pending_rows(cfg.name)[0]
                self.assertEqual(row["radarr_blocklist_status"], "blocked")
                self.assertEqual(row["radarr_blocklist_verify_misses"], 0)
            finally:
                db.close()

    def test_already_ambiguous_request_is_verified_without_resending_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp))
            db.set_radarr_blocklist_state(
                HASH1, "verify_pending", error="connection closed after request", verify_misses=0
            )
            release_title = db.pending_rows(cfg.name)[0]["title"]
            arr, calls = self.fake_arr([[{"sourceTitle": release_title, "id": 444}]])
            try:
                importer.process_radarr_blocklist(cfg, arr, db)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["method"], "GET")
                self.assertIn("/api/v3/blocklist/movie?movieId=", calls[0]["url"])
                row = db.pending_rows(cfg.name)[0]
                self.assertEqual(row["radarr_blocklist_status"], "already_present")
                self.assertEqual(row["radarr_blocklist_verify_misses"], 0)
            finally:
                db.close()

    def test_ambiguous_post_is_not_repeated_and_fails_after_three_empty_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp))
            db.set_radarr_blocklist_state(
                HASH1, "verify_pending", error="HTTP 502 after request", verify_misses=0
            )
            arr, calls = self.fake_arr([[], [], []])
            try:
                for _ in range(3):
                    importer.process_radarr_blocklist(cfg, arr, db)
                self.assertEqual([call["method"] for call in calls], ["GET", "GET", "GET"])
                row = db.pending_rows(cfg.name)[0]
                self.assertEqual(row["radarr_blocklist_status"], "failed")
                self.assertEqual(row["radarr_blocklist_verify_misses"], 3)
                self.assertIn("was ambiguous", row["radarr_blocklist_last_error"])
            finally:
                db.close()

    def test_permanent_radarr_api_error_is_logged_as_failed_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp))
            arr, calls = self.fake_arr([
                importer.HttpStatusError(
                    401, "POST", "http://127.0.0.1:7878/api/v3/history/failed/73", "Unauthorized"
                )
            ])
            try:
                importer.process_radarr_blocklist(cfg, arr, db)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["method"], "POST")
                row = db.pending_rows(cfg.name)[0]
                self.assertEqual(row["radarr_blocklist_status"], "failed")
                self.assertIn("HTTP 401", row["radarr_blocklist_last_error"])
            finally:
                db.close()

    def test_network_error_leaves_a_read_only_verification_state(self):
        from urllib.error import URLError

        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp))
            arr, calls = self.fake_arr([URLError("connection reset")])
            try:
                importer.process_radarr_blocklist(cfg, arr, db)
                row = db.pending_rows(cfg.name)[0]
                self.assertEqual(row["radarr_blocklist_status"], "verify_pending")
                self.assertEqual(row["radarr_blocklist_verify_misses"], 0)
                arr2, calls2 = self.fake_arr([[]])
                importer.process_radarr_blocklist(cfg, arr2, db)
                self.assertEqual([call["method"] for call in calls2], ["GET"])
                self.assertEqual(db.pending_rows(cfg.name)[0]["radarr_blocklist_status"], "verify_pending")
                self.assertEqual(calls[0]["method"], "POST")
            finally:
                db.close()

    def test_disabled_and_sonarr_profiles_do_not_call_radarr_blocklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp))
            arr, calls = self.fake_arr([])
            try:
                disabled = importer.replace(cfg, radarr_blocklist_enabled=False).normalized()
                importer.process_radarr_blocklist(disabled, arr, db)
                self.assertEqual(calls, [])
            finally:
                db.close()

        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp), arr_type="sonarr", name="sonarr-1080")
            arr, calls = self.fake_arr([])
            try:
                importer.process_radarr_blocklist(cfg, arr, db)
                self.assertEqual(calls, [])
                self.assertIsNone(db.pending_rows(cfg.name)[0]["radarr_blocklist_status"])
            finally:
                db.close()

    def test_terminal_preparation_events_are_not_blocklist_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            db.record_terminal_event(
                event_key=f"{cfg.name}:{HASH1}:prepare",
                source=cfg.name, arr_type="radarr", category="movie", arr_media_id=124,
                media_title="Preparation Failure", release_title="Preparation.Release",
                title_slug="preparation-failure", stage="prepare",
                reason="HTTP 410 resolving download URL",
            )
            arr, calls = self.fake_arr([])
            try:
                importer.process_radarr_blocklist(cfg, arr, db)
                self.assertEqual(calls, [])
            finally:
                db.close()

    def test_terminal_apprise_block_mentions_confirmed_outcome_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, db = self.make_failed(Path(tmp))
            try:
                row = db.pending_rows(cfg.name)[0]
                row["radarr_blocklist_status"] = "blocked"
                message = importer.terminal_notification_block(cfg, row)
                self.assertIn("Блок-лист Radarr: релиз добавлен", message)
                disabled = importer.replace(cfg, radarr_blocklist_enabled=False).normalized()
                self.assertNotIn(
                    "Блок-лист Radarr",
                    importer.terminal_notification_block(disabled, row),
                )
                self.assertNotIn(
                    "Блок-лист Radarr",
                    importer.terminal_notification_block(cfg, row, preparation=True),
                )
            finally:
                db.close()


class NotificationTests(unittest.TestCase):
    def make_notification_config(self, root):
        return importer.replace(
            make_config(root),
            arr_ui_url="http://192.168.0.14:7878",
            notifications_enabled=True,
            apprise_url="http://192.168.0.14:8000",
            apprise_config_key="arr-stack",
            service_name="arr2torr / Radarr 1080p",
            notification_timeout_sec=5,
            notification_tag_critical="critical",
            notification_tag_warning="warnings",
        ).normalized()

    def fake_apprise(self, captured):
        def send(request, timeout):
            captured.append({
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            })
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.getcode.return_value = 200
            response.read.return_value = b'{"status":"OK"}'
            return response
        return send

    def test_retry_warning_is_numeric_aggregated_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self.make_notification_config(root)
            db = importer.StateDB(cfg.state_db)
            one = make_grab(HASH1, 1, "Private.Release.Name.1080p")
            two = make_grab(HASH2, 2, "Another.Private.Release.Name.1080p")
            for grab in (one, two):
                db.begin_pending(grab, cfg.name, {"title": "Secret Film Title"}, 120)
                self.assertEqual(db.fail_pending(grab.hash, "metadata timeout", 3, 1), "retry_wait")

            sent = []
            with mock.patch.object(importer, "urlopen", side_effect=self.fake_apprise(sent)):
                importer.dispatch_notifications(cfg, db)
                self.assertEqual(len(sent), 1)
                importer.dispatch_notifications(cfg, db)
                self.assertEqual(len(sent), 1)

            payload = sent[0]["payload"]
            self.assertEqual(sent[0]["url"], "http://192.168.0.14:8000/notify/arr-stack")
            self.assertEqual(sent[0]["timeout"], 5)
            self.assertEqual(payload["tag"], "warnings")
            self.assertEqual(payload["type"], "warning")
            self.assertIn("2 проблемных раздач", payload["body"])
            self.assertNotIn("Secret Film Title", payload["body"])
            self.assertNotIn("Private.Release.Name", payload["body"])
            self.assertNotIn(HASH1, payload["body"])
            self.assertNotIn(HASH2, payload["body"])
            db.close()

    def test_terminal_failure_includes_title_release_and_arr_card_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_notification_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            grab = make_grab(HASH1, 1, "All.Good.Things.2010.1080p.WEB-DL")
            db.begin_pending(
                grab, cfg.name,
                {"title": "All Good Things", "titleSlug": "all-good-things-2010-603"},
                120,
            )
            self.assertEqual(
                db.fail_pending(HASH1, "verification deadline expired; metadata timeout", 1, 1),
                "failed",
            )
            second = make_grab(HASH2, 2, "Inside.Out.2.2024.1080p.WEB-DL")
            db.begin_pending(
                second, cfg.name,
                {"title": "Inside Out 2", "titleSlug": "inside-out-2-2024"},
                120,
            )
            self.assertEqual(
                db.fail_pending(HASH2, "verification deadline expired; metadata timeout", 1, 1),
                "failed",
            )
            sent = []
            with mock.patch.object(importer, "urlopen", side_effect=self.fake_apprise(sent)):
                importer.dispatch_notifications(cfg, db)
                self.assertEqual(len(sent), 1)
                importer.dispatch_notifications(cfg, db)
                self.assertEqual(len(sent), 1)

            payload = sent[0]["payload"]
            self.assertEqual(payload["tag"], "critical")
            self.assertEqual(payload["type"], "failure")
            self.assertIn("Терминальных ошибок в этой сводке: 2", payload["body"])
            self.assertIn("All Good Things", payload["body"])
            self.assertIn("All.Good.Things.2010.1080p.WEB-DL", payload["body"])
            self.assertIn("http://192.168.0.14:7878/movie/all-good-things-2010-603", payload["body"])
            self.assertIn("Inside Out 2", payload["body"])
            self.assertNotIn(HASH1, payload["body"])
            self.assertNotIn(HASH2, payload["body"])
            self.assertEqual(len(db.unnotified_terminal_failures(cfg.name)), 0)
            db.close()

    def test_permanent_prepare_failure_is_aggregated_without_leaking_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_notification_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            db.record_terminal_event(
                event_key=f"{cfg.name}:{HASH1}:prepare",
                source=cfg.name,
                arr_type="radarr",
                category="movie",
                arr_media_id=123,
                media_title="The Prestige",
                release_title="The.Prestige.1080p.WEB-DL",
                title_slug="the-prestige-2006",
                stage="prepare",
                reason="HTTP 410 resolving download URL https://indexer.invalid/?apikey=do-not-send",
            )
            sent = []
            with mock.patch.object(importer, "urlopen", side_effect=self.fake_apprise(sent)):
                importer.dispatch_notifications(cfg, db)

            self.assertEqual(len(sent), 1)
            payload = sent[0]["payload"]
            self.assertEqual(payload["tag"], "critical")
            self.assertIn("The Prestige", payload["body"])
            self.assertIn("HTTP 410 resolving download URL", payload["body"])
            self.assertNotIn("indexer.invalid", payload["body"])
            self.assertNotIn("do-not-send", payload["body"])
            self.assertNotIn(HASH1, payload["body"])
            self.assertEqual(db.unnotified_terminal_events(cfg.name), [])
            db.close()

    def test_apprise_failure_does_not_raise_or_retry_forever(self):
        from urllib.error import URLError

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_notification_config(Path(tmp))
            db = importer.StateDB(cfg.state_db)
            grab = make_grab(HASH1, 1, "Example.Release.1080p")
            db.begin_pending(grab, cfg.name, {"title": "Example Film"}, 120)
            db.fail_pending(HASH1, "metadata timeout", 1, 1)

            with mock.patch.object(importer, "urlopen", side_effect=URLError("offline")) as send:
                importer.dispatch_notifications(cfg, db)
                self.assertEqual(len(db.unnotified_terminal_failures(cfg.name)), 0)
                importer.dispatch_notifications(cfg, db)
                self.assertEqual(send.call_count, 1)
            db.close()

    def test_notification_disabled_makes_no_http_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = importer.replace(make_config(Path(tmp)), notifications_enabled=False)
            db = importer.StateDB(cfg.state_db)
            grab = make_grab(HASH1, 1, "Example.Release.1080p")
            db.begin_pending(grab, cfg.name, {"title": "Example Film"}, 120)
            db.fail_pending(HASH1, "metadata timeout", 1, 1)

            with mock.patch.object(importer, "urlopen") as send:
                importer.dispatch_notifications(cfg, db)
                send.assert_not_called()
            # Disabled means no delivery attempt; the new failure remains queued
            # for a later run after notifications are enabled.
            self.assertEqual(len(db.unnotified_terminal_failures(cfg.name)), 1)

            enabled = self.make_notification_config(Path(tmp))
            sent = []
            with mock.patch.object(importer, "urlopen", side_effect=self.fake_apprise(sent)):
                importer.dispatch_notifications(enabled, db)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["payload"]["tag"], "critical")
            self.assertEqual(db.unnotified_terminal_failures(cfg.name), [])
            db.close()

    def test_url_and_hash_are_redacted_from_notification_causes(self):
        message = importer.safe_notification_text(
            "HTTP 410 from https://indexer.invalid/?apikey=secret "
            "magnet:?xt=urn:btih:" + HASH1 + " " + HASH1
        )
        self.assertNotIn("indexer.invalid", message)
        self.assertNotIn("secret", message)
        self.assertNotIn(HASH1, message)
        self.assertIn("<URL-redacted>", message)
        self.assertIn("<hash-redacted>", message)

if __name__ == "__main__":
    unittest.main()
