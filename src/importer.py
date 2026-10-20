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
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


VERSION = "1.1.0"
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
    user_agent: str = "torr-arr-importer/1.1.0"
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
        return replace(self, arr_type=arr_type, category=category, lock_file=lock_file)


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
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

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
            CREATE INDEX IF NOT EXISTS idx_pending_source_status
                ON pending_adds(source, status);
            """
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
                   imdb_id, tmdb_id, tvdb_id, requested_at, deadline_at, attempt_count,
                   status, last_checked_at, last_status, last_error, next_retry_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, 'request-started', NULL, NULL)
                ON CONFLICT(hash) DO UPDATE SET
                  source=excluded.source,
                  arr_history_id=excluded.arr_history_id,
                  arr_media_id=excluded.arr_media_id,
                  download_id=excluded.download_id,
                  title=excluded.title,
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
                  next_retry_at=NULL
                """,
                (
                    grab.hash, source, grab.history_id, grab.arr_media_id,
                    str(grab.data.get("downloadId") or grab.download_id or "") or None,
                    grab.source_title,
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
                    last_status = ?, last_error = NULL, next_retry_at = NULL
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

    def requeue_failed(self, source: str) -> int:
        """Explicitly re-arm exhausted, never-confirmed attempts for operator-directed retry."""
        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE pending_adds
                SET status = 'retry_wait', attempt_count = 0, next_retry_at = ?,
                    last_error = 'Manually re-queued with --retry-failed'
                WHERE source = ? AND status = 'failed'
                """,
                (iso_now(), source),
            )
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()


def normalize_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


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


def verification_evidence(record: dict[str, Any] | None) -> str | None:
    """Return observable evidence that an exact hash is ready or persisted."""
    if not isinstance(record, dict):
        return None

    status_text = str(record.get("stat_string") or record.get("statString") or "").strip().lower()
    status_num = as_int(record.get("stat"))
    file_stats = record.get("file_stats") or record.get("FileStats") or record.get("files")
    if isinstance(file_stats, list):
        for item in file_stats:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or item.get("Path") or "").strip()
            file_id = as_int(item.get("id", item.get("Id")))
            if path and file_id is not None and file_id > 0:
                return "file-stats"
    if status_num == 3 or "torrent working" in status_text:
        return "working-status"
    if status_num == 5 or "torrent in db" in status_text:
        return "database-presence"
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

        # Reconcile every unverified attempt against the current list before candidate
        # selection. If a previously failed hash is present but not ready, resume
        # observation instead of adding its magnet again.
        if not dry_run:
            for pending in db.pending_rows(source=cfg.name):
                status = str(pending.get("status") or "")
                if status not in {"pending", "retry_wait", "failed"}:
                    continue
                h = str(pending.get("hash") or "").lower()
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

                if h in ts_hashes and status in {"retry_wait", "failed"}:
                    observed = str(record.get("stat_string") or record.get("statString") or "present-not-ready")
                    db.resume_verification(h, cfg.verification_timeout_sec, observed)
                    logging.info(
                        "VERIFY_RESUMED source=%s hash=%s stat=%s reason=present-without-readiness",
                        cfg.name, h, observed,
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
            if grab.hash in ts_hashes:
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
            candidates.append(grab)

        logging.info(
            "SNAPSHOT source=%s arr_grabbed=%d torrserver=%d import_history_known=%d pending=%d candidates=%d mode=%s",
            cfg.name, history_count, len(ts_hashes), known_count, len(pending_map), len(candidates),
            "rebuild" if rebuild else "normal",
        )

        skipped_present = sum(1 for grab in grabs if grab.hash in ts_hashes)
        skipped_known = sum(1 for grab in grabs if grab.hash not in ts_hashes and not rebuild and grab.hash in imported_hashes)
        media_cache: dict[int, dict[str, Any]] = {}

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
            try:
                if media is None:
                    media = fetch_media(cfg, arr, grab)
                    if grab.arr_media_id:
                        media_cache[grab.arr_media_id] = media
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
                continue

            reason = (
                "retry-after-verification-failure" if grab.hash in retry_candidates
                else ("rebuild" if rebuild else "new-hash")
            )
            attempt = db.begin_pending(
                grab, cfg.name, media, cfg.verification_timeout_sec,
            )
            if attempt == 0:
                logging.info(
                    "ADD_SKIPPED source=%s hash=%s reason=shared-pending-claim",
                    cfg.name, grab.hash,
                )
                continue
            logging.info(
                "ADD source=%s hash=%s title=%r imdb=%s tmdb=%s reason=%s attempt=%d",
                cfg.name, grab.hash, grab.source_title, media.get("imdbId") or "",
                media.get("tmdbId") or "", reason, attempt,
            )
            try:
                response = add_to_torrserver(
                    cfg, ts, grab, media, resolved_link=resolved_link,
                )
                response_status = str(response.get("stat_string") or response.get("statString") or "accepted")
                db.observe_pending(grab.hash, "request-accepted: " + response_status)
                submitted += 1
                logging.info(
                    "ADD_ACCEPTED source=%s hash=%s stat=%s attempt=%d",
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

                status_text = (
                    str(record.get("stat_string") or record.get("statString") or "present-not-ready")
                    if record else "absent-from-list"
                )
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

        pending_count = len(db.pending_rows())
        logging.info(
            "DONE source=%s candidates=%d submitted=%d added=%d failed=%d pending=%d "
            "skipped_present=%d skipped_known=%d duration=%.2fs",
            cfg.name, len(candidates), submitted, added, failed, pending_count,
            skipped_present, skipped_known, time.monotonic() - started,
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
        user_agent=str(raw.get("user_agent", "torr-arr-importer/1.1.0")),
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
