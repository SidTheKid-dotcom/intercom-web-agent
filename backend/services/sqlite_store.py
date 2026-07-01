"""
SQLite persistence for crawl state, site readiness, and source metadata.

The vector index stays in ChromaDB; SQLite tracks crawl progress and the
metadata needed by the extension UI.
"""
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(_DATA_DIR, "intercom.db")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingested_pages (
                url TEXT PRIMARY KEY,
                title TEXT,
                chunk_count INTEGER NOT NULL,
                source TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                site_key TEXT PRIMARY KEY,
                start_url TEXT NOT NULL,
                root_domain TEXT NOT NULL,
                boundary_mode TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL,
                html_pages INTEGER NOT NULL DEFAULT 0,
                pdf_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                last_job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                job_id TEXT PRIMARY KEY,
                site_key TEXT NOT NULL,
                start_url TEXT NOT NULL,
                status TEXT NOT NULL,
                force_refresh INTEGER NOT NULL DEFAULT 0,
                pages_discovered INTEGER NOT NULL DEFAULT 0,
                pages_crawled INTEGER NOT NULL DEFAULT 0,
                pdfs_discovered INTEGER NOT NULL DEFAULT 0,
                pdfs_processed INTEGER NOT NULL DEFAULT 0,
                chunks_written INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                error_summary TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(site_key) REFERENCES sites(site_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_sources (
                source_key TEXT PRIMARY KEY,
                site_key TEXT NOT NULL,
                source_url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                source_type TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL,
                http_status INTEGER,
                content_hash TEXT,
                discovered_from TEXT,
                crawl_depth INTEGER NOT NULL DEFAULT 0,
                page_number INTEGER,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                last_crawled_at TEXT,
                error_type TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(site_key) REFERENCES sites(site_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                site_key TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_type TEXT,
                failure_type TEXT NOT NULL,
                status_code INTEGER,
                message TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES crawl_jobs(job_id),
                FOREIGN KEY(site_key) REFERENCES sites(site_key)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crawl_jobs_site_key
            ON crawl_jobs(site_key)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crawl_sources_site_key
            ON crawl_sources(site_key)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_crawl_sources_status
            ON crawl_sources(site_key, status)
            """
        )
        conn.commit()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


def upsert_ingest(
    url: str, title: Optional[str], chunk_count: int, source: str
) -> None:
    init_db()
    now = _utc_now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO ingested_pages (url, title, chunk_count, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                chunk_count=excluded.chunk_count,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (url, title, chunk_count, source, now),
        )
        conn.commit()


def get_ingest(url: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT url, title, chunk_count, source, updated_at "
            "FROM ingested_pages WHERE url = ?",
            (url,),
        ).fetchone()
    return _row_to_dict(row)


def upsert_site(
    site_key: str,
    start_url: str,
    root_domain: str,
    boundary_mode: str,
    title: Optional[str],
    status: str,
    *,
    html_pages: Optional[int] = None,
    pdf_count: Optional[int] = None,
    chunk_count: Optional[int] = None,
    last_job_id: Optional[str] = None,
) -> None:
    init_db()
    now = _utc_now()
    existing = get_site(site_key)
    values = {
        "title": title if title is not None else (existing or {}).get("title"),
        "html_pages": html_pages if html_pages is not None else (existing or {}).get("html_pages", 0),
        "pdf_count": pdf_count if pdf_count is not None else (existing or {}).get("pdf_count", 0),
        "chunk_count": chunk_count if chunk_count is not None else (existing or {}).get("chunk_count", 0),
        "last_job_id": last_job_id if last_job_id is not None else (existing or {}).get("last_job_id"),
    }
    created_at = (existing or {}).get("created_at", now)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sites (
                site_key, start_url, root_domain, boundary_mode, title, status,
                html_pages, pdf_count, chunk_count, last_job_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site_key) DO UPDATE SET
                start_url=excluded.start_url,
                root_domain=excluded.root_domain,
                boundary_mode=excluded.boundary_mode,
                title=excluded.title,
                status=excluded.status,
                html_pages=excluded.html_pages,
                pdf_count=excluded.pdf_count,
                chunk_count=excluded.chunk_count,
                last_job_id=excluded.last_job_id,
                updated_at=excluded.updated_at
            """,
            (
                site_key,
                start_url,
                root_domain,
                boundary_mode,
                values["title"],
                status,
                values["html_pages"],
                values["pdf_count"],
                values["chunk_count"],
                values["last_job_id"],
                created_at,
                now,
            ),
        )
        conn.commit()


def get_site(site_key: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sites WHERE site_key = ?", (site_key,)).fetchone()
    return _row_to_dict(row)


def create_crawl_job(
    job_id: str, site_key: str, start_url: str, status: str, force_refresh: bool
) -> None:
    init_db()
    now = _utc_now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO crawl_jobs (
                job_id, site_key, start_url, status, force_refresh,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, site_key, start_url, status, 1 if force_refresh else 0, now, now),
        )
        conn.commit()


def update_crawl_job(job_id: str, **fields: Any) -> None:
    init_db()
    if not fields:
        return

    fields["updated_at"] = _utc_now()
    columns = ", ".join(f"{key} = ?" for key in fields.keys())
    values = list(fields.values())
    values.append(job_id)

    with _connect() as conn:
        conn.execute(
            f"UPDATE crawl_jobs SET {columns} WHERE job_id = ?",
            values,
        )
        conn.commit()


def get_crawl_job(job_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM crawl_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_latest_job_for_site(site_key: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM crawl_jobs
            WHERE site_key = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (site_key,),
        ).fetchone()
    return _row_to_dict(row)


def upsert_source(
    source_key: str,
    site_key: str,
    source_url: str,
    canonical_url: str,
    source_type: str,
    status: str,
    *,
    title: Optional[str] = None,
    http_status: Optional[int] = None,
    content_hash: Optional[str] = None,
    discovered_from: Optional[str] = None,
    crawl_depth: int = 0,
    page_number: Optional[int] = None,
    chunk_count: int = 0,
    last_crawled_at: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    init_db()
    now = _utc_now()
    existing = get_source(source_key)
    created_at = (existing or {}).get("created_at", now)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO crawl_sources (
                source_key, site_key, source_url, canonical_url, source_type, title,
                status, http_status, content_hash, discovered_from, crawl_depth,
                page_number, chunk_count, last_crawled_at, error_type, error_message,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                site_key=excluded.site_key,
                source_url=excluded.source_url,
                canonical_url=excluded.canonical_url,
                source_type=excluded.source_type,
                title=excluded.title,
                status=excluded.status,
                http_status=excluded.http_status,
                content_hash=excluded.content_hash,
                discovered_from=excluded.discovered_from,
                crawl_depth=excluded.crawl_depth,
                page_number=excluded.page_number,
                chunk_count=excluded.chunk_count,
                last_crawled_at=excluded.last_crawled_at,
                error_type=excluded.error_type,
                error_message=excluded.error_message,
                updated_at=excluded.updated_at
            """,
            (
                source_key,
                site_key,
                source_url,
                canonical_url,
                source_type,
                title,
                status,
                http_status,
                content_hash,
                discovered_from,
                crawl_depth,
                page_number,
                chunk_count,
                last_crawled_at,
                error_type,
                error_message,
                created_at,
                now,
            ),
        )
        conn.commit()


def get_source(source_key: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM crawl_sources WHERE source_key = ?",
            (source_key,),
        ).fetchone()
    return _row_to_dict(row)


def list_sources(site_key: str, *, source_type: Optional[str] = None) -> Iterable[Dict[str, Any]]:
    init_db()
    query = "SELECT * FROM crawl_sources WHERE site_key = ?"
    params = [site_key]
    if source_type:
        query += " AND source_type = ?"
        params.append(source_type)
    query += " ORDER BY source_type, canonical_url, COALESCE(page_number, 0)"

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def clear_site_sources(site_key: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM crawl_sources WHERE site_key = ?", (site_key,))
        conn.execute("DELETE FROM crawl_failures WHERE site_key = ?", (site_key,))
        conn.commit()


def record_failure(
    job_id: str,
    site_key: str,
    source_url: str,
    failure_type: str,
    *,
    source_type: Optional[str] = None,
    status_code: Optional[int] = None,
    message: Optional[str] = None,
    retryable: bool = False,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO crawl_failures (
                job_id, site_key, source_url, source_type, failure_type,
                status_code, message, retryable, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                site_key,
                source_url,
                source_type,
                failure_type,
                status_code,
                message,
                1 if retryable else 0,
                _utc_now(),
            ),
        )
        conn.commit()


def list_failures(job_id: str) -> Iterable[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM crawl_failures
            WHERE job_id = ?
            ORDER BY created_at ASC
            """,
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]

