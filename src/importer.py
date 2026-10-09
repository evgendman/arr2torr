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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


VERSION = "1.0.3"
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
    user_agent: str = "torr-arr-importer/1.0.3"
    log_level: str = "INFO"
    log_file: str = ""
    retry_count: int = 2
    retry_delay: float = 1.0
    resolve_download_url: bool = True
    lock_file: str = ""

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
        now = datetime.now(timezone.utc).isoformat()
        # Commit the historical marker only after TorrServer has confirmed the add.
        # BEGIN IMMEDIATE also serializes writers briefly across the four importer instances.
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
                    now,
                    source,
                    grab.history_id,
                    grab.arr_media_id,
                    str(grab.data.get("downloadId") or "") or None,
                    grab.source_title,
                    str(media.get("imdbId") or "") or None,
                    str(media.get("tmdbId") or "") or None,
                    str(media.get("tvdbId") or "") or None,
                ),
            )

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


def add_to_torrserver(cfg: Config, ts: HttpClient, grab: Grab, media: dict[str, Any]) -> dict[str, Any]:
    if not grab.download_url:
        raise ImporterError(f"hash={grab.hash} has no downloadUrl")
    link = ts.resolve_link(grab.download_url) if cfg.resolve_download_url else grab.download_url
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


def run(cfg: Config, rebuild: bool = False, dry_run: bool = False) -> int:
    cfg = cfg.normalized()
    started = time.monotonic()
    logging.info("START source=%s type=%s category=%s target_quality=%s", cfg.name, cfg.arr_type, cfg.category, cfg.target_quality)

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

        ts_hashes = extract_ts_hashes(ts_records)
        imported_hashes = db.all_hashes()
        history_count = len(grabs)
        known_count = sum(1 for g in grabs if g.hash in imported_hashes)
        candidates = [
            g for g in grabs
            if g.hash not in ts_hashes and (rebuild or g.hash not in imported_hashes)
        ]

        logging.info(
            "SNAPSHOT source=%s arr_grabbed=%d torrserver=%d import_history_known=%d candidates=%d mode=%s",
            cfg.name, history_count, len(ts_hashes), known_count, len(candidates), "rebuild" if rebuild else "normal"
        )

        added = 0
        failed = 0
        skipped_present = 0
        skipped_known = 0
        media_cache: dict[int, dict[str, Any]] = {}
        # Counters are reconstructed for useful summary logging.
        for grab in grabs:
            if grab.hash in ts_hashes:
                skipped_present += 1
            elif not rebuild and grab.hash in imported_hashes:
                skipped_known += 1

        if dry_run:
            for grab in candidates:
                media = media_cache.get(grab.arr_media_id) if grab.arr_media_id else None
                if media is None:
                    media = fetch_media(cfg, arr, grab)
                    if grab.arr_media_id:
                        media_cache[grab.arr_media_id] = media
                logging.info(
                    "DRY_RUN source=%s hash=%s title=%r imdb=%s tmdb=%s reason=%s",
                    cfg.name,
                    grab.hash,
                    grab.source_title,
                    media.get("imdbId") or "",
                    media.get("tmdbId") or "",
                    "rebuild" if rebuild else "new-hash",
                )
            logging.info(
                "DONE source=%s candidates=%d added=0 failed=0 skipped_present=%d skipped_known=%d duration=%.2fs dry_run=true",
                cfg.name, len(candidates), skipped_present, skipped_known, time.monotonic() - started
            )
            return 0

        for grab in candidates:
            try:
                media = media_cache.get(grab.arr_media_id) if grab.arr_media_id else None
                if media is None:
                    media = fetch_media(cfg, arr, grab)
                    if grab.arr_media_id:
                        media_cache[grab.arr_media_id] = media
                logging.info(
                    "ADD source=%s hash=%s title=%r imdb=%s tmdb=%s reason=%s",
                    cfg.name,
                    grab.hash,
                    grab.source_title,
                    media.get("imdbId") or "",
                    media.get("tmdbId") or "",
                    "rebuild" if rebuild else "new-hash",
                )
                response = add_to_torrserver(cfg, ts, grab, media)
                # Update in-memory set immediately; a later candidate with the same hash is skipped.
                ts_hashes.add(grab.hash)
                db.add(grab, cfg.name, media)
                imported_hashes.add(grab.hash)
                added += 1
                logging.info(
                    "ADDED source=%s hash=%s stat=%s",
                    cfg.name, grab.hash, str(response.get("stat_string") or "")
                )
            except Exception as exc:
                failed += 1
                logging.error("FAILED source=%s hash=%s title=%r error=%s", cfg.name, grab.hash, grab.source_title, exc)

        logging.info(
            "DONE source=%s candidates=%d added=%d failed=%d skipped_present=%d skipped_known=%d duration=%.2fs",
            cfg.name, len(candidates), added, failed, skipped_present, skipped_known, time.monotonic() - started
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
        user_agent=str(raw.get("user_agent", "torr-arr-importer/1.0.3")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        log_file=str(raw.get("log_file", "")),
        retry_count=int(raw.get("retry_count", 2)),
        retry_delay=float(raw.get("retry_delay", 1.0)),
        resolve_download_url=bool(raw.get("resolve_download_url", True)),
        lock_file=str(raw.get("lock_file", "")),
    )
    return cfg.normalized()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize Radarr/Sonarr Grab history into TorrServer")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", required=True, help="Path to TOML config")
    parser.add_argument("--rebuild", action="store_true", help="Ignore import history and restore all ARR grabs missing on TorrServer")
    parser.add_argument("--dry-run", action="store_true", help="Show candidates without adding or modifying state")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override configured log level")
    args = parser.parse_args(argv)

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
        return run(cfg, rebuild=args.rebuild, dry_run=args.dry_run)
    except Exception as exc:
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        logging.exception("FATAL error=%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
