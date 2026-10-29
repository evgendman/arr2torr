#!/usr/bin/env python3
"""torr-arr-importer v1

Polling synchronizer between a Radarr/Sonarr history and TorrServer.
No webhook and no blackhole dependency.
Standard-library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sqlite3
import fcntl
import hashlib
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


VERSION = "1.3.5"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_TIMEOUT = 30


class ImporterError(RuntimeError):
    pass


class HttpStatusError(ImporterError):
    def __init__(self, code: int, method: str, url: str, body: str = "") -> None:
        self.code = code
        self.method = method
        self.url = url
        self.body = body
        super().__init__(f"HTTP {code} {method} {redact_url(url)}: {body[:500]}")


class NetworkError(ImporterError):
    pass


def redact_url(url: str) -> str:
    # Never expose API keys or other query credentials in logs/exceptions.
    return re.sub(r"([?&](?:apikey|api_key|key)=)[^&]*", r"\1<redacted>", url, flags=re.IGNORECASE)


def is_retryable_http(code: int) -> bool:
    return code == 408 or code == 429 or 500 <= code <= 599


class NoRedirectHandler(HTTPRedirectHandler):
    def _no_redirect(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = _no_redirect
    http_error_302 = _no_redirect
    http_error_303 = _no_redirect
    http_error_307 = _no_redirect
    http_error_308 = _no_redirect


NO_REDIRECT_OPENER = build_opener(NoRedirectHandler())


@dataclass(frozen=True)
class Config:
    name: str
    arr_type: str               # radarr | sonarr
    arr_url: str
    arr_api_key: str
    torrserver_url: str
    category: str               # movie | tv
    target_quality: int         # 1080 | 2160 (metadata only)
    state_db: str
    history_page_size: int = DEFAULT_PAGE_SIZE
    timeout: int = DEFAULT_TIMEOUT
    user_agent: str = "torr-arr-importer/1.3.5"
    log_level: str = "INFO"
    log_file: str = ""
    retry_count: int = 2
    retry_delay: float = 1.0
    resolve_download_url: bool = True
    lock_file: str = ""
    verification_timeout_sec: int = 120
    verification_poll_interval_sec: float = 3.0
    retry_backoff_sec: int = 60
    max_add_attempts: int = 3
    radarr_blocklist_enabled: bool = True
    sonarr_blocklist_enabled: bool = True
    arr_ui_url: str = ""
    notifications_enabled: bool = False
    apprise_url: str = ""
    apprise_config_key: str = "arr-stack"
    service_name: str = ""
    notification_timeout_sec: int = 5
    notification_tag_critical: str = "critical"
    notification_tag_warning: str = "warnings"
    seerr_watchlist_enabled: bool = False
    seerr_url: str = ""
    seerr_api_key: str = ""

    def normalized(self) -> "Config":
        arr_type = self.arr_type.strip().lower()
        category = self.category.strip().lower()
        if arr_type not in {"radarr", "sonarr"}:
            raise ValueError("arr_type must be 'radarr' or 'sonarr'")
        expected = {"radarr": "movie", "sonarr": "tv"}[arr_type]
        if category != expected:
            raise ValueError(f"category={category!r} does not match arr_type={arr_type!r}; expected {expected!r}")
        if self.target_quality not in (1080, 2160):
            raise ValueError("target_quality must be 1080 or 2160")
        lock_file = self.lock_file.strip() or f"/var/lib/torr-arr-importer/{self.name}.lock"
        if self.history_page_size < 1 or self.history_page_size > 1000:
            raise ValueError("history_page_size must be between 1 and 1000")
        if not 10 <= self.verification_timeout_sec <= 900:
            raise ValueError("verification_timeout_sec must be between 10 and 900")
        if not 0.2 <= self.verification_poll_interval_sec <= 30:
            raise ValueError("verification_poll_interval_sec must be between 0.2 and 30")
        if not 1 <= self.retry_backoff_sec <= 86400:
            raise ValueError("retry_backoff_sec must be between 1 and 86400")
        if not 1 <= self.max_add_attempts <= 10:
            raise ValueError("max_add_attempts must be between 1 and 10")
        if not 1 <= self.notification_timeout_sec <= 30:
            raise ValueError("notification_timeout_sec must be between 1 and 30")

        arr_ui_url = self.arr_ui_url.strip().rstrip("/")
        apprise_url = self.apprise_url.strip().rstrip("/")
        seerr_url = self.seerr_url.strip().rstrip("/")
        seerr_api_key = self.seerr_api_key.strip()
        for label, value in (
            ("arr_ui_url", arr_ui_url),
            ("apprise_url", apprise_url),
            ("seerr_url", seerr_url),
        ):
            if not value:
                continue
            parts = urlsplit(value)
            if (
                parts.scheme not in {"http", "https"} or not parts.netloc
                or parts.username or parts.password or parts.query or parts.fragment
            ):
                raise ValueError(
                    f"{label} must be an HTTP(S) base URL without credentials, query or fragment"
                )

        config_key = self.apprise_config_key.strip()
        critical_tag = self.notification_tag_critical.strip()
        warning_tag = self.notification_tag_warning.strip()
        service_name = self.service_name.strip() or f"arr2torr / {self.name}"
        if self.notifications_enabled:
            if not apprise_url:
                raise ValueError("apprise_url is required when notifications_enabled=true")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", config_key):
                raise ValueError("apprise_config_key must contain only letters, digits, '_' or '-'")
            if not critical_tag or not warning_tag:
                raise ValueError("notification tags must not be empty")
        if self.seerr_watchlist_enabled:
            if not seerr_url:
                raise ValueError("seerr_url is required when seerr_watchlist_enabled=true")
            if not seerr_api_key or seerr_api_key.lower() in {
                "put_seerr_api_key_here", "changeme", "replace_me", "your_api_key"
            }:
                raise ValueError("seerr_api_key must contain a real key when seerr_watchlist_enabled=true")
            if not self.notifications_enabled:
                raise ValueError("notifications_enabled must be true when seerr_watchlist_enabled=true")
        return replace(
            self, arr_type=arr_type, category=category, lock_file=lock_file,
            arr_ui_url=arr_ui_url, apprise_url=apprise_url, seerr_url=seerr_url,
            seerr_api_key=seerr_api_key,
            apprise_config_key=config_key, service_name=service_name,
            notification_tag_critical=critical_tag, notification_tag_warning=warning_tag,
        )


@dataclass(frozen=True)
class Grab:
    history_id: int
    movie_id: int | None
    series_id: int | None
    source_title: str
    date: str | None
    data: dict[str, Any]
    quality: dict[str, Any]
    languages: list[dict[str, Any]]
    custom_formats: list[dict[str, Any]]
    custom_format_score: int | None
    download_id: str | None

    @property
    def hash(self) -> str:
        value = self.data.get("torrentInfoHash")
        return str(value).strip().lower() if value else ""

    @property
    def download_url(self) -> str:
        return str(self.data.get("downloadUrl") or "").strip()

    @property
    def arr_media_id(self) -> int | None:
        return self.movie_id if self.movie_id is not None else self.series_id


class HttpClient:
    def __init__(self, timeout: int, user_agent: str, api_key: str | None = None,
                 basic_user: str | None = None, basic_password: str | None = None) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.api_key = api_key
        self.basic_user = basic_user
        self.basic_password = basic_password

    def _request_once(self, method: str, url: str, payload: Any | None = None,
                      api_key: bool = False) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if api_key and self.api_key:
            headers["X-Api-Key"] = self.api_key
        if self.basic_user is not None:
            token = base64.b64encode(f"{self.basic_user}:{self.basic_password or ''}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            raise HttpStatusError(exc.code, method, url, body) from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise NetworkError(f"Network error {method} {redact_url(url)}: {reason}") from exc
        except json.JSONDecodeError as exc:
            raise ImporterError(f"Invalid JSON response from {method} {url}") from exc

    def request_json(self, method: str, url: str, payload: Any | None = None,
                     api_key: bool = False, retry_count: int = 0, retry_delay: float = 1.0) -> Any:
        attempts = max(0, int(retry_count)) + 1
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._request_once(method, url, payload=payload, api_key=api_key)
            except HttpStatusError as exc:
                last = exc
                # Do not retry permanent client/authentication errors such as 401/403.
                if not is_retryable_http(exc.code) or attempt >= attempts:
                    raise
                delay = max(0.0, retry_delay)
                logging.warning(
                    "RETRY method=%s url=%s status=%d attempt=%d/%d delay=%.1fs",
                    method, redact_url(url), exc.code, attempt + 1, attempts, delay
                )
                time.sleep(delay)
            except (URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt >= attempts:
                    raise
                delay = max(0.0, retry_delay)
                logging.warning(
                    "RETRY method=%s url=%s error=%s attempt=%d/%d delay=%.1fs",
                    method, redact_url(url), exc, attempt + 1, attempts, delay
                )
                time.sleep(delay)
            except ImporterError as exc:
                # Non-HTTP application/data errors are not made better by retrying.
                raise
        assert last is not None
        raise last


    def resolve_link(self, url: str, max_redirects: int = 5) -> str:
        """Resolve HTTP(S) download URLs to a magnet when the endpoint redirects to one.

        This prevents a Prowlarr API key embedded in the download URL from being passed
        onward to TorrServer (which may log its link). Non-redirect HTTP(S) URLs are
        returned unchanged so TorrServer can handle direct .torrent URLs itself.
        """
        current = url
        for _ in range(max_redirects + 1):
            if current.lower().startswith("magnet:"):
                return current
            request = Request(current, headers={"User-Agent": self.user_agent}, method="GET")
            try:
                with NO_REDIRECT_OPENER.open(request, timeout=self.timeout) as response:
                    # A direct HTTP(S) response is left for TorrServer to consume.
                    return current
            except HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise ImporterError(f"HTTP {exc.code} resolving download URL") from exc
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise ImporterError("HTTP redirect without Location while resolving download URL")
                current = location
        raise ImporterError("Too many redirects while resolving download URL")


class StateDB:
    """Shared success history plus restart-safe state for asynchronous add attempts."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Different profile services share one database. Lock before opening the
        # connection so concurrent first-use PRAGMA/schema initialization is safe.
        init_lock_path = self.path.with_name(self.path.name + ".init.lock")
        with init_lock_path.open("a") as init_lock:
            fcntl.flock(init_lock.fileno(), fcntl.LOCK_EX)
            try:
                self.conn = sqlite3.connect(str(self.path), timeout=30)
                self.conn.row_factory = sqlite3.Row
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self._init()
            except Exception:
                if hasattr(self, "conn"):
                    self.conn.close()
                raise
            finally:
                fcntl.flock(init_lock.fileno(), fcntl.LOCK_UN)

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
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
            CREATE INDEX IF NOT EXISTS idx_imports_source ON imports(source);
            CREATE TABLE IF NOT EXISTS pending_adds (
                hash TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                arr_history_id INTEGER,
                arr_media_id INTEGER,
                download_id TEXT,
                title TEXT,
                media_title TEXT,
                title_slug TEXT,
                imdb_id TEXT,
                tmdb_id TEXT,
                tvdb_id TEXT,
                requested_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                last_checked_at TEXT,
                last_status TEXT,
                metadata_activation_attempt INTEGER,
                last_error TEXT,
                next_retry_at TEXT,
                notification_attempted_at TEXT,
                notification_attempt_count INTEGER NOT NULL DEFAULT 0,
                notification_next_retry_at TEXT,
                notification_last_error TEXT,
                seerr_watchlist_status TEXT,
                seerr_watchlist_last_error TEXT,
                seerr_watchlist_error_fingerprint TEXT,
                seerr_watchlist_error_notified_fingerprint TEXT,
                seerr_watchlist_warning_notified_at TEXT,
                radarr_blocklist_status TEXT,
                radarr_blocklist_last_error TEXT,
                radarr_blocklist_verify_misses INTEGER NOT NULL DEFAULT 0,
                sonarr_blocklist_status TEXT,
                sonarr_blocklist_last_error TEXT,
                sonarr_blocklist_verify_misses INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_pending_source_status
                ON pending_adds(source, status);
            CREATE TABLE IF NOT EXISTS notification_state (
                state_key TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS terminal_events (
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
                tmdb_id TEXT,
                tvdb_id TEXT,
                created_at TEXT NOT NULL,
                notification_attempted_at TEXT,
                notification_attempt_count INTEGER NOT NULL DEFAULT 0,
                notification_next_retry_at TEXT,
                notification_last_error TEXT,
                seerr_watchlist_status TEXT,
                seerr_watchlist_last_error TEXT,
                seerr_watchlist_error_fingerprint TEXT,
                seerr_watchlist_error_notified_fingerprint TEXT,
                seerr_watchlist_warning_notified_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_terminal_events_source
                ON terminal_events(source, notification_attempted_at);
            """
        )
        # Multiple importer profiles share this database and may start together.
        # Serialize schema inspection + ALTER TABLE so another process cannot add
        # a column after this process inspected the old schema.
        self.conn.execute("BEGIN IMMEDIATE")
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(pending_adds)").fetchall()
        }
        added_notification_marker = "notification_attempted_at" not in columns
        added_radarr_blocklist_marker = "radarr_blocklist_status" not in columns
        added_sonarr_blocklist_marker = "sonarr_blocklist_status" not in columns
        migrations = {
            "media_title": "TEXT",
            "title_slug": "TEXT",
            "metadata_activation_attempt": "INTEGER",
            "notification_attempted_at": "TEXT",
            "notification_attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "notification_next_retry_at": "TEXT",
            "notification_last_error": "TEXT",
            "seerr_watchlist_status": "TEXT",
            "seerr_watchlist_last_error": "TEXT",
            "seerr_watchlist_error_fingerprint": "TEXT",
            "seerr_watchlist_error_notified_fingerprint": "TEXT",
            "seerr_watchlist_warning_notified_at": "TEXT",
            "radarr_blocklist_status": "TEXT",
            "radarr_blocklist_last_error": "TEXT",
            "radarr_blocklist_verify_misses": "INTEGER NOT NULL DEFAULT 0",
            "sonarr_blocklist_status": "TEXT",
            "sonarr_blocklist_last_error": "TEXT",
            "sonarr_blocklist_verify_misses": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, column_type in migrations.items():
            if column not in columns:
                self.conn.execute(f"ALTER TABLE pending_adds ADD COLUMN {column} {column_type}")
        terminal_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(terminal_events)").fetchall()
        }
        terminal_migrations = {
            "tmdb_id": "TEXT",
            "tvdb_id": "TEXT",
            "notification_attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "notification_next_retry_at": "TEXT",
            "notification_last_error": "TEXT",
            "seerr_watchlist_status": "TEXT",
            "seerr_watchlist_last_error": "TEXT",
            "seerr_watchlist_error_fingerprint": "TEXT",
            "seerr_watchlist_error_notified_fingerprint": "TEXT",
            "seerr_watchlist_warning_notified_at": "TEXT",
        }
        for column, column_type in terminal_migrations.items():
            if column not in terminal_columns:
                self.conn.execute(f"ALTER TABLE terminal_events ADD COLUMN {column} {column_type}")
        if added_notification_marker:
            # Suppress notifications for failures that predate this feature.
            self.conn.execute(
                "UPDATE pending_adds SET notification_attempted_at = ? WHERE status = 'failed'",
                (datetime.now(timezone.utc).isoformat(),),
            )
        if added_radarr_blocklist_marker:
            # Do not retrospectively mark a batch of pre-upgrade failed rows as failed in Radarr.
            self.conn.execute(
                "UPDATE pending_adds SET radarr_blocklist_status = 'legacy_skipped' WHERE status = 'failed'"
            )
        if added_sonarr_blocklist_marker:
            # Do not retroactively invoke Sonarr for terminal failures that predate this feature.
            self.conn.execute(
                "UPDATE pending_adds SET sonarr_blocklist_status = 'legacy_skipped' WHERE status = 'failed'"
            )
        self.conn.commit()

    def contains(self, torrent_hash: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM imports WHERE hash = ?", (torrent_hash,)).fetchone()
        return row is not None

    def all_hashes(self) -> set[str]:
        rows = self.conn.execute("SELECT hash FROM imports").fetchall()
        return {str(row["hash"]).lower() for row in rows}

    def add(self, grab: Grab, source: str, media: dict[str, Any]) -> None:
        """Legacy helper retained for callers/tests; production adds use begin/confirm_pending."""
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO imports
                  (hash, imported_at, source, arr_history_id, arr_media_id, download_id,
                   title, imdb_id, tmdb_id, tvdb_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grab.hash,
                    iso_now(),
                    source,
                    grab.history_id,
                    grab.arr_media_id,
                    str(grab.data.get("downloadId") or grab.download_id or "") or None,
                    grab.source_title,
                    str(media.get("imdbId") or "") or None,
                    str(media.get("tmdbId") or "") or None,
                    str(media.get("tvdbId") or "") or None,
                ),
            )

    def pending_rows(self, source: str | None = None) -> list[dict[str, Any]]:
        if source is None:
            rows = self.conn.execute("SELECT * FROM pending_adds ORDER BY requested_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pending_adds WHERE source = ? ORDER BY requested_at", (source,)
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_hashes(self) -> set[str]:
        rows = self.conn.execute("SELECT hash FROM pending_adds").fetchall()
        return {str(row["hash"]).strip().lower() for row in rows}

    def begin_pending(
        self,
        grab: Grab,
        source: str,
        media: dict[str, Any],
        verification_timeout_sec: int,
        *,
        allow_known: bool = False,
    ) -> int:
        """Atomically claim a hash and persist intent before the add request.

        Returns zero if another run already owns an unresolved attempt or if a
        retry is not due. A caller must not submit the add in that case.
        """
        now_dt = datetime.now(timezone.utc)
        requested_at = now_dt.isoformat()
        deadline_at = (now_dt + timedelta(seconds=verification_timeout_sec)).isoformat()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not allow_known:
                already_imported = self.conn.execute(
                    "SELECT 1 FROM imports WHERE hash = ?", (grab.hash,)
                ).fetchone()
                if already_imported:
                    self.conn.commit()
                    return 0
            existing = self.conn.execute(
                "SELECT attempt_count, status, next_retry_at FROM pending_adds WHERE hash = ?",
                (grab.hash,),
            ).fetchone()
            if existing:
                existing_status = str(existing["status"] or "")
                if existing_status in {"pending", "failed"}:
                    self.conn.commit()
                    return 0
                if existing_status == "retry_wait":
                    due = parse_iso_datetime(existing["next_retry_at"])
                    if due is None or due > now_dt:
                        self.conn.commit()
                        return 0
                attempt_count = int(existing["attempt_count"]) + 1
            else:
                attempt_count = 1

            self.conn.execute(
                """
                INSERT INTO pending_adds
                  (hash, source, arr_history_id, arr_media_id, download_id, title,
                   media_title, title_slug, imdb_id, tmdb_id, tvdb_id, requested_at,
                   deadline_at, attempt_count, status, last_checked_at, last_status,
                   last_error, next_retry_at, notification_attempted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL,
                        'request-started', NULL, NULL, NULL)
                ON CONFLICT(hash) DO UPDATE SET
                  source=excluded.source,
                  arr_history_id=excluded.arr_history_id,
                  arr_media_id=excluded.arr_media_id,
                  download_id=excluded.download_id,
                  title=excluded.title,
                  media_title=excluded.media_title,
                  title_slug=excluded.title_slug,
                  imdb_id=excluded.imdb_id,
                  tmdb_id=excluded.tmdb_id,
                  tvdb_id=excluded.tvdb_id,
                  requested_at=excluded.requested_at,
                  deadline_at=excluded.deadline_at,
                  attempt_count=excluded.attempt_count,
                  status='pending',
                  last_checked_at=NULL,
                  last_status='request-started',
                  last_error=NULL,
                  next_retry_at=NULL,
                  notification_attempted_at=NULL,
                  seerr_watchlist_status=NULL,
                  seerr_watchlist_last_error=NULL,
                  seerr_watchlist_error_fingerprint=NULL,
                  seerr_watchlist_error_notified_fingerprint=NULL,
                  seerr_watchlist_warning_notified_at=NULL,
                  radarr_blocklist_status=NULL,
                  radarr_blocklist_last_error=NULL,
                  radarr_blocklist_verify_misses=0,
                  sonarr_blocklist_status=NULL,
                  sonarr_blocklist_last_error=NULL,
                  sonarr_blocklist_verify_misses=0
                """,
                (
                    grab.hash, source, grab.history_id, grab.arr_media_id,
                    str(grab.data.get("downloadId") or grab.download_id or "") or None,
                    grab.source_title,
                    str(media.get("title") or "") or None,
                    str(media.get("titleSlug") or "") or None,
                    str(media.get("imdbId") or "") or None,
                    str(media.get("tmdbId") or "") or None,
                    str(media.get("tvdbId") or "") or None,
                    requested_at, deadline_at, attempt_count,
                ),
            )
            self.conn.commit()
            return attempt_count
        except Exception:
            self.conn.rollback()
            raise

    def metadata_activation_attempt(self, torrent_hash: str) -> int | None:
        row = self.conn.execute(
            "SELECT metadata_activation_attempt FROM pending_adds WHERE hash = ?",
            (torrent_hash,),
        ).fetchone()
        return as_int(row["metadata_activation_attempt"]) if row is not None else None

    def mark_metadata_activation_attempt(self, torrent_hash: str, attempt_count: int) -> bool:
        """Atomically allow one DB-only metadata activation request per verification attempt."""
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE pending_adds
                SET metadata_activation_attempt = ?, last_checked_at = ?,
                    last_status = 'metadata-activation-requested'
                WHERE hash = ? AND status = 'pending' AND attempt_count = ?
                  AND (metadata_activation_attempt IS NULL OR metadata_activation_attempt != ?)
                """,
                (attempt_count, iso_now(), torrent_hash, attempt_count, attempt_count),
            )
        return cur.rowcount == 1

    def observe_pending(
        self,
        torrent_hash: str,
        observed_status: str,
        error: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE pending_adds
                SET last_checked_at = ?, last_status = ?, last_error = COALESCE(?, last_error)
                WHERE hash = ? AND status = 'pending'
                """,
                (iso_now(), observed_status[:200], error[:1000] if error else None, torrent_hash),
            )

    def resume_verification(self, torrent_hash: str, verification_timeout_sec: int, observed_status: str) -> None:
        """Resume observation of an already-present hash without resending its magnet."""
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=verification_timeout_sec)).isoformat()
        with self.conn:
            self.conn.execute(
                """
                UPDATE pending_adds
                SET status = 'pending', deadline_at = ?, last_checked_at = ?,
                    last_status = ?, last_error = NULL, next_retry_at = NULL,
                    notification_attempted_at = NULL,
                    radarr_blocklist_status = NULL,
                    radarr_blocklist_last_error = NULL,
                    radarr_blocklist_verify_misses = 0,
                     sonarr_blocklist_status = NULL,
                     sonarr_blocklist_last_error = NULL,
                     sonarr_blocklist_verify_misses = 0
                WHERE hash = ? AND status IN ('retry_wait', 'failed')
                """,
                (deadline, iso_now(), observed_status[:200], torrent_hash),
            )

    def fail_pending(
        self,
        torrent_hash: str,
        reason: str,
        max_attempts: int,
        retry_backoff_sec: int,
        *,
        permanent: bool = False,
    ) -> str:
        row = self.conn.execute(
            "SELECT attempt_count FROM pending_adds WHERE hash = ?", (torrent_hash,)
        ).fetchone()
        if row is None:
            return "missing"
        attempts = int(row["attempt_count"])
        retry = not permanent and attempts < max_attempts
        status = "retry_wait" if retry else "failed"
        if retry:
            backoff = min(3600, retry_backoff_sec * (2 ** max(0, attempts - 1)))
            next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat()
        else:
            next_retry_at = None
        with self.conn:
            self.conn.execute(
                """
                UPDATE pending_adds
                SET status = ?, last_checked_at = ?, last_status = 'verification-failed',
                    last_error = ?, next_retry_at = ?
                WHERE hash = ?
                """,
                (status, iso_now(), reason[:1000], next_retry_at, torrent_hash),
            )
        return status

    def confirm_pending(self, torrent_hash: str) -> dict[str, Any] | None:
        """Atomically promote a verified pending attempt into successful import history."""
        row = self.conn.execute(
            "SELECT * FROM pending_adds WHERE hash = ?", (torrent_hash,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO imports
                  (hash, imported_at, source, arr_history_id, arr_media_id, download_id,
                   title, imdb_id, tmdb_id, tvdb_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value["hash"], iso_now(), value["source"], value["arr_history_id"],
                    value["arr_media_id"], value["download_id"], value["title"],
                    value["imdb_id"], value["tmdb_id"], value["tvdb_id"],
                ),
            )
            self.conn.execute("DELETE FROM pending_adds WHERE hash = ?", (torrent_hash,))
        return value

    def rearm_failed_for_new_grab(
        self, grab: Grab, source: str, verification_timeout_sec: int,
    ) -> bool:
        """Re-arm a terminal failure only for a newer Radarr Grab of the same hash."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        deadline_text = (now + timedelta(seconds=verification_timeout_sec)).isoformat()
        download_id = str(grab.data.get("downloadId") or grab.download_id or "") or None
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE pending_adds
                SET arr_history_id = ?, arr_media_id = ?, download_id = ?, title = ?,
                    requested_at = ?, deadline_at = ?, attempt_count = 0,
                    metadata_activation_attempt = NULL,
                    status = 'retry_wait', last_checked_at = NULL,
                    last_status = 'new-radarr-grab', last_error = NULL,
                    next_retry_at = ?,
                    notification_attempted_at = NULL, notification_attempt_count = 0,
                    notification_next_retry_at = NULL, notification_last_error = NULL,
                    seerr_watchlist_status = NULL, seerr_watchlist_last_error = NULL,
                    seerr_watchlist_error_fingerprint = NULL,
                    seerr_watchlist_error_notified_fingerprint = NULL,
                    seerr_watchlist_warning_notified_at = NULL,
                    radarr_blocklist_status = NULL, radarr_blocklist_last_error = NULL,
                    radarr_blocklist_verify_misses = 0,
                     sonarr_blocklist_status = NULL,
                     sonarr_blocklist_last_error = NULL,
                     sonarr_blocklist_verify_misses = 0
                WHERE hash = ? AND source = ? AND status = 'failed'
                  AND COALESCE(arr_history_id, 0) < ?
                """,
                (
                    grab.history_id, grab.arr_media_id, download_id, grab.source_title,
                    now_text, deadline_text, now_text, grab.hash, source, grab.history_id,
                ),
            )
        return cur.rowcount == 1

    def requeue_failed(self, source: str) -> int:
        """Explicitly re-arm exhausted, never-confirmed attempts for operator-directed retry."""
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE pending_adds
                SET status = 'retry_wait', attempt_count = 0, next_retry_at = ?,
                    metadata_activation_attempt = NULL,
                    last_error = 'Manually re-queued with --retry-failed',
                    notification_attempted_at = NULL,
                    seerr_watchlist_status = NULL,
                    seerr_watchlist_last_error = NULL,
                    seerr_watchlist_error_fingerprint = NULL,
                    seerr_watchlist_error_notified_fingerprint = NULL,
                    seerr_watchlist_warning_notified_at = NULL,
                    radarr_blocklist_status = NULL,
                    radarr_blocklist_last_error = NULL,
                    radarr_blocklist_verify_misses = 0,
                     sonarr_blocklist_status = NULL,
                     sonarr_blocklist_last_error = NULL,
                     sonarr_blocklist_verify_misses = 0
                WHERE source = ? AND status = 'failed'
                """,
                (iso_now(), source),
            )
        return cur.rowcount

    def radarr_blocklist_candidates(self, source: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM pending_adds
            WHERE source = ? AND status = 'failed'
              AND (radarr_blocklist_status IS NULL
                   OR radarr_blocklist_status IN ('retry', 'request_started', 'verify_pending'))
            ORDER BY requested_at
            """,
            (source,),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_radarr_blocklist_state(
        self,
        torrent_hash: str,
        status: str,
        *,
        error: str | None = None,
        verify_misses: int | None = None,
    ) -> None:
        with self.conn:
            if verify_misses is None:
                self.conn.execute(
                    """
                    UPDATE pending_adds
                    SET radarr_blocklist_status = ?, radarr_blocklist_last_error = ?
                    WHERE hash = ?
                    """,
                    (status, error[:1000] if error else None, torrent_hash),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE pending_adds
                    SET radarr_blocklist_status = ?, radarr_blocklist_last_error = ?,
                        radarr_blocklist_verify_misses = ?
                    WHERE hash = ?
                    """,
                    (status, error[:1000] if error else None, verify_misses, torrent_hash),
                )

    def sonarr_blocklist_candidates(self, source: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM pending_adds
            WHERE source = ? AND status = 'failed'
              AND (sonarr_blocklist_status IS NULL
                   OR sonarr_blocklist_status IN ('retry', 'request_started', 'verify_pending'))
            ORDER BY requested_at
            """,
            (source,),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_sonarr_blocklist_state(
        self,
        torrent_hash: str,
        status: str,
        *,
        error: str | None = None,
        verify_misses: int | None = None,
    ) -> None:
        with self.conn:
            if verify_misses is None:
                self.conn.execute(
                    """
                    UPDATE pending_adds
                    SET sonarr_blocklist_status = ?, sonarr_blocklist_last_error = ?
                    WHERE hash = ?
                    """,
                    (status, error[:1000] if error else None, torrent_hash),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE pending_adds
                    SET sonarr_blocklist_status = ?, sonarr_blocklist_last_error = ?,
                        sonarr_blocklist_verify_misses = ?
                    WHERE hash = ?
                    """,
                    (status, error[:1000] if error else None, verify_misses, torrent_hash),
                )

    def record_terminal_event(
        self,
        event_key: str,
        source: str,
        arr_type: str,
        category: str,
        arr_media_id: int | None,
        media_title: str | None,
        release_title: str | None,
        title_slug: str | None,
        stage: str,
        reason: str,
        tmdb_id: str | int | None = None,
        tvdb_id: str | int | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO terminal_events
                  (event_key, source, arr_type, category, arr_media_id, media_title,
                   release_title, title_slug, stage, attempt_count, max_attempts,
                   reason, tmdb_id, tvdb_id, created_at, notification_attempted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL)
                """,
                (
                    event_key, source, arr_type, category, arr_media_id, media_title,
                    release_title, title_slug, stage, reason[:1000],
                    str(tmdb_id).strip() if tmdb_id not in (None, "") else None,
                    str(tvdb_id).strip() if tvdb_id not in (None, "") else None,
                    iso_now(),
                ),
            )

    def unnotified_terminal_failures(self, source: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM pending_adds
            WHERE source = ? AND status = 'failed'
              AND notification_attempted_at IS NULL
              AND (notification_next_retry_at IS NULL OR notification_next_retry_at <= ?)
            ORDER BY requested_at
            """,
            (source, iso_now()),
        ).fetchall()
        return [
            {**dict(row), "record_table": "pending_adds", "record_key": str(row["hash"])}
            for row in rows
        ]

    def unnotified_terminal_events(self, source: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM terminal_events
            WHERE source = ? AND notification_attempted_at IS NULL
              AND (notification_next_retry_at IS NULL OR notification_next_retry_at <= ?)
            ORDER BY created_at
            """,
            (source, iso_now()),
        ).fetchall()
        return [
            {**dict(row), "record_table": "terminal_events", "record_key": str(row["event_key"])}
            for row in rows
        ]

    def mark_notification_delivered(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        now = iso_now()
        with self.conn:
            for record in records:
                table = str(record.get("record_table") or "")
                if table == "pending_adds":
                    key_column = "hash"
                elif table == "terminal_events":
                    key_column = "event_key"
                else:
                    raise ValueError("Unknown terminal-notification record table")
                self.conn.execute(
                    f"""
                    UPDATE {table}
                    SET notification_attempted_at = ?,
                        notification_next_retry_at = NULL,
                        notification_last_error = NULL
                    WHERE {key_column} = ? AND source = ?
                    """,
                    (now, str(record.get("record_key") or ""), str(record.get("source") or "")),
                )

    def schedule_notification_retry(self, records: list[dict[str, Any]], error: str) -> int:
        """Durably re-queue undelivered terminal notifications using bounded exponential backoff."""
        longest_delay = 0
        now = datetime.now(timezone.utc)
        with self.conn:
            for record in records:
                table = str(record.get("record_table") or "")
                if table == "pending_adds":
                    key_column = "hash"
                elif table == "terminal_events":
                    key_column = "event_key"
                else:
                    raise ValueError("Unknown terminal-notification record table")
                key = str(record.get("record_key") or "")
                source = str(record.get("source") or "")
                row = self.conn.execute(
                    f"SELECT notification_attempt_count FROM {table} WHERE {key_column} = ? AND source = ?",
                    (key, source),
                ).fetchone()
                if row is None:
                    continue
                attempt = int(row["notification_attempt_count"] or 0) + 1
                delay = notification_delivery_retry_delay(attempt)
                retry_at = (now + timedelta(seconds=delay)).isoformat()
                self.conn.execute(
                    f"""
                    UPDATE {table}
                    SET notification_attempt_count = ?,
                        notification_next_retry_at = ?,
                        notification_last_error = ?
                    WHERE {key_column} = ? AND source = ? AND notification_attempted_at IS NULL
                    """,
                    (attempt, retry_at, safe_notification_text(error, 100), key, source),
                )
                longest_delay = max(longest_delay, delay)
        return longest_delay

    def notification_fingerprint(self, state_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT fingerprint FROM notification_state WHERE state_key = ?",
            (state_key,),
        ).fetchone()
        return str(row["fingerprint"]) if row else None

    def set_notification_fingerprint(self, state_key: str, fingerprint: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO notification_state(state_key, fingerprint, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                  fingerprint=excluded.fingerprint, updated_at=excluded.updated_at
                """,
                (state_key, fingerprint, iso_now()),
            )

    def seerr_watchlist_candidates(self, source: str) -> list[dict[str, Any]]:
        pending = self.conn.execute(
            """
            SELECT 'pending_adds' AS record_table, hash AS record_key, source,
                   title AS release_title, media_title, tmdb_id, tvdb_id,
                   seerr_watchlist_status, seerr_watchlist_last_error,
                   seerr_watchlist_error_fingerprint,
                   seerr_watchlist_error_notified_fingerprint,
                   seerr_watchlist_warning_notified_at
            FROM pending_adds
            WHERE source = ? AND status = 'failed'
            """,
            (source,),
        ).fetchall()
        terminal = self.conn.execute(
            """
            SELECT 'terminal_events' AS record_table, event_key AS record_key, source,
                   release_title, media_title, tmdb_id, tvdb_id,
                   seerr_watchlist_status, seerr_watchlist_last_error,
                   seerr_watchlist_error_fingerprint,
                   seerr_watchlist_error_notified_fingerprint,
                   seerr_watchlist_warning_notified_at
            FROM terminal_events
            WHERE source = ?
            """,
            (source,),
        ).fetchall()
        result = [dict(row) for row in [*pending, *terminal]]
        result.sort(key=lambda row: str(row.get("record_key") or ""))
        return result

    def set_seerr_watchlist_state(
        self,
        record: dict[str, Any],
        status: str,
        *,
        error: str | None = None,
        error_fingerprint: str | None = None,
        clear_notified_error: bool = False,
    ) -> None:
        table = str(record.get("record_table") or "")
        if table == "pending_adds":
            key_column = "hash"
        elif table == "terminal_events":
            key_column = "event_key"
        else:
            raise ValueError("Unknown Seerr Watchlist state table")
        key = str(record.get("record_key") or "")
        if not key:
            raise ValueError("Missing Seerr Watchlist state key")
        with self.conn:
            self.conn.execute(
                f"""
                UPDATE {table}
                SET seerr_watchlist_status = ?,
                    seerr_watchlist_last_error = ?,
                    seerr_watchlist_error_fingerprint = ?,
                    seerr_watchlist_error_notified_fingerprint =
                      CASE WHEN ? THEN NULL ELSE seerr_watchlist_error_notified_fingerprint END
                WHERE {key_column} = ? AND source = ?
                """,
                (status, error[:1000] if error else None, error_fingerprint,
                 1 if clear_notified_error else 0, key, str(record.get("source") or "")),
            )

    def seerr_watchlist_warning_rows(self, source: str) -> list[dict[str, Any]]:
        return [
            row for row in self.seerr_watchlist_candidates(source)
            if row.get("seerr_watchlist_status") in {"added", "already_present"}
            and not row.get("seerr_watchlist_warning_notified_at")
        ]

    def seerr_watchlist_error_rows(self, source: str) -> list[dict[str, Any]]:
        return [
            row for row in self.seerr_watchlist_candidates(source)
            if row.get("seerr_watchlist_status") in {"error", "missing_tmdb_id"}
            and row.get("seerr_watchlist_error_fingerprint")
            and row.get("seerr_watchlist_error_fingerprint")
                != row.get("seerr_watchlist_error_notified_fingerprint")
        ]

    def mark_seerr_watchlist_warnings_notified(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        now = iso_now()
        with self.conn:
            for record in records:
                table = str(record.get("record_table") or "")
                key_column = "hash" if table == "pending_adds" else "event_key" if table == "terminal_events" else ""
                if not key_column:
                    raise ValueError("Unknown Seerr Watchlist state table")
                self.conn.execute(
                    f"UPDATE {table} SET seerr_watchlist_warning_notified_at = ? "
                    f"WHERE {key_column} = ? AND source = ?",
                    (now, str(record.get("record_key") or ""), str(record.get("source") or "")),
                )

    def mark_seerr_watchlist_errors_notified(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with self.conn:
            for record in records:
                table = str(record.get("record_table") or "")
                key_column = "hash" if table == "pending_adds" else "event_key" if table == "terminal_events" else ""
                if not key_column:
                    raise ValueError("Unknown Seerr Watchlist state table")
                self.conn.execute(
                    f"UPDATE {table} SET seerr_watchlist_error_notified_fingerprint = seerr_watchlist_error_fingerprint "
                    f"WHERE {key_column} = ? AND source = ?",
                    (str(record.get("record_key") or ""), str(record.get("source") or "")),
                )

    def close(self) -> None:
        self.conn.close()


def normalize_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def permanent_preparation_error(exc: Exception) -> bool:
    if isinstance(exc, HttpStatusError):
        return 400 <= exc.code < 500 and not is_retryable_http(exc.code)
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc), flags=re.IGNORECASE)
    if not match:
        return False
    code = int(match.group(1))
    return 400 <= code < 500 and not is_retryable_http(code)


def arr_card_url(cfg: Config, title_slug: str | None) -> str | None:
    if not cfg.arr_ui_url or not title_slug:
        return None
    parts = urlsplit(cfg.arr_ui_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        return None
    endpoint = "movie" if cfg.arr_type == "radarr" else "series"
    slug = quote(str(title_slug).strip(), safe="-._~")
    if not slug:
        return None
    base_path = parts.path.rstrip("/")
    path = f"{base_path}/{endpoint}/{slug}" if base_path else f"/{endpoint}/{slug}"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def safe_notification_text(value: Any, max_length: int = 220) -> str:
    text_value = str(value or "").replace("\x00", " ")
    text_value = re.sub(r"(?i)\b(?:api[_-]?key|token|password|authorization)\s*[:=]\s*\S+", "<secret-redacted>", text_value)
    text_value = re.sub(r"(?i)magnet:\?\S+", "<magnet-redacted>", text_value)
    text_value = re.sub(r"https?://\S+", "<URL-redacted>", text_value)
    text_value = re.sub(r"\b[a-fA-F0-9]{40,64}\b", "<hash-redacted>", text_value)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value[:max_length] or "не указана"


def apprise_notify(cfg: Config, title: str, body: str, notify_type: str, tag: str) -> bool:
    if not cfg.notifications_enabled:
        return False
    endpoint = cfg.apprise_url.rstrip("/") + "/notify/" + quote(cfg.apprise_config_key, safe="")
    payload = json.dumps(
        {"title": title, "body": body, "type": notify_type, "tag": tag},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": cfg.user_agent},
        method="POST",
    )
    try:
        with urlopen(request, timeout=cfg.notification_timeout_sec) as response:
            response.read(2048)
            status = int(response.getcode())
        if 200 <= status < 300:
            logging.info("APPRISE_SENT source=%s tag=%s status=%d", cfg.name, tag, status)
            return True
        logging.warning("APPRISE_FAILED source=%s tag=%s http_status=%d", cfg.name, tag, status)
    except HTTPError as exc:
        logging.warning("APPRISE_FAILED source=%s tag=%s http_status=%d", cfg.name, tag, exc.code)
    except Exception as exc:
        # Do not send a notification about notification delivery failures.
        logging.warning(
            "APPRISE_FAILED source=%s tag=%s error_type=%s",
            cfg.name, tag, type(exc).__name__,
        )
    return False


def seerr_error_fingerprint(error: str) -> str:
    normalized = safe_notification_text(error, 800).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def seerr_watchlist_contains(cfg: Config, client: HttpClient, tmdb_id: int, media_type: str) -> bool:
    """Verify a 409 against the local Seerr Watchlist for administrator user ID 1."""
    for page in range(1, 1001):
        url = normalize_url(cfg.seerr_url, f"/api/v1/user/1/watchlist?page={page}")
        response = client.request_json(
            "GET", url, api_key=True,
            retry_count=cfg.retry_count, retry_delay=cfg.retry_delay,
        )
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise ImporterError("Invalid response while verifying Seerr Watchlist duplicate")
        for item in response["results"]:
            if not isinstance(item, dict):
                continue
            requester = item.get("requestedBy")
            requester_id = requester.get("id") if isinstance(requester, dict) else requester
            if as_int(requester_id) != 1:
                continue
            if as_int(item.get("tmdbId")) == tmdb_id and str(
                item.get("mediaType") or item.get("type") or ""
            ).strip().lower() == media_type:
                return True
        total_pages = as_int(response.get("totalPages"))
        if total_pages is not None:
            if page >= max(1, total_pages):
                break
        elif not response["results"] or len(response["results"]) < 20:
            break
    else:
        raise ImporterError("Seerr Watchlist duplicate verification exceeded 1000 pages")
    return False


def _seerr_watchlist_record_error(
    cfg: Config, db: StateDB, record: dict[str, Any], status: str, error: str
) -> None:
    safe_error = safe_notification_text(error, 800)
    fingerprint = seerr_error_fingerprint(safe_error)
    unchanged = (
        record.get("seerr_watchlist_status") == status
        and record.get("seerr_watchlist_error_fingerprint") == fingerprint
    )
    db.set_seerr_watchlist_state(record, status, error=safe_error, error_fingerprint=fingerprint)
    if not unchanged:
        media_title = safe_notification_text(
            record.get("media_title") or record.get("release_title") or "Неизвестный элемент", 160
        )
        logging.error(
            "SEERR_WATCHLIST_FAILED source=%s type=%s title=%r tmdb=%s error=%s",
            cfg.name, cfg.category, media_title, record.get("tmdb_id") or "missing", safe_error,
        )
    else:
        logging.debug(
            "SEERR_WATCHLIST_RETRY source=%s type=%s tmdb=%s status=%s",
            cfg.name, cfg.category, record.get("tmdb_id") or "missing", status,
        )


def _seerr_watchlist_success(
    cfg: Config, db: StateDB, record: dict[str, Any], status: str
) -> None:
    db.set_seerr_watchlist_state(
        record, status, error=None, error_fingerprint=None, clear_notified_error=True,
    )
    logging.info(
        "SEERR_WATCHLIST_%s source=%s type=%s title=%r tmdb=%s",
        "ADDED" if status == "added" else "ALREADY_PRESENT",
        cfg.name, cfg.category,
        safe_notification_text(record.get("media_title") or record.get("release_title") or "Unknown", 160),
        record.get("tmdb_id"),
    )


def process_radarr_blocklist(cfg: Config, arr: HttpClient, db: StateDB) -> None:
    """Use Radarr's native failed-history action to blocklist terminally failed grabs."""
    if cfg.arr_type != "radarr":
        return
    if not cfg.radarr_blocklist_enabled:
        logging.info("RADARR_BLOCKLIST_SKIPPED source=%s reason=disabled", cfg.name)
        return

    def title_key(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    for row in db.radarr_blocklist_candidates(cfg.name):
        torrent_hash = str(row.get("hash") or "").strip().lower()
        media_id = as_int(row.get("arr_media_id"))
        history_id = as_int(row.get("arr_history_id"))
        release_title = str(row.get("title") or "").strip()
        media_title = safe_notification_text(
            row.get("media_title") or row.get("title") or "Unknown", 160
        )
        safe_release_title = safe_notification_text(release_title or "Unknown", 200)
        current_status = str(row.get("radarr_blocklist_status") or "")
        attempt_count = int(row.get("attempt_count") or 0)

        # A definitive terminal status can occur before the configured retry limit
        # (for example, a permanent API rejection). Only an ambiguous request already
        # in flight is verified regardless of count; do not blocklist a fresh item early.
        if current_status not in {"request_started", "verify_pending"} and attempt_count < cfg.max_add_attempts:
            reason = (
                f"attempt count {attempt_count}/{cfg.max_add_attempts}; "
                "configured attempt limit has not been exhausted"
            )
            db.set_radarr_blocklist_state(
                torrent_hash, "skipped_attempt_limit", error=reason, verify_misses=0
            )
            logging.warning(
                "RADARR_BLOCKLIST_SKIPPED source=%s media_id=%s history_id=%s title=%r release=%r attempts=%d/%d reason=attempt-limit-not-exhausted",
                cfg.name, media_id or "missing", history_id or "missing",
                media_title, safe_release_title, attempt_count, cfg.max_add_attempts,
            )
            continue

        if not torrent_hash or not media_id or not history_id or not release_title:
            reason = "required Radarr history/media/release identity is missing"
            db.set_radarr_blocklist_state(torrent_hash, "failed", error=reason)
            logging.error(
                "RADARR_BLOCKLIST_FAILED source=%s media_id=%s history_id=%s title=%r release=%r reason=%s",
                cfg.name, media_id or "missing", history_id or "missing",
                media_title, safe_release_title, reason,
            )
            continue

        # Once a non-idempotent failed-history request may have been sent, never resend
        # it blindly. Radarr's BlocklistResource does not expose the torrent infohash,
        # so recovery can only compare the exact source title for that movie.
        if current_status in {"request_started", "verify_pending"}:
            url = normalize_url(cfg.arr_url, "/api/v3/blocklist/movie") + "?" + urlencode(
                {"movieId": media_id}
            )
            try:
                response = arr.request_json(
                    "GET", url, api_key=True,
                    retry_count=cfg.retry_count, retry_delay=cfg.retry_delay,
                )
                if not isinstance(response, list):
                    raise ImporterError("Radarr movie blocklist response is not an array")
                found = any(
                    isinstance(item, dict)
                    and title_key(item.get("sourceTitle")) == title_key(release_title)
                    for item in response
                )
            except Exception as exc:
                safe_error = safe_notification_text(str(exc), 500)
                db.set_radarr_blocklist_state(
                    torrent_hash, "verify_pending", error=safe_error
                )
                logging.warning(
                    "RADARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r reason=lookup-error error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, safe_error,
                )
                continue

            if found:
                db.set_radarr_blocklist_state(
                    torrent_hash, "already_present", error=None, verify_misses=0
                )
                logging.info(
                    "RADARR_BLOCKLIST_ALREADY_PRESENT source=%s media_id=%s history_id=%s title=%r release=%r evidence=source-title-match_after_ambiguous_request",
                    cfg.name, media_id, history_id, media_title, safe_release_title,
                )
                continue

            misses = int(row.get("radarr_blocklist_verify_misses") or 0) + 1
            if misses >= 3:
                error = (
                    "Radarr accepted no verifiable blocklist evidence after three checks; "
                    "the request result was ambiguous, and it was not resent to avoid duplicate side effects"
                )
                db.set_radarr_blocklist_state(
                    torrent_hash, "failed", error=error, verify_misses=misses
                )
                logging.error(
                    "RADARR_BLOCKLIST_FAILED source=%s media_id=%s history_id=%s title=%r release=%r verify_misses=%d error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, misses, error,
                )
            else:
                db.set_radarr_blocklist_state(
                    torrent_hash, "verify_pending", error=row.get("radarr_blocklist_last_error"),
                    verify_misses=misses,
                )
                logging.warning(
                    "RADARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r verify_misses=%d/3",
                    cfg.name, media_id, history_id, media_title, safe_release_title, misses,
                )
            continue

        # Persist the intent before the side effect. If the process dies after the
        # HTTP request, the next run performs a read-only verification instead of
        # sending a second failed-history event that could trigger another search.
        db.set_radarr_blocklist_state(torrent_hash, "request_started", error=None, verify_misses=0)
        url = normalize_url(cfg.arr_url, f"/api/v3/history/failed/{history_id}")
        try:
            arr.request_json("POST", url, api_key=True, retry_count=0)
        except HttpStatusError as exc:
            safe_error = safe_notification_text(str(exc), 500)
            if is_retryable_http(exc.code):
                db.set_radarr_blocklist_state(
                    torrent_hash, "verify_pending", error=safe_error, verify_misses=0
                )
                logging.warning(
                    "RADARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r http_status=%s error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, exc.code, safe_error,
                )
            else:
                db.set_radarr_blocklist_state(torrent_hash, "failed", error=safe_error)
                logging.error(
                    "RADARR_BLOCKLIST_FAILED source=%s media_id=%s history_id=%s title=%r release=%r http_status=%s error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, exc.code, safe_error,
                )
            continue
        except Exception as exc:
            safe_error = safe_notification_text(str(exc), 500)
            db.set_radarr_blocklist_state(
                torrent_hash, "verify_pending", error=safe_error, verify_misses=0
            )
            logging.warning(
                "RADARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r error=%s",
                cfg.name, media_id, history_id, media_title, safe_release_title, safe_error,
            )
            continue

        # Radarr handles the failed-history event synchronously and returns only after
        # the native failure/blocklist path has run. No unsupported /blocklist POST is used.
        db.set_radarr_blocklist_state(torrent_hash, "blocked", error=None, verify_misses=0)
        logging.info(
            "RADARR_BLOCKLIST_BLOCKED source=%s media_id=%s history_id=%s title=%r release=%r",
            cfg.name, media_id, history_id, media_title, safe_release_title,
        )


def process_sonarr_blocklist(cfg: Config, arr: HttpClient, db: StateDB) -> None:
    """Use Sonarr's native failed-history action to blocklist terminally failed grabs."""
    if cfg.arr_type != "sonarr":
        return
    if not cfg.sonarr_blocklist_enabled:
        logging.info("SONARR_BLOCKLIST_SKIPPED source=%s reason=disabled", cfg.name)
        return

    def title_key(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    for row in db.sonarr_blocklist_candidates(cfg.name):
        torrent_hash = str(row.get("hash") or "").strip().lower()
        media_id = as_int(row.get("arr_media_id"))
        history_id = as_int(row.get("arr_history_id"))
        release_title = str(row.get("title") or "").strip()
        media_title = safe_notification_text(
            row.get("media_title") or row.get("title") or "Unknown", 160
        )
        safe_release_title = safe_notification_text(release_title or "Unknown", 200)
        current_status = str(row.get("sonarr_blocklist_status") or "")
        attempt_count = int(row.get("attempt_count") or 0)

        # A definitive terminal status can occur before the configured retry limit
        # (for example, a permanent API rejection). Only an ambiguous request already
        # in flight is verified regardless of count; do not blocklist a fresh item early.
        if current_status not in {"request_started", "verify_pending"} and attempt_count < cfg.max_add_attempts:
            reason = (
                f"attempt count {attempt_count}/{cfg.max_add_attempts}; "
                "configured attempt limit has not been exhausted"
            )
            db.set_sonarr_blocklist_state(
                torrent_hash, "skipped_attempt_limit", error=reason, verify_misses=0
            )
            logging.warning(
                "SONARR_BLOCKLIST_SKIPPED source=%s media_id=%s history_id=%s title=%r release=%r attempts=%d/%d reason=attempt-limit-not-exhausted",
                cfg.name, media_id or "missing", history_id or "missing",
                media_title, safe_release_title, attempt_count, cfg.max_add_attempts,
            )
            continue

        if not torrent_hash or not media_id or not history_id or not release_title:
            reason = "required Sonarr history/media/release identity is missing"
            db.set_sonarr_blocklist_state(torrent_hash, "failed", error=reason)
            logging.error(
                "SONARR_BLOCKLIST_FAILED source=%s media_id=%s history_id=%s title=%r release=%r reason=%s",
                cfg.name, media_id or "missing", history_id or "missing",
                media_title, safe_release_title, reason,
            )
            continue

        # Once a non-idempotent failed-history request may have been sent, never resend
        # it blindly. Sonarr's BlocklistResource does not expose the torrent infohash,
        # so recovery can only compare the exact source title for that series.
        if current_status in {"request_started", "verify_pending"}:
            url = normalize_url(cfg.arr_url, "/api/v3/blocklist") + "?" + urlencode(
                {"seriesIds": media_id, "page": 1, "pageSize": 1000}
            )
            try:
                response = arr.request_json(
                    "GET", url, api_key=True,
                    retry_count=cfg.retry_count, retry_delay=cfg.retry_delay,
                )
                # Sonarr exposes a paged resource with the matching entries in "records".
                # Accept a bare list as a defensive compatibility path for older builds.
                if isinstance(response, dict) and isinstance(response.get("records"), list):
                    records = response["records"]
                elif isinstance(response, list):
                    records = response
                else:
                    raise ImporterError("Sonarr blocklist response does not contain a records array")
                found = any(
                    isinstance(item, dict)
                    and title_key(item.get("sourceTitle")) == title_key(release_title)
                    for item in records
                )
            except Exception as exc:
                safe_error = safe_notification_text(str(exc), 500)
                db.set_sonarr_blocklist_state(
                    torrent_hash, "verify_pending", error=safe_error
                )
                logging.warning(
                    "SONARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r reason=lookup-error error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, safe_error,
                )
                continue

            if found:
                db.set_sonarr_blocklist_state(
                    torrent_hash, "already_present", error=None, verify_misses=0
                )
                logging.info(
                    "SONARR_BLOCKLIST_ALREADY_PRESENT source=%s media_id=%s history_id=%s title=%r release=%r evidence=source-title-match_after_ambiguous_request",
                    cfg.name, media_id, history_id, media_title, safe_release_title,
                )
                continue

            misses = int(row.get("sonarr_blocklist_verify_misses") or 0) + 1
            if misses >= 3:
                error = (
                    "Sonarr accepted no verifiable blocklist evidence after three checks; "
                    "the request result was ambiguous, and it was not resent to avoid duplicate side effects"
                )
                db.set_sonarr_blocklist_state(
                    torrent_hash, "failed", error=error, verify_misses=misses
                )
                logging.error(
                    "SONARR_BLOCKLIST_FAILED source=%s media_id=%s history_id=%s title=%r release=%r verify_misses=%d error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, misses, error,
                )
            else:
                db.set_sonarr_blocklist_state(
                    torrent_hash, "verify_pending", error=row.get("sonarr_blocklist_last_error"),
                    verify_misses=misses,
                )
                logging.warning(
                    "SONARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r verify_misses=%d/3",
                    cfg.name, media_id, history_id, media_title, safe_release_title, misses,
                )
            continue

        # Persist the intent before the side effect. If the process dies after the
        # HTTP request, the next run performs a read-only verification instead of
        # sending a second failed-history event that could trigger another search.
        db.set_sonarr_blocklist_state(torrent_hash, "request_started", error=None, verify_misses=0)
        url = normalize_url(cfg.arr_url, f"/api/v3/history/failed/{history_id}")
        try:
            arr.request_json("POST", url, api_key=True, retry_count=0)
        except HttpStatusError as exc:
            safe_error = safe_notification_text(str(exc), 500)
            if is_retryable_http(exc.code):
                db.set_sonarr_blocklist_state(
                    torrent_hash, "verify_pending", error=safe_error, verify_misses=0
                )
                logging.warning(
                    "SONARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r http_status=%s error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, exc.code, safe_error,
                )
            else:
                db.set_sonarr_blocklist_state(torrent_hash, "failed", error=safe_error)
                logging.error(
                    "SONARR_BLOCKLIST_FAILED source=%s media_id=%s history_id=%s title=%r release=%r http_status=%s error=%s",
                    cfg.name, media_id, history_id, media_title, safe_release_title, exc.code, safe_error,
                )
            continue
        except Exception as exc:
            safe_error = safe_notification_text(str(exc), 500)
            db.set_sonarr_blocklist_state(
                torrent_hash, "verify_pending", error=safe_error, verify_misses=0
            )
            logging.warning(
                "SONARR_BLOCKLIST_VERIFY_PENDING source=%s media_id=%s history_id=%s title=%r release=%r error=%s",
                cfg.name, media_id, history_id, media_title, safe_release_title, safe_error,
            )
            continue

        # Sonarr handles the failed-history event synchronously and returns only after
        # its native failed-download/blocklist path has run. No unsupported POST is used.
        db.set_sonarr_blocklist_state(torrent_hash, "blocked", error=None, verify_misses=0)
        logging.info(
            "SONARR_BLOCKLIST_BLOCKED source=%s media_id=%s history_id=%s title=%r release=%r",
            cfg.name, media_id, history_id, media_title, safe_release_title,
        )


def process_seerr_watchlist(cfg: Config, db: StateDB) -> None:
    """Mirror terminal TorrServer failures into the administrator's Seerr Watchlist."""
    if not cfg.seerr_watchlist_enabled:
        return
    client = HttpClient(cfg.timeout, cfg.user_agent, api_key=cfg.seerr_api_key)
    records = db.seerr_watchlist_candidates(cfg.name)
    considered = added = already_present = errors = 0

    for record in records:
        status = str(record.get("seerr_watchlist_status") or "")
        if status in {"added", "already_present"}:
            continue
        tmdb_id = as_int(record.get("tmdb_id"))
        if tmdb_id is None or tmdb_id <= 0:
            if status != "missing_tmdb_id":
                _seerr_watchlist_record_error(
                    cfg, db, record, "missing_tmdb_id",
                    "TMDb ID is missing or invalid in arr2torr state; no external lookup was attempted",
                )
                errors += 1
            continue

        media_type = "tv" if cfg.category == "tv" else "movie"
        considered += 1
        try:
            client.request_json(
                "POST", normalize_url(cfg.seerr_url, "/api/v1/watchlist"),
                payload={"tmdbId": tmdb_id, "mediaType": media_type},
                api_key=True, retry_count=cfg.retry_count, retry_delay=cfg.retry_delay,
            )
        except HttpStatusError as exc:
            if exc.code == 409:
                try:
                    present = seerr_watchlist_contains(cfg, client, tmdb_id, media_type)
                except Exception as verify_error:
                    _seerr_watchlist_record_error(
                        cfg, db, record, "error",
                        f"HTTP 409 from Seerr; duplicate verification failed: {verify_error}",
                    )
                    errors += 1
                    continue
                if present:
                    _seerr_watchlist_success(cfg, db, record, "already_present")
                    already_present += 1
                    continue
                _seerr_watchlist_record_error(
                    cfg, db, record, "error",
                    f"HTTP 409 from Seerr but exact item was not found in the administrator Watchlist: {exc.body}",
                )
                errors += 1
                continue
            _seerr_watchlist_record_error(cfg, db, record, "error", f"Seerr POST failed: {exc}")
            errors += 1
            continue
        except Exception as exc:
            _seerr_watchlist_record_error(cfg, db, record, "error", f"Seerr POST failed: {exc}")
            errors += 1
            continue

        _seerr_watchlist_success(cfg, db, record, "added")
        added += 1

    logging.info(
        "SEERR_WATCHLIST_SUMMARY source=%s candidates=%d requests=%d added=%d already_present=%d errors=%d",
        cfg.name, len(records), considered, added, already_present, errors,
    )


def notification_delivery_retry_delay(attempt: int) -> int:
    # Retry delivery independently from torrent adds. The systemd timer may make
    # actual retries less frequent; state survives restarts and an Apprise outage.
    delays = (60, 300, 900, 1800, 3600)
    return delays[min(max(1, int(attempt)) - 1, len(delays) - 1)]


def human_failure_reason(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    http = re.search(r"\bHTTP\s+(\d{3})\b", raw, flags=re.IGNORECASE)
    code = int(http.group(1)) if http else None

    if code == 410 and ("download url" in lowered or "resolving" in lowered):
        return "ссылка на выбранную раздачу больше недоступна (HTTP 410); запрос добавления в TorrServer не выполнялся"
    if code in {401, 403}:
        return f"сервис отклонил запрос из-за авторизации (HTTP {code})"
    if code == 404 and ("download" in lowered or "url" in lowered):
        return "ссылка на выбранную раздачу не найдена (HTTP 404)"
    if "verification deadline expired" in lowered:
        if "absent-from-list" in lowered:
            return "TorrServer не подтвердил появление раздачи в своём списке за отведённое время"
        return "TorrServer не подтвердил готовность раздачи за отведённое время"
    if "connection refused" in lowered:
        return "не удалось подключиться к TorrServer: соединение отклонено"
    if "timed out" in lowered or "timeout" in lowered:
        if "download url" in lowered or "resolve" in lowered:
            return "истекло время ожидания при получении ссылки на раздачу"
        return "истекло время ожидания ответа от TorrServer"
    if "name or service not known" in lowered or "temporary failure in name resolution" in lowered:
        return "не удалось определить адрес одного из сервисов"
    if code is not None:
        return f"сервис вернул HTTP {code}"
    return safe_notification_text(raw, 180)


def human_seerr_error(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    http = re.search(r"\bHTTP\s+(\d{3})\b", raw, flags=re.IGNORECASE)
    code = int(http.group(1)) if http else None
    if code in {401, 403}:
        return f"Seerr отклонил авторизацию (HTTP {code})"
    if code is not None and code >= 500:
        return f"Seerr временно недоступен (HTTP {code}); arr2torr повторит регистрацию"
    if "timeout" in lowered or "timed out" in lowered or "urlerror" in lowered:
        return "Seerr не ответил; arr2torr повторит регистрацию"
    return safe_notification_text(raw, 160)


def terminal_notification_block(cfg: Config, row: dict[str, Any], *, preparation: bool = False) -> str:
    media_title = safe_notification_text(
        row.get("media_title") or row.get("title") or row.get("release_title") or "Неизвестное медиа",
        160,
    )
    release_title = safe_notification_text(row.get("release_title") or row.get("title") or "", 200)
    media_label = "Фильм" if cfg.category == "movie" else "Сериал"
    lines = [f"{media_label}: {media_title}"]
    if release_title and release_title != media_title:
        lines.append(f"Выбранная раздача: {release_title}")

    lines.append("Не удалось добавить выбранную раздачу в TorrServer.")
    reason = row.get("reason") if preparation else (row.get("reason") or row.get("last_error"))
    lines.append(f"Причина: {human_failure_reason(reason)}")

    if not preparation:
        attempts = int(row.get("attempt_count") or 0)
        lines.append(f"Автоматические попытки закончились ({attempts} из {cfg.max_add_attempts}).")

    arr_label = "Radarr" if cfg.arr_type == "radarr" else "Sonarr"
    lines.append(f"Советуем выбрать другую раздачу в {arr_label} и нажать Grab.")

    if cfg.seerr_watchlist_enabled:
        status = str(row.get("seerr_watchlist_status") or "")
        if status == "added":
            lines.append(f"{media_label} добавлен в Watchlist администратора Seerr." if cfg.category == "movie" else "Сериал добавлен в Watchlist администратора Seerr.")
        elif status == "already_present":
            lines.append(f"{media_label} уже есть в Watchlist администратора Seerr." if cfg.category == "movie" else "Сериал уже есть в Watchlist администратора Seerr.")
        elif status == "missing_tmdb_id":
            lines.append(f"Не удалось добавить {media_label.lower()} в Watchlist Seerr: в данных нет TMDb ID.")
        elif status == "error":
            seerr_error = human_seerr_error(row.get("seerr_watchlist_last_error"))
            lines.append(f"Не удалось добавить {media_label.lower()} в Watchlist Seerr: {seerr_error}.")
        else:
            lines.append(f"Не удалось проверить статус {media_label.lower()} в Watchlist Seerr; arr2torr попробует снова.")

    link = arr_card_url(cfg, row.get("title_slug"))
    if link:
        lines.append(f"Карточка в {arr_label}: {link}")
    # The configured blocklist result is deliberately the final item in the alert.
    blocklist_label = "Radarr" if cfg.arr_type == "radarr" else "Sonarr"
    blocklist_enabled = (
        cfg.radarr_blocklist_enabled if cfg.arr_type == "radarr"
        else cfg.sonarr_blocklist_enabled
    )
    state_prefix = "radarr" if cfg.arr_type == "radarr" else "sonarr"
    blocklist_status = str(row.get(f"{state_prefix}_blocklist_status") or "")
    if not preparation and blocklist_enabled:
        blocklist_error = safe_notification_text(
            row.get(f"{state_prefix}_blocklist_last_error") or "", 120
        )
        if blocklist_status == "blocked":
            if cfg.arr_type == "radarr":
                lines.append("Блок-лист Radarr: релиз добавлен; Radarr сможет исключить эту раздачу при дальнейшем поиске.")
            else:
                lines.append("Блок-лист Sonarr: команда принята; Sonarr сможет исключить эту раздачу при дальнейшем поиске.")
        elif blocklist_status == "already_present":
            lines.append(f"Блок-лист {blocklist_label}: запись с таким названием релиза уже найдена; точный infohash через API подтвердить нельзя.")
        elif blocklist_status in {"request_started", "verify_pending", "retry"}:
            lines.append(f"Блок-лист {blocklist_label}: команда отправлена или её результат пока не подтверждён; импортёр проверяет результат и не отправляет повторную команду автоматически.")
            if blocklist_error:
                lines.append(f"Проверка блок-листа: {blocklist_error}")
        elif blocklist_status == "failed":
            lines.append(f"Блок-лист {blocklist_label}: не удалось подтвердить блокировку этой раздачи.")
            if blocklist_error:
                lines.append(f"Причина блокировки: {blocklist_error}")
        elif blocklist_status == "legacy_skipped":
            lines.append(f"Блок-лист {blocklist_label}: эта ошибка относится к периоду до обновления; автоматическая блокировка задним числом не выполнялась.")
        elif blocklist_status == "skipped_attempt_limit":
            lines.append(f"Блок-лист {blocklist_label}: блокировка не выполнялась, поскольку зарегистрировано меньше попыток, чем настроенный лимит; условие исчерпания попыток не выполнено.")
    return "\n".join(lines)


def chunk_terminal_records(
    cfg: Config, records: list[dict[str, Any]], limit: int = 3250
) -> list[list[dict[str, Any]]]:
    """Split a large terminal batch into the fewest messages that fit the Telegram-size budget."""
    service = safe_notification_text(cfg.service_name or cfg.name, 140)
    intro = f"{service}\nОкончательные ошибки передачи в TorrServer: {len(records)}."
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        candidate_rows = current + [record]
        candidate = intro + "\n\n" + "\n\n".join(
            terminal_notification_block(cfg, item, preparation=item.get("record_table") == "terminal_events")
            for item in candidate_rows
        )
        if current and len(candidate) > limit:
            chunks.append(current)
            current = [record]
        else:
            current.append(record)
    if current:
        chunks.append(current)
    return chunks


def terminal_notification_body(
    cfg: Config, records: list[dict[str, Any]], total_count: int, part: int, parts: int
) -> str:
    service = safe_notification_text(cfg.service_name or cfg.name, 140)
    lines = [service, f"Окончательные ошибки передачи в TorrServer: {total_count}."]
    if parts > 1:
        lines.append(f"Часть {part} из {parts}.")
    lines.extend(
        terminal_notification_block(cfg, row, preparation=row.get("record_table") == "terminal_events")
        for row in records
    )
    return "\n\n".join(lines)


def dispatch_notifications(cfg: Config, db: StateDB) -> None:
    # Interim retry states stay in the journal. Telegram is reserved for terminal,
    # actionable failures, one grouped critical message per batch.
    records = [
        *db.unnotified_terminal_failures(cfg.name),
        *db.unnotified_terminal_events(cfg.name),
    ]
    if not records:
        return
    if not cfg.notifications_enabled:
        logging.info(
            "APPRISE_SKIPPED source=%s reason=notifications-disabled terminal_items=%d",
            cfg.name, len(records),
        )
        return

    chunks = chunk_terminal_records(cfg, records)
    for index, chunk in enumerate(chunks, start=1):
        body = terminal_notification_body(cfg, chunk, len(records), index, len(chunks))
        suffix = f" (часть {index}/{len(chunks)})" if len(chunks) > 1 else ""
        noun = "раздачу" if len(records) == 1 else "выбранные раздачи"
        title = f"{cfg.service_name} — Не удалось добавить {noun} в TorrServer{suffix}"
        if apprise_notify(cfg, title, body, "failure", cfg.notification_tag_critical):
            db.mark_notification_delivered(chunk)
            logging.info(
                "APPRISE_TERMINAL_DELIVERED source=%s items=%d part=%d/%d",
                cfg.name, len(chunk), index, len(chunks),
            )
        else:
            delay = db.schedule_notification_retry(chunk, "apprise_delivery_failed")
            logging.warning(
                "APPRISE_TERMINAL_RETRY_QUEUED source=%s items=%d part=%d/%d retry_in_sec=%d",
                cfg.name, len(chunk), index, len(chunks), delay,
            )


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_history(cfg: Config, arr: HttpClient) -> list[Grab]:
    all_records: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urlencode({
            "page": page,
            "pageSize": cfg.history_page_size,
            "sortKey": "date",
            "sortDirection": "descending",
        })
        url = normalize_url(cfg.arr_url, "/api/v3/history") + "?" + query
        response = arr.request_json("GET", url, api_key=True, retry_count=cfg.retry_count, retry_delay=cfg.retry_delay)
        if not isinstance(response, dict):
            raise ImporterError("Arr history response is not an object")
        records = response.get("records") or []
        if not isinstance(records, list):
            raise ImporterError("Arr history response has invalid records")
        all_records.extend(r for r in records if isinstance(r, dict))
        total = as_int(response.get("totalRecords")) or len(all_records)
        logging.debug("history page=%d returned=%d total=%d", page, len(records), total)
        if len(all_records) >= total or not records:
            break
        page += 1

    grabs: dict[str, Grab] = {}
    grabbed_records = 0
    skipped_no_data = 0
    skipped_no_hash = 0
    skipped_no_url = 0
    for record in all_records:
        if str(record.get("eventType") or "").lower() != "grabbed":
            continue
        grabbed_records += 1
        data = record.get("data")
        if not isinstance(data, dict):
            skipped_no_data += 1
            continue
        torrent_hash = str(data.get("torrentInfoHash") or "").strip().lower()
        download_url = str(data.get("downloadUrl") or "").strip()
        if not torrent_hash:
            skipped_no_hash += 1
            continue
        if not download_url:
            skipped_no_url += 1
            continue
        grab = Grab(
            history_id=as_int(record.get("id")) or 0,
            movie_id=as_int(record.get("movieId")),
            series_id=as_int(record.get("seriesId")),
            source_title=str(record.get("sourceTitle") or ""),
            date=str(record.get("date") or "") or None,
            data=data,
            quality=record.get("quality") if isinstance(record.get("quality"), dict) else {},
            languages=record.get("languages") if isinstance(record.get("languages"), list) else [],
            custom_formats=record.get("customFormats") if isinstance(record.get("customFormats"), list) else [],
            custom_format_score=as_int(record.get("customFormatScore")),
            download_id=str(record.get("downloadId") or "") or None,
        )
        # Keep the newest history record for each hash.
        previous = grabs.get(grab.hash)
        if previous is None or (grab.date or "") > (previous.date or ""):
            grabs[grab.hash] = grab
    duplicate_count = grabbed_records - skipped_no_data - skipped_no_hash - skipped_no_url - len(grabs)
    logging.info(
        "HISTORY source=%s records=%d grabbed=%d unique_hashes=%d duplicates=%d skipped_no_data=%d skipped_no_hash=%d skipped_no_url=%d",
        cfg.name, len(all_records), grabbed_records, len(grabs), max(0, duplicate_count), skipped_no_data, skipped_no_hash, skipped_no_url
    )
    return list(grabs.values())


def fetch_torrserver(cfg: Config, ts: HttpClient) -> list[dict[str, Any]]:
    url = normalize_url(cfg.torrserver_url, "/torrents")
    response = ts.request_json("POST", url, payload={"action": "list"}, retry_count=cfg.retry_count, retry_delay=cfg.retry_delay)
    if not isinstance(response, list):
        raise ImporterError("TorrServer list response is not an array")
    return [r for r in response if isinstance(r, dict)]


def extract_ts_hashes(records: Iterable[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for record in records:
        value = str(record.get("hash") or "").strip().lower()
        if value:
            result.add(value)
    return result


def fetch_torrserver_item(cfg: Config, ts: HttpClient, torrent_hash: str) -> dict[str, Any]:
    """Read the live torrent object without submitting another add request."""
    url = normalize_url(cfg.torrserver_url, "/torrents")
    response = ts.request_json(
        "POST", url, payload={"action": "get", "hash": torrent_hash},
        retry_count=cfg.retry_count, retry_delay=cfg.retry_delay,
    )
    if not isinstance(response, dict):
        raise ImporterError("TorrServer get response is not an object")
    response_hash = str(response.get("hash") or "").strip().lower()
    if response_hash != torrent_hash:
        raise ImporterError(
            f"TorrServer get returned hash={response_hash or 'missing'}, expected {torrent_hash}"
        )
    return response


def torrserver_status(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return "absent-from-list"
    return str(record.get("stat_string") or record.get("statString") or "unknown").strip()


def is_db_only_record(record: dict[str, Any] | None) -> bool:
    return torrserver_status(record).casefold() == "torrent in db"


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def file_stats_problem(record: dict[str, Any] | None) -> str | None:
    """Explain why a TorrServer record does not contain a usable file structure."""
    if not isinstance(record, dict):
        return "record-missing"
    stats = None
    for key in ("file_stats", "FileStats", "fileStats"):
        if key in record:
            stats = record.get(key)
            break
    if not isinstance(stats, list) or not stats:
        return "file-stats-missing-or-empty"

    seen_ids: set[int] = set()
    seen_paths: set[str] = set()
    for index, entry in enumerate(stats, start=1):
        if not isinstance(entry, dict):
            return f"file-stats-entry-{index}-not-an-object"
        file_id = as_int(entry.get("id", entry.get("ID")))
        if file_id is None or file_id <= 0:
            return f"file-stats-entry-{index}-invalid-id"
        if file_id in seen_ids:
            return f"file-stats-entry-{index}-duplicate-id"
        seen_ids.add(file_id)

        raw_path = entry.get("path", entry.get("Path"))
        if not isinstance(raw_path, str) or not raw_path.strip():
            return f"file-stats-entry-{index}-invalid-path"
        if "\x00" in raw_path or "\\" in raw_path or re.match(r"^[A-Za-z]:", raw_path):
            return f"file-stats-entry-{index}-unsafe-path"
        path = PurePosixPath(raw_path)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            return f"file-stats-entry-{index}-unsafe-path"
        normalized_path = str(path)
        if normalized_path in seen_paths:
            return f"file-stats-entry-{index}-duplicate-path"
        seen_paths.add(normalized_path)
    return None


def verification_evidence(record: dict[str, Any] | None) -> str | None:
    """Accept success only after a non-empty, safe TorrServer FileStats structure is available."""
    if file_stats_problem(record) is None:
        return "usable-file-stats"
    return None


def pending_deadline(row: dict[str, Any]) -> datetime | None:
    return parse_iso_datetime(row.get("deadline_at"))


def pending_retry_due(row: dict[str, Any], source: str, now: datetime, max_attempts: int) -> bool:
    if row.get("source") != source or row.get("status") != "retry_wait":
        return False
    if int(row.get("attempt_count") or 0) >= max_attempts:
        return False
    due = parse_iso_datetime(row.get("next_retry_at"))
    return due is not None and due <= now


def fetch_media(cfg: Config, arr: HttpClient, grab: Grab) -> dict[str, Any]:
    media_id = grab.arr_media_id
    if not media_id:
        return {}
    endpoint = "movie" if cfg.arr_type == "radarr" else "series"
    url = normalize_url(cfg.arr_url, f"/api/v3/{endpoint}/{media_id}")
    response = arr.request_json("GET", url, api_key=True, retry_count=cfg.retry_count, retry_delay=cfg.retry_delay)
    if not isinstance(response, dict):
        raise ImporterError(f"Invalid {cfg.arr_type} media response for id={media_id}")
    return response


def non_sensitive_data(cfg: Config, grab: Grab, media: dict[str, Any]) -> str:
    # Deliberately exclude downloadUrl/nzbInfoUrl from persistent data and logs because
    # the former may contain a Prowlarr API key.
    payload: dict[str, Any] = {
        "source": cfg.name,
        "type": cfg.category,
        "target_quality": cfg.target_quality,
        "imdbid": media.get("imdbId") or None,
        "tmdbid": media.get("tmdbId") or None,
        "tvdbid": media.get("tvdbId") or None,
        "arr_media_id": grab.arr_media_id,
        "arr_history_id": grab.history_id,
        "download_id": grab.download_id,
        "indexer": grab.data.get("indexer") or None,
        "indexer_id": grab.data.get("indexerId") or None,
        "release_group": grab.data.get("releaseGroup") or None,
        "arr_custom_format_score": grab.custom_format_score,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class ProcessLock:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.handle = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise ImporterError(f"another importer instance is already running: {self.path}") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def add_to_torrserver(cfg: Config, ts: HttpClient, grab: Grab, media: dict[str, Any], resolved_link: str | None = None) -> dict[str, Any]:
    if not grab.download_url:
        raise ImporterError(f"hash={grab.hash} has no downloadUrl")
    link = resolved_link if resolved_link is not None else (ts.resolve_link(grab.download_url) if cfg.resolve_download_url else grab.download_url)
    payload = {
        "action": "add",
        "link": link,
        "title": grab.source_title,
        "category": cfg.category,
        "data": non_sensitive_data(cfg, grab, media),
        "save_to_db": True,
    }
    url = normalize_url(cfg.torrserver_url, "/torrents")
    response = ts.request_json("POST", url, payload=payload, retry_count=cfg.retry_count, retry_delay=cfg.retry_delay)
    if not isinstance(response, dict):
        raise ImporterError(f"TorrServer add returned unexpected response for hash={grab.hash}")
    response_hash = str(response.get("hash") or "").strip().lower()
    stat = str(response.get("stat_string") or "").strip()
    if response_hash and response_hash != grab.hash:
        raise ImporterError(f"TorrServer returned hash={response_hash}, expected {grab.hash}")
    # The tested TorrServer returns stat_string='Torrent added' for this operation.
    # Accept any 200 response containing the expected hash; if hash is omitted, be conservative.
    if not response_hash:
        raise ImporterError(f"TorrServer add did not return hash; stat={stat!r}")
    return response


def run(cfg: Config, rebuild: bool = False, dry_run: bool = False, retry_failed: bool = False) -> int:
    cfg = cfg.normalized()
    started = time.monotonic()
    logging.info(
        "START source=%s type=%s category=%s target_quality=%s verification_timeout_sec=%s",
        cfg.name, cfg.arr_type, cfg.category, cfg.target_quality, cfg.verification_timeout_sec,
    )

    lock = ProcessLock(cfg.lock_file)
    lock.__enter__()
    arr = HttpClient(cfg.timeout, cfg.user_agent, api_key=cfg.arr_api_key)
    ts = HttpClient(cfg.timeout, cfg.user_agent)
    db = StateDB(cfg.state_db)
    try:
        try:
            grabs = fetch_history(cfg, arr)
            ts_records = fetch_torrserver(cfg, ts)
        except ImporterError:
            logging.exception("SYNC_FAILED source=%s stage=fetch", cfg.name)
            return 2

        ts_by_hash = {
            str(record.get("hash") or "").strip().lower(): record
            for record in ts_records
            if str(record.get("hash") or "").strip()
        }
        ts_hashes = set(ts_by_hash)
        imported_hashes = db.all_hashes()
        added = 0
        failed = 0
        submitted = 0
        history_count = len(grabs)
        known_count = sum(1 for grab in grabs if grab.hash in imported_hashes)

        if retry_failed and not dry_run:
            requeued = db.requeue_failed(cfg.name)
            logging.warning("RETRY_FAILED source=%s requeued=%d", cfg.name, requeued)

        # Reconcile persisted state against successful-import history and the FileStats contract.
        # Hash presence alone is intermediate; previously imported hashes remain protected from
        # automatic resurrection when they have intentionally been removed from TorrServer.
        grabs_by_hash = {grab.hash.lower(): grab for grab in grabs if grab.hash}
        media_cache: dict[int, dict[str, Any]] = {}
        if not dry_run:
            for pending in db.pending_rows(source=cfg.name):
                status = str(pending.get("status") or "")
                if status not in {"pending", "retry_wait", "failed"}:
                    continue
                h = str(pending.get("hash") or "").lower()

                if h in imported_hashes:
                    db.confirm_pending(h)
                    logging.info(
                        "PENDING_CLEARED source=%s hash=%s reason=already-in-import-history",
                        cfg.name, h,
                    )
                    continue

                record = ts_by_hash.get(h)
                evidence = verification_evidence(record)
                if evidence:
                    completed = db.confirm_pending(h)
                    if completed is not None:
                        imported_hashes.add(h)
                        added += 1
                        logging.info(
                            "ADDED source=%s hash=%s verification=%s recovery=true",
                            cfg.name, h, evidence,
                        )
                    continue

                # If a persisted DB-only entry is not active, re-add only to activate its metadata.
                # Never issue even this activation request after the verification deadline expired.
                # A live object is inspected via action=get; it is never blindly added again.
                current_deadline = pending_deadline(pending)
                verification_window_open = (
                    current_deadline is None or datetime.now(timezone.utc) < current_deadline
                )
                if status == "pending" and record is not None and verification_window_open:
                    attempt_count = int(pending.get("attempt_count") or 0)
                    if is_db_only_record(record):
                        current_grab = grabs_by_hash.get(h)
                        if (
                            current_grab is not None
                            and db.metadata_activation_attempt(h) != attempt_count
                            and db.mark_metadata_activation_attempt(h, attempt_count)
                        ):
                            try:
                                media = media_cache.get(current_grab.arr_media_id) if current_grab.arr_media_id else None
                                if media is None:
                                    media = fetch_media(cfg, arr, current_grab)
                                    if current_grab.arr_media_id:
                                        media_cache[current_grab.arr_media_id] = media
                                if not current_grab.download_url:
                                    raise ImporterError(f"hash={h} has no downloadUrl for DB-only metadata activation")
                                resolved_link = (
                                    ts.resolve_link(current_grab.download_url)
                                    if cfg.resolve_download_url else current_grab.download_url
                                )
                                response = add_to_torrserver(
                                    cfg, ts, current_grab, media, resolved_link=resolved_link,
                                )
                                response_status = str(
                                    response.get("stat_string") or response.get("statString") or "accepted"
                                )
                                submitted += 1
                                db.observe_pending(h, "metadata-activation-accepted: " + response_status)
                                logging.info(
                                    "METADATA_ACTIVATION_ACCEPTED source=%s hash=%s attempt=%d stat=%s",
                                    cfg.name, h, attempt_count, response_status,
                                )
                            except Exception as exc:
                                db.observe_pending(h, "metadata-activation-failed", str(exc))
                                logging.warning(
                                    "METADATA_ACTIVATION_FAILED source=%s hash=%s attempt=%d error=%s",
                                    cfg.name, h, attempt_count, exc,
                                )
                    else:
                        try:
                            details = fetch_torrserver_item(cfg, ts, h)
                            detail_evidence = verification_evidence(details)
                            if detail_evidence:
                                completed = db.confirm_pending(h)
                                if completed is not None:
                                    imported_hashes.add(h)
                                    added += 1
                                    logging.info(
                                        "ADDED source=%s hash=%s verification=%s recovery=action-get",
                                        cfg.name, h, detail_evidence,
                                    )
                                continue
                            db.observe_pending(
                                h, "action-get-incomplete: " + torrserver_status(details)
                                + "; " + str(file_stats_problem(details)),
                            )
                        except HttpStatusError as exc:
                            db.observe_pending(
                                h, "action-get-not-found" if exc.code == 404 else "action-get-failed", str(exc),
                            )
                            logging.warning(
                                "METADATA_RECOVERY_GET_FAILED source=%s hash=%s http_status=%s error=%s",
                                cfg.name, h, exc.code, exc,
                            )
                        except Exception as exc:
                            db.observe_pending(h, "action-get-failed", str(exc))
                            logging.warning(
                                "METADATA_RECOVERY_GET_FAILED source=%s hash=%s error=%s",
                                cfg.name, h, exc,
                            )

                # A later Radarr Grab of the same infohash represents a new operator-selected
                # attempt. Do not let a terminal row from the older Grab block it forever.
                if status == "failed":
                    current_grab = grabs_by_hash.get(h)
                    old_history_id = as_int(pending.get("arr_history_id")) or 0
                    new_history_id = as_int(current_grab.history_id) if current_grab else None
                    if (
                        current_grab is not None
                        and old_history_id > 0
                        and new_history_id is not None
                        and new_history_id > old_history_id
                        and db.rearm_failed_for_new_grab(
                            current_grab, cfg.name, cfg.verification_timeout_sec,
                        )
                    ):
                        logging.warning(
                            "REARMED_NEW_GRAB source=%s hash=%s previous_history_id=%d current_history_id=%d reason=new-radarr-grab",
                            cfg.name, h, old_history_id, new_history_id,
                        )
                        continue

                if status == "pending":
                    deadline = pending_deadline(pending)
                    if deadline is not None and datetime.now(timezone.utc) >= deadline:
                        state = db.fail_pending(
                            h,
                            "Verification deadline expired before this run; last observed status="
                            + str(pending.get("last_status") or "unknown"),
                            cfg.max_add_attempts,
                            cfg.retry_backoff_sec,
                        )
                        failed += 1
                        logging.error(
                            "VERIFY_FAILED source=%s hash=%s status=%s attempts=%s reason=deadline-expired",
                            cfg.name, h, state, pending.get("attempt_count"),
                        )

        pending_map = {row["hash"]: row for row in db.pending_rows()}
        now = datetime.now(timezone.utc)
        candidates: list[Grab] = []
        retry_candidates: set[str] = set()
        for grab in grabs:
            record = ts_by_hash.get(grab.hash)
            evidence = verification_evidence(record)
            if evidence:
                continue
            if not rebuild and grab.hash in imported_hashes:
                continue
            pending = pending_map.get(grab.hash)
            if pending is not None:
                if pending_retry_due(pending, cfg.name, now, cfg.max_add_attempts):
                    candidates.append(grab)
                    retry_candidates.add(grab.hash)
                # Any non-due, in-flight, other-source, or exhausted attempt blocks
                # another add. This also prevents cross-instance duplicate requests.
                continue
            # Missing hashes may be added. Present-but-incomplete hashes are observed or
            # explicitly reactivated only when the snapshot says the object is DB-only.
            candidates.append(grab)

        skipped_present = sum(1 for grab in grabs if verification_evidence(ts_by_hash.get(grab.hash)))
        present_incomplete = sum(
            1 for grab in grabs
            if grab.hash in ts_by_hash and not verification_evidence(ts_by_hash.get(grab.hash))
        )
        skipped_known = sum(
            1 for grab in grabs
            if not verification_evidence(ts_by_hash.get(grab.hash))
            and not rebuild and grab.hash in imported_hashes
        )
        logging.info(
            "SNAPSHOT source=%s arr_grabbed=%d torrserver=%d import_history_known=%d pending=%d candidates=%d present_incomplete=%d mode=%s",
            cfg.name, history_count, len(ts_hashes), known_count, len(pending_map), len(candidates),
            present_incomplete, "rebuild" if rebuild else "normal",
        )

        if dry_run:
            for grab in candidates:
                media = media_cache.get(grab.arr_media_id) if grab.arr_media_id else None
                if media is None:
                    media = fetch_media(cfg, arr, grab)
                    if grab.arr_media_id:
                        media_cache[grab.arr_media_id] = media
                reason = (
                    "retry-after-verification-failure" if grab.hash in retry_candidates
                    else ("rebuild" if rebuild else "new-hash")
                )
                logging.info(
                    "DRY_RUN source=%s hash=%s title=%r imdb=%s tmdb=%s reason=%s",
                    cfg.name, grab.hash, grab.source_title, media.get("imdbId") or "",
                    media.get("tmdbId") or "", reason,
                )
            logging.info(
                "DONE source=%s candidates=%d submitted=0 added=0 failed=0 pending=%d "
                "skipped_present=%d skipped_known=%d duration=%.2fs dry_run=true",
                cfg.name, len(candidates), len(db.pending_rows()), skipped_present,
                skipped_known, time.monotonic() - started,
            )
            return 0

        # Submit all eligible candidates first. Each add receives its own deadline;
        # one shared polling loop below verifies the whole batch without serial waits.
        for grab in candidates:
            media = media_cache.get(grab.arr_media_id) if grab.arr_media_id else None
            record = ts_by_hash.get(grab.hash)
            needs_add = record is None or is_db_only_record(record)
            try:
                if media is None:
                    media = fetch_media(cfg, arr, grab)
                    if grab.arr_media_id:
                        media_cache[grab.arr_media_id] = media
                resolved_link = None
                if needs_add:
                    if not grab.download_url:
                        raise ImporterError(f"hash={grab.hash} has no downloadUrl")
                    resolved_link = (
                        ts.resolve_link(grab.download_url)
                        if cfg.resolve_download_url else grab.download_url
                    )
            except Exception as exc:
                failed += 1
                logging.error(
                    "FAILED source=%s hash=%s title=%r stage=prepare error=%s",
                    cfg.name, grab.hash, grab.source_title, exc,
                )
                if permanent_preparation_error(exc):
                    media_for_error = media if isinstance(media, dict) else {}
                    db.record_terminal_event(
                        event_key=f"{cfg.name}:{grab.hash}:prepare",
                        source=cfg.name,
                        arr_type=cfg.arr_type,
                        category=cfg.category,
                        arr_media_id=grab.arr_media_id,
                        media_title=str(media_for_error.get("title") or "") or None,
                        release_title=grab.source_title or None,
                        title_slug=str(media_for_error.get("titleSlug") or "") or None,
                        stage="prepare",
                        reason=str(exc),
                        tmdb_id=media_for_error.get("tmdbId"),
                        tvdb_id=media_for_error.get("tvdbId"),
                    )
                continue

            reason = (
                "retry-after-verification-failure" if grab.hash in retry_candidates
                else ("rebuild" if rebuild else "new-hash")
            )
            attempt = db.begin_pending(
                grab, cfg.name, media, cfg.verification_timeout_sec,
                allow_known=rebuild,
            )
            if attempt == 0:
                logging.info(
                    "ADD_SKIPPED source=%s hash=%s reason=shared-pending-claim",
                    cfg.name, grab.hash,
                )
                continue
            record = ts_by_hash.get(grab.hash)
            is_metadata_activation = is_db_only_record(record)
            logging.info(
                "%s source=%s hash=%s title=%r imdb=%s tmdb=%s reason=%s attempt=%d",
                "METADATA_ACTIVATION" if is_metadata_activation else "ADD",
                cfg.name, grab.hash, grab.source_title, media.get("imdbId") or "",
                media.get("tmdbId") or "", reason, attempt,
            )
            if record is not None and not is_metadata_activation:
                # A live object already exists. Inspect it, but never submit a duplicate add.
                try:
                    details = fetch_torrserver_item(cfg, ts, grab.hash)
                    detail_evidence = verification_evidence(details)
                    if detail_evidence:
                        completed = db.confirm_pending(grab.hash)
                        if completed is not None:
                            imported_hashes.add(grab.hash)
                            added += 1
                            logging.info(
                                "ADDED source=%s hash=%s verification=%s recovery=action-get",
                                cfg.name, grab.hash, detail_evidence,
                            )
                        continue
                    db.observe_pending(
                        grab.hash, "action-get-incomplete: " + torrserver_status(details)
                        + "; " + str(file_stats_problem(details)),
                    )
                except HttpStatusError as exc:
                    db.observe_pending(
                        grab.hash, "action-get-not-found" if exc.code == 404 else "action-get-failed", str(exc),
                    )
                    logging.warning(
                        "METADATA_RECOVERY_GET_FAILED source=%s hash=%s http_status=%s error=%s",
                        cfg.name, grab.hash, exc.code, exc,
                    )
                except Exception as exc:
                    db.observe_pending(grab.hash, "action-get-failed", str(exc))
                    logging.warning(
                        "METADATA_RECOVERY_GET_FAILED source=%s hash=%s error=%s",
                        cfg.name, grab.hash, exc,
                    )
                continue

            if is_metadata_activation and not db.mark_metadata_activation_attempt(grab.hash, attempt):
                logging.info(
                    "METADATA_ACTIVATION_SKIPPED source=%s hash=%s attempt=%d reason=already-requested",
                    cfg.name, grab.hash, attempt,
                )
                continue

            try:
                response = add_to_torrserver(
                    cfg, ts, grab, media, resolved_link=resolved_link,
                )
                response_status = str(response.get("stat_string") or response.get("statString") or "accepted")
                event_status = (
                    "metadata-activation-accepted: " + response_status
                    if is_metadata_activation else "request-accepted: " + response_status
                )
                db.observe_pending(grab.hash, event_status)
                submitted += 1
                logging.info(
                    "%s source=%s hash=%s stat=%s attempt=%d",
                    "METADATA_ACTIVATION_ACCEPTED" if is_metadata_activation else "ADD_ACCEPTED",
                    cfg.name, grab.hash, response_status, attempt,
                )
            except HttpStatusError as exc:
                if 400 <= exc.code < 500 and not is_retryable_http(exc.code):
                    state = db.fail_pending(
                        grab.hash, str(exc), cfg.max_add_attempts,
                        cfg.retry_backoff_sec, permanent=True,
                    )
                    failed += 1
                    logging.error(
                        "ADD_REJECTED source=%s hash=%s http_status=%d state=%s error=%s",
                        cfg.name, grab.hash, exc.code, state, exc,
                    )
                else:
                    db.observe_pending(grab.hash, "request-outcome-uncertain", str(exc))
                    logging.warning(
                        "ADD_UNCERTAIN source=%s hash=%s error=%s; verify presence before retry",
                        cfg.name, grab.hash, exc,
                    )
            except Exception as exc:
                db.observe_pending(grab.hash, "request-outcome-uncertain", str(exc))
                logging.warning(
                    "ADD_UNCERTAIN source=%s hash=%s error=%s; verify presence before retry",
                    cfg.name, grab.hash, exc,
                )

        while True:
            active = [
                row for row in db.pending_rows(source=cfg.name)
                if row.get("status") == "pending"
            ]
            if not active:
                break

            try:
                current_records = fetch_torrserver(cfg, ts)
                current_by_hash = {
                    str(record.get("hash") or "").strip().lower(): record
                    for record in current_records
                    if str(record.get("hash") or "").strip()
                }
                poll_error = None
            except Exception as exc:
                current_by_hash = {}
                poll_error = str(exc)
                logging.warning(
                    "VERIFY_POLL_FAILED source=%s pending=%d error=%s",
                    cfg.name, len(active), exc,
                )

            now = datetime.now(timezone.utc)
            for pending in active:
                h = str(pending["hash"]).lower()
                record = current_by_hash.get(h)
                evidence = verification_evidence(record)
                if evidence:
                    completed = db.confirm_pending(h)
                    if completed is not None:
                        imported_hashes.add(h)
                        added += 1
                        requested = parse_iso_datetime(pending.get("requested_at")) or now
                        logging.info(
                            "ADDED source=%s hash=%s verification=%s attempt=%s elapsed=%.1fs",
                            cfg.name, h, evidence, pending.get("attempt_count"),
                            max(0.0, (now - requested).total_seconds()),
                        )
                    continue

                status_text = torrserver_status(record)
                if record is not None:
                    status_text += "; " + str(file_stats_problem(record))
                deadline = pending_deadline(pending)
                if deadline is not None and now >= deadline:
                    detail = poll_error or f"verification deadline expired; last observed status={status_text}"
                    previous_error = str(pending.get("last_error") or "").strip()
                    if previous_error and previous_error not in detail:
                        detail = detail + "; previous_error=" + previous_error[:500]
                    state = db.fail_pending(
                        h, detail, cfg.max_add_attempts, cfg.retry_backoff_sec,
                    )
                    failed += 1
                    requested = parse_iso_datetime(pending.get("requested_at")) or now
                    logging.error(
                        "VERIFY_FAILED source=%s hash=%s status=%s attempts=%s elapsed=%.1fs error=%s",
                        cfg.name, h, state, pending.get("attempt_count"),
                        max(0.0, (now - requested).total_seconds()), detail,
                    )
                else:
                    db.observe_pending(h, status_text, poll_error)

            active_after = [
                row for row in db.pending_rows(source=cfg.name)
                if row.get("status") == "pending"
            ]
            if not active_after:
                break
            now = datetime.now(timezone.utc)
            remaining = []
            for row in active_after:
                deadline = pending_deadline(row)
                if deadline is not None:
                    remaining.append(max(0.0, (deadline - now).total_seconds()))
            sleep_for = cfg.verification_poll_interval_sec
            if remaining:
                sleep_for = min(sleep_for, max(0.1, min(remaining)))
            time.sleep(sleep_for)

        process_radarr_blocklist(cfg, arr, db)
        process_sonarr_blocklist(cfg, arr, db)
        if cfg.seerr_watchlist_enabled:
            process_seerr_watchlist(cfg, db)
        dispatch_notifications(cfg, db)
        pending_count = len(db.pending_rows())
        logging.info(
            "DONE source=%s candidates=%d submitted=%d added=%d failed=%d pending=%d "
            "skipped_present=%d present_incomplete=%d skipped_known=%d duration=%.2fs",
            cfg.name, len(candidates), submitted, added, failed, pending_count,
            skipped_present, present_incomplete, skipped_known, time.monotonic() - started,
        )
        return 0 if failed == 0 else 1
    finally:
        db.close()
        lock.__exit__(None, None, None)


def load_toml(path: str) -> dict[str, Any]:
    try:
        import tomllib
    except ImportError as exc:
        raise ImporterError("Python 3.11+ with tomllib is required") from exc
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def config_from_file(path: str) -> Config:
    raw = load_toml(path)
    # Allow a flat TOML file for simple deployment.
    cfg = Config(
        name=str(raw["name"]),
        arr_type=str(raw["arr_type"]),
        arr_url=str(raw["arr_url"]),
        arr_api_key=str(raw["arr_api_key"]),
        torrserver_url=str(raw["torrserver_url"]),
        category=str(raw["category"]),
        target_quality=int(raw["target_quality"]),
        state_db=str(raw.get("state_db", "/var/lib/torr-arr-importer/state.db")),
        history_page_size=int(raw.get("history_page_size", DEFAULT_PAGE_SIZE)),
        timeout=int(raw.get("timeout", DEFAULT_TIMEOUT)),
        user_agent=str(raw.get("user_agent", "torr-arr-importer/1.3.5")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        log_file=str(raw.get("log_file", "")),
        retry_count=int(raw.get("retry_count", 2)),
        retry_delay=float(raw.get("retry_delay", 1.0)),
        resolve_download_url=bool(raw.get("resolve_download_url", True)),
        lock_file=str(raw.get("lock_file", "")),
        verification_timeout_sec=int(raw.get("verification_timeout_sec", 120)),
        verification_poll_interval_sec=float(raw.get("verification_poll_interval_sec", 3.0)),
        retry_backoff_sec=int(raw.get("retry_backoff_sec", 60)),
        max_add_attempts=int(raw.get("max_add_attempts", 3)),
        radarr_blocklist_enabled=bool(raw.get("radarr_blocklist_enabled", True)),
        sonarr_blocklist_enabled=bool(raw.get("sonarr_blocklist_enabled", True)),
        arr_ui_url=str(raw.get("arr_ui_url", "")),
        notifications_enabled=bool(raw.get("notifications_enabled", False)),
        apprise_url=str(raw.get("apprise_url", "")),
        apprise_config_key=str(raw.get("apprise_config_key", "arr-stack")),
        service_name=str(raw.get("service_name", "")),
        notification_timeout_sec=int(raw.get("notification_timeout_sec", 5)),
        notification_tag_critical=str(raw.get("notification_tag_critical", "critical")),
        notification_tag_warning=str(raw.get("notification_tag_warning", "warnings")),
        seerr_watchlist_enabled=bool(raw.get("seerr_watchlist_enabled", False)),
        seerr_url=str(raw.get("seerr_url", "")),
        seerr_api_key=str(raw.get("seerr_api_key", "")),
    )
    return cfg.normalized()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize Radarr/Sonarr Grab history into TorrServer")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", required=True, help="Path to TOML config")
    parser.add_argument("--rebuild", action="store_true", help="Ignore import history and restore all ARR grabs missing on TorrServer")
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without adding or modifying state")
    parser.add_argument("--retry-failed", action="store_true", help="Re-queue exhausted, unverified add attempts for this instance")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override configured log level")
    args = parser.parse_args(argv)
    if args.dry_run and args.retry_failed:
        parser.error("--retry-failed cannot be combined with --dry-run")

    try:
        cfg = config_from_file(args.config)
        level = args.log_level or cfg.log_level
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        root = logging.getLogger()
        root.setLevel(getattr(logging, level))
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
        if cfg.log_file:
            file_handler = logging.FileHandler(cfg.log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        return run(cfg, rebuild=args.rebuild, dry_run=args.dry_run, retry_failed=args.retry_failed)
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        logging.exception("FATAL error=%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
