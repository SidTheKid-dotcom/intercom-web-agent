import asyncio
import hashlib
import logging
import random
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from services.embedder import chunk_text, embed_texts
from services.pdf_utils import extract_pdf_documents
from services import retriever, sqlite_store
from services.site_utils import (
    BOUNDARY_MODE,
    build_site_key,
    get_registrable_domain,
    guess_title_from_url,
    is_pdf_url,
    is_same_site_url,
    normalize_url,
    source_key_for,
)

logger = logging.getLogger(__name__)

HTML_LIMIT = 2500
PDF_LIMIT = 500
MAX_DEPTH = 8
HTML_CONCURRENCY = 2
PDF_CONCURRENCY = 2
REQUEST_TIMEOUT = 30
RETRY_BUDGET = 3
HOST_DELAY_SECONDS = 0.75
CHALLENGE_PATTERNS = (
    "attention required",
    "verify you are human",
    "checking your browser",
    "cloudflare",
    "captcha",
    "access denied",
    "ddos protection",
)
READY_STATUSES = {"completed", "completed_with_issues"}
ACTIVE_STATUSES = {"queued", "running"}


@dataclass
class CrawlTarget:
    url: str
    depth: int
    discovered_from: Optional[str]


@dataclass
class CrawlDocument:
    source_url: str
    source_type: str
    title: str
    text: str
    content_hash: str
    discovered_from: Optional[str]
    crawl_depth: int
    page_number: Optional[int] = None


@dataclass
class CrawlState:
    job_id: str
    site_key: str
    start_url: str
    root_domain: str
    title: Optional[str]
    seed_page_text: Optional[str]
    user_agent: str
    cookies: List[Dict[str, Any]]
    force_refresh: bool
    pages_discovered: int = 0
    pages_crawled: int = 0
    pdfs_discovered: int = 0
    pdfs_processed: int = 0
    chunks_written: int = 0
    failures: int = 0
    navigation_entries: List[str] = field(default_factory=list)
    failure_messages: List[str] = field(default_factory=list)
    seen_html: Set[str] = field(default_factory=set)
    seen_pdf: Set[str] = field(default_factory=set)
    html_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pdf_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    host_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    host_access: Dict[str, float] = field(default_factory=dict)
    progress_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def status_payload(self) -> Dict[str, Any]:
        return {
            "status": "running",
            "pages_discovered": self.pages_discovered,
            "pages_crawled": self.pages_crawled,
            "pdfs_discovered": self.pdfs_discovered,
            "pdfs_processed": self.pdfs_processed,
            "chunks_written": self.chunks_written,
            "failures": self.failures,
        }


class CrawlBlockedError(Exception):
    pass


class CrawlStatusError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _same_site_cookies_for_playwright(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    converted = []
    same_site_map = {
        "strict": "Strict",
        "lax": "Lax",
        "no_restriction": "None",
        "unspecified": "Lax",
    }
    for cookie in cookies or []:
        item = {
            "name": cookie.get("name"),
            "value": cookie.get("value", ""),
            "domain": cookie.get("domain"),
            "path": cookie.get("path") or "/",
            "secure": bool(cookie.get("secure")),
            "httpOnly": bool(cookie.get("httpOnly")),
        }
        expires = cookie.get("expirationDate")
        if expires:
            item["expires"] = int(expires)
        same_site = str(cookie.get("sameSite") or "").lower()
        if same_site in same_site_map:
            item["sameSite"] = same_site_map[same_site]
        if item["name"] and item["domain"]:
            converted.append(item)
    return converted


def _cookies_for_httpx(cookies: List[Dict[str, Any]]) -> Dict[str, str]:
    output = {}
    for cookie in cookies or []:
        name = cookie.get("name")
        if name:
            output[name] = cookie.get("value", "")
    return output


def _build_html_document(result: Dict[str, Any], target: CrawlTarget) -> CrawlDocument:
    headings = "\n".join(result.get("headings") or [])
    interactive = "\n".join(result.get("interactive") or [])
    links = "\n".join(result.get("link_summaries") or [])
    body = result.get("body_text") or ""

    text = (
        f"Title: {result.get('title') or guess_title_from_url(result['url'])}\n"
        f"URL: {result['url']}\n"
        f"Description: {result.get('description') or ''}\n\n"
        f"--- Headings ---\n{headings}\n\n"
        f"--- Interactive Elements ---\n{interactive}\n\n"
        f"--- Page Content ---\n{body}\n\n"
        f"--- Links On Page ---\n{links}"
    ).strip()

    return CrawlDocument(
        source_url=result["url"],
        source_type="html",
        title=result.get("title") or guess_title_from_url(result["url"]),
        text=text,
        content_hash=_hash_text(text),
        discovered_from=target.discovered_from,
        crawl_depth=target.depth,
    )


def _looks_like_challenge(title: str, text: str, html: str) -> bool:
    combined = f"{title}\n{text}\n{html}".lower()
    return any(pattern in combined for pattern in CHALLENGE_PATTERNS)


def _extract_links(result_links: List[str], current_url: str) -> List[str]:
    links = []
    for raw in result_links or []:
        try:
            absolute = normalize_url(urljoin(current_url, raw))
        except Exception:
            continue
        links.append(absolute)
    return links


class SiteCrawlerService:
    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}

    async def start_site_crawl(
        self,
        start_url: str,
        title: Optional[str],
        page_text: Optional[str],
        user_agent: str,
        cookies: Optional[List[Dict[str, Any]]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        sqlite_store.init_db()

        normalized_url = normalize_url(start_url)
        parsed = urlparse(normalized_url)
        root_domain = get_registrable_domain(parsed.hostname or "")
        site_key = build_site_key(normalized_url, BOUNDARY_MODE)

        existing_site = sqlite_store.get_site(site_key)
        latest_job = sqlite_store.get_latest_job_for_site(site_key)
        if not force_refresh and existing_site:
            if existing_site.get("status") in READY_STATUSES and int(existing_site.get("chunk_count") or 0) > 0:
                return {
                    "job_id": existing_site.get("last_job_id") or (latest_job or {}).get("job_id"),
                    "site_key": site_key,
                    "status": existing_site.get("status"),
                }
            if latest_job and latest_job.get("status") in ACTIVE_STATUSES:
                return {
                    "job_id": latest_job["job_id"],
                    "site_key": site_key,
                    "status": latest_job["status"],
                }

        job_id = str(uuid.uuid4())
        sqlite_store.upsert_site(
            site_key,
            normalized_url,
            root_domain,
            BOUNDARY_MODE,
            title,
            "queued",
            html_pages=0 if force_refresh else None,
            pdf_count=0 if force_refresh else None,
            chunk_count=0 if force_refresh else None,
            last_job_id=job_id,
        )
        sqlite_store.create_crawl_job(job_id, site_key, normalized_url, "queued", force_refresh)

        state = CrawlState(
            job_id=job_id,
            site_key=site_key,
            start_url=normalized_url,
            root_domain=root_domain,
            title=title,
            seed_page_text=page_text,
            user_agent=user_agent or "IntercomBot/1.0",
            cookies=cookies or [],
            force_refresh=force_refresh,
        )
        task = asyncio.create_task(self._run_crawl(state))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

        return {"job_id": job_id, "site_key": site_key, "status": "queued"}

    def check_site(self, url: str) -> Dict[str, Any]:
        sqlite_store.init_db()
        normalized_url = normalize_url(url)
        site_key = build_site_key(normalized_url, BOUNDARY_MODE)
        site = sqlite_store.get_site(site_key)
        job = sqlite_store.get_latest_job_for_site(site_key)

        if not site:
            legacy = sqlite_store.get_ingest(normalized_url)
            if legacy and int(legacy.get("chunk_count") or 0) > 0:
                return {
                    "exists": True,
                    "status": "completed",
                    "chunk_count": int(legacy["chunk_count"]),
                    "site_key": site_key,
                }
            return {"exists": False, "status": "missing", "site_key": site_key}

        chunk_count = int(site.get("chunk_count") or 0)
        ready = site.get("status") in READY_STATUSES and chunk_count > 0
        response = {
            "exists": ready,
            "status": site.get("status"),
            "site_key": site_key,
            "chunk_count": chunk_count,
            "html_pages": int(site.get("html_pages") or 0),
            "pdf_count": int(site.get("pdf_count") or 0),
            "job_id": site.get("last_job_id"),
        }
        if job:
            response.update(
                {
                    "pages_discovered": int(job.get("pages_discovered") or 0),
                    "pages_crawled": int(job.get("pages_crawled") or 0),
                    "pdfs_discovered": int(job.get("pdfs_discovered") or 0),
                    "pdfs_processed": int(job.get("pdfs_processed") or 0),
                    "chunks_written": int(job.get("chunks_written") or 0),
                    "failures": int(job.get("failures") or 0),
                    "started_at": job.get("started_at"),
                    "finished_at": job.get("finished_at"),
                }
            )
        return response

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return sqlite_store.get_crawl_job(job_id)

    async def start_browser_crawl(
        self,
        *,
        start_url: str,
        title: Optional[str],
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        sqlite_store.init_db()
        normalized_url = normalize_url(start_url)
        parsed = urlparse(normalized_url)
        root_domain = get_registrable_domain(parsed.hostname or "")
        site_key = build_site_key(normalized_url, BOUNDARY_MODE)
        job_id = str(uuid.uuid4())

        sqlite_store.upsert_site(
            site_key,
            normalized_url,
            root_domain,
            BOUNDARY_MODE,
            title,
            "running",
            html_pages=0 if force_refresh else None,
            pdf_count=0 if force_refresh else None,
            chunk_count=0 if force_refresh else None,
            last_job_id=job_id,
        )
        sqlite_store.create_crawl_job(job_id, site_key, normalized_url, "running", force_refresh)
        sqlite_store.update_crawl_job(job_id, started_at=sqlite_store._utc_now())

        if force_refresh:
            retriever.reset_site_collection(site_key)
            sqlite_store.clear_site_sources(site_key)

        return {"job_id": job_id, "site_key": site_key, "status": "running"}

    async def ingest_browser_page(
        self,
        *,
        job_id: str,
        url: str,
        title: Optional[str],
        page_text: str,
        discovered_from: Optional[str],
        crawl_depth: int,
        discovered_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        job = sqlite_store.get_crawl_job(job_id)
        if not job:
            raise ValueError("Browser crawl job not found")

        site_key = job["site_key"]
        normalized_url = normalize_url(url)
        text = (page_text or "").strip()
        if not text:
            raise ValueError("Browser page text is empty")

        document = CrawlDocument(
            source_url=normalized_url,
            source_type="html",
            title=title or guess_title_from_url(normalized_url),
            text=text,
            content_hash=_hash_text(text),
            discovered_from=discovered_from,
            crawl_depth=crawl_depth,
        )
        try:
            chunk_count = await asyncio.to_thread(self._store_document, site_key, document)
        except Exception as exc:
            sqlite_store.upsert_source(
                source_key_for(site_key, normalized_url, "html"),
                site_key,
                normalized_url,
                normalized_url,
                "html",
                "failed",
                title=document.title,
                content_hash=document.content_hash,
                discovered_from=discovered_from,
                crawl_depth=crawl_depth,
                error_type="embedding_failed",
                error_message=str(exc),
                last_crawled_at=sqlite_store._utc_now(),
            )
            sqlite_store.record_failure(
                job_id,
                site_key,
                normalized_url,
                "embedding_failed",
                source_type="html",
                message=str(exc),
                retryable="429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc),
            )
            sqlite_store.update_crawl_job(
                job_id,
                failures=int(job.get("failures") or 0) + 1,
                pages_discovered=max(int(job.get("pages_discovered") or 0), discovered_count or 0),
            )
            return {
                "job_id": job_id,
                "site_key": site_key,
                "status": "page_failed",
                "indexed": False,
                "error": str(exc),
            }

        sqlite_store.upsert_source(
            source_key_for(site_key, normalized_url, "html"),
            site_key,
            normalized_url,
            normalized_url,
            "html",
            "indexed",
            title=document.title,
            content_hash=document.content_hash,
            discovered_from=discovered_from,
            crawl_depth=crawl_depth,
            chunk_count=chunk_count,
            last_crawled_at=sqlite_store._utc_now(),
        )

        current_pages = int(job.get("pages_crawled") or 0) + 1
        current_chunks = int(job.get("chunks_written") or 0) + chunk_count
        pages_discovered = max(int(job.get("pages_discovered") or 0), discovered_count or 0, current_pages)
        sqlite_store.update_crawl_job(
            job_id,
            pages_discovered=pages_discovered,
            pages_crawled=current_pages,
            chunks_written=current_chunks,
        )
        sqlite_store.upsert_site(
            site_key,
            job["start_url"],
            get_registrable_domain(urlparse(job["start_url"]).hostname or ""),
            BOUNDARY_MODE,
            title,
            "running",
            html_pages=current_pages,
            chunk_count=current_chunks,
            last_job_id=job_id,
        )
        return {
            "job_id": job_id,
            "site_key": site_key,
            "status": "running",
            "pages_crawled": current_pages,
            "chunks_written": current_chunks,
        }

    async def finish_browser_crawl(
        self,
        *,
        job_id: str,
        status: str = "completed",
        error_summary: Optional[str] = None,
        discovered_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        job = sqlite_store.get_crawl_job(job_id)
        if not job:
            raise ValueError("Browser crawl job not found")

        site_key = job["site_key"]
        chunks_written = int(job.get("chunks_written") or 0)
        failures = int(job.get("failures") or 0)
        pages_crawled = int(job.get("pages_crawled") or 0)
        pages_discovered = max(int(job.get("pages_discovered") or 0), discovered_count or 0, pages_crawled)
        final_status = status
        if chunks_written <= 0:
            final_status = "failed"
            error_summary = error_summary or "No browser-rendered pages were indexed."
        elif failures:
            final_status = "completed_with_issues"

        sqlite_store.update_crawl_job(
            job_id,
            status=final_status,
            pages_discovered=pages_discovered,
            pages_crawled=pages_crawled,
            chunks_written=chunks_written,
            failures=failures,
            error_summary=error_summary,
            finished_at=sqlite_store._utc_now(),
        )
        sqlite_store.upsert_site(
            site_key,
            job["start_url"],
            get_registrable_domain(urlparse(job["start_url"]).hostname or ""),
            BOUNDARY_MODE,
            None,
            final_status,
            html_pages=pages_crawled,
            chunk_count=chunks_written,
            last_job_id=job_id,
        )
        return {"job_id": job_id, "site_key": site_key, "status": final_status}

    async def _run_crawl(self, state: CrawlState) -> None:
        sqlite_store.update_crawl_job(
            state.job_id,
            status="running",
            started_at=sqlite_store._utc_now(),
        )
        sqlite_store.upsert_site(
            state.site_key,
            state.start_url,
            state.root_domain,
            BOUNDARY_MODE,
            state.title,
            "running",
            last_job_id=state.job_id,
        )

        if state.force_refresh:
            retriever.reset_site_collection(state.site_key)
            sqlite_store.clear_site_sources(state.site_key)

        try:
            if state.seed_page_text and state.seed_page_text.strip():
                seeded_chunks = await asyncio.to_thread(self._store_seed_page, state)
                async with state.progress_lock:
                    state.chunks_written += seeded_chunks
                    if seeded_chunks > 0:
                        state.pages_crawled += 1
                    await self._flush_progress(state)

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=state.user_agent,
                    ignore_https_errors=True,
                )
                cookies = _same_site_cookies_for_playwright(state.cookies)
                if cookies:
                    await context.add_cookies(cookies)

                async with httpx.AsyncClient(
                    headers={"User-Agent": state.user_agent},
                    cookies=_cookies_for_httpx(state.cookies),
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                ) as client:
                    await self._seed_targets(state, client)
                    await self._process_all_targets(state, context, client)
                    await self._store_navigation_index(state)

                await context.close()
                await browser.close()

            if state.chunks_written <= 0:
                final_status = "failed"
                if not state.failure_messages:
                    state.failure_messages.append("No content could be indexed for this site.")
            else:
                final_status = "completed_with_issues" if state.failures else "completed"
            sqlite_store.update_crawl_job(
                state.job_id,
                status=final_status,
                pages_discovered=state.pages_discovered,
                pages_crawled=state.pages_crawled,
                pdfs_discovered=state.pdfs_discovered,
                pdfs_processed=state.pdfs_processed,
                chunks_written=state.chunks_written,
                failures=state.failures,
                error_summary="; ".join(state.failure_messages[:5]) or None,
                finished_at=sqlite_store._utc_now(),
            )
            sqlite_store.upsert_site(
                state.site_key,
                state.start_url,
                state.root_domain,
                BOUNDARY_MODE,
                state.title,
                final_status,
                html_pages=state.pages_crawled,
                pdf_count=state.pdfs_processed,
                chunk_count=state.chunks_written,
                last_job_id=state.job_id,
            )
        except Exception as exc:
            logger.exception("Site crawl failed for %s", state.start_url)
            state.failures += 1
            message = str(exc)
            state.failure_messages.append(message)
            sqlite_store.record_failure(
                state.job_id,
                state.site_key,
                state.start_url,
                "job_failed",
                message=message,
                retryable=False,
            )
            sqlite_store.update_crawl_job(
                state.job_id,
                status="failed",
                failures=state.failures,
                error_summary="; ".join(state.failure_messages[:5]) or message,
                finished_at=sqlite_store._utc_now(),
            )
            sqlite_store.upsert_site(
                state.site_key,
                state.start_url,
                state.root_domain,
                BOUNDARY_MODE,
                state.title,
                "failed",
                html_pages=state.pages_crawled,
                pdf_count=state.pdfs_processed,
                chunk_count=state.chunks_written,
                last_job_id=state.job_id,
            )

    async def _seed_targets(self, state: CrawlState, client: httpx.AsyncClient) -> None:
        await self._enqueue_html(state, normalize_url(state.start_url), 0, None)

        seed_urls = [normalize_url(state.start_url)]
        robots_url = urljoin(state.start_url, "/robots.txt")
        sitemap_urls = {urljoin(state.start_url, "/sitemap.xml")}
        try:
            response = await client.get(robots_url)
            if response.status_code < 400 and response.text:
                for line in response.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sitemap_url = normalize_url(line.split(":", 1)[1].strip())
                        if sitemap_url:
                            sitemap_urls.add(sitemap_url)
        except Exception as exc:
            logger.info("robots.txt fetch skipped for %s: %s", state.start_url, exc)

        for sitemap_url in sitemap_urls:
            for discovered_url in await self._collect_sitemap_urls(client, sitemap_url, state.root_domain):
                seed_urls.append(discovered_url)

        for seed_url in seed_urls:
            if is_pdf_url(seed_url):
                await self._enqueue_pdf(state, seed_url, 0, None)
            else:
                await self._enqueue_html(state, seed_url, 0, None)

    async def _collect_sitemap_urls(
        self, client: httpx.AsyncClient, sitemap_url: str, root_domain: str
    ) -> List[str]:
        queue = deque([normalize_url(sitemap_url)])
        visited = set()
        collected: List[str] = []

        while queue and len(visited) < 25 and len(collected) < (HTML_LIMIT + PDF_LIMIT):
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            try:
                response = await client.get(current)
                if response.status_code >= 400:
                    continue
                xml_text = response.text
                root = ET.fromstring(xml_text)
            except Exception:
                continue

            ns_match = re.match(r"\{(.*)\}", root.tag or "")
            ns = {"ns": ns_match.group(1)} if ns_match else {}
            url_nodes = root.findall(".//ns:url/ns:loc", ns) if ns else root.findall(".//url/loc")
            if url_nodes:
                for node in url_nodes:
                    if node.text:
                        normalized = normalize_url(node.text.strip())
                        if is_same_site_url(normalized, root_domain):
                            collected.append(normalized)
                continue

            sitemap_nodes = root.findall(".//ns:sitemap/ns:loc", ns) if ns else root.findall(".//sitemap/loc")
            for node in sitemap_nodes:
                if node.text:
                    nested = normalize_url(node.text.strip())
                    if nested not in visited:
                        queue.append(nested)

        return collected

    async def _process_all_targets(
        self,
        state: CrawlState,
        context: BrowserContext,
        client: httpx.AsyncClient,
    ) -> None:
        html_workers = [
            asyncio.create_task(self._html_worker(state, context))
            for _ in range(HTML_CONCURRENCY)
        ]
        pdf_workers = [
            asyncio.create_task(self._pdf_worker(state, client))
            for _ in range(PDF_CONCURRENCY)
        ]

        await state.html_queue.join()
        await state.pdf_queue.join()

        for worker in html_workers + pdf_workers:
            worker.cancel()
        await asyncio.gather(*(html_workers + pdf_workers), return_exceptions=True)

    async def _html_worker(self, state: CrawlState, context: BrowserContext) -> None:
        while True:
            target = await state.html_queue.get()
            try:
                await self._process_html_target(state, context, target)
            finally:
                state.html_queue.task_done()

    async def _pdf_worker(self, state: CrawlState, client: httpx.AsyncClient) -> None:
        while True:
            target = await state.pdf_queue.get()
            try:
                await self._process_pdf_target(state, client, target)
            finally:
                state.pdf_queue.task_done()

    async def _process_html_target(
        self, state: CrawlState, context: BrowserContext, target: CrawlTarget
    ) -> None:
        final_url = normalize_url(target.url)
        source_key = source_key_for(state.site_key, final_url, "html")
        sqlite_store.upsert_source(
            source_key,
            state.site_key,
            final_url,
            final_url,
            "html",
            "fetching",
            title=guess_title_from_url(final_url),
            discovered_from=target.discovered_from,
            crawl_depth=target.depth,
        )

        for attempt in range(RETRY_BUDGET + 1):
            try:
                result = await self._fetch_html_page(state, context, final_url)
                final_page_url = normalize_url(result["url"])

                if is_pdf_url(final_page_url) or "application/pdf" in (result.get("content_type") or ""):
                    await self._enqueue_pdf(state, final_page_url, target.depth, target.discovered_from)
                    sqlite_store.upsert_source(
                        source_key,
                        state.site_key,
                        final_url,
                        final_page_url,
                        "html",
                        "redirected_to_pdf",
                        title=guess_title_from_url(final_page_url),
                        discovered_from=target.discovered_from,
                        crawl_depth=target.depth,
                    )
                    return

                if _looks_like_challenge(result.get("title", ""), result.get("body_text", ""), result.get("html", "")):
                    raise CrawlBlockedError("Challenge or anti-bot page detected")

                for link in _extract_links(result.get("links") or [], final_page_url):
                    if not is_same_site_url(link, state.root_domain):
                        continue
                    if is_pdf_url(link):
                        await self._enqueue_pdf(state, link, target.depth + 1, final_page_url)
                    else:
                        await self._enqueue_html(state, link, target.depth + 1, final_page_url)

                document = _build_html_document(result, CrawlTarget(final_page_url, target.depth, target.discovered_from))
                chunk_count = await asyncio.to_thread(self._store_document, state.site_key, document)
                state.navigation_entries.append(
                    f"{document.title} | {document.source_url} | {', '.join((result.get('headings') or [])[:3])}"
                )

                sqlite_store.upsert_source(
                    source_key_for(state.site_key, final_page_url, "html"),
                    state.site_key,
                    final_page_url,
                    final_page_url,
                    "html",
                    "indexed",
                    title=document.title,
                    http_status=result.get("status"),
                    content_hash=document.content_hash,
                    discovered_from=target.discovered_from,
                    crawl_depth=target.depth,
                    chunk_count=chunk_count,
                    last_crawled_at=sqlite_store._utc_now(),
                )
                async with state.progress_lock:
                    state.chunks_written += chunk_count
                    state.pages_crawled += 1
                    await self._flush_progress(state)
                return
            except CrawlBlockedError as exc:
                await self._record_target_failure(
                    state,
                    final_url,
                    "html",
                    "blocked",
                    str(exc),
                    status_code=403,
                    target=target,
                    retryable=False,
                )
                return
            except CrawlStatusError as exc:
                if exc.status_code in (401, 403):
                    await self._record_target_failure(
                        state,
                        final_url,
                        "html",
                        "blocked",
                        str(exc),
                        status_code=exc.status_code,
                        target=target,
                        retryable=False,
                    )
                    return
                retryable = exc.status_code == 429 or exc.status_code >= 500
                if retryable and attempt < RETRY_BUDGET:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.15, 0.75))
                    continue
                await self._record_target_failure(
                    state,
                    final_url,
                    "html",
                    "http_error",
                    str(exc),
                    status_code=exc.status_code,
                    target=target,
                    retryable=retryable,
                )
                return
            except Exception as exc:
                if attempt < RETRY_BUDGET:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.15, 0.75))
                    continue
                await self._record_target_failure(
                    state,
                    final_url,
                    "html",
                    "fetch_failed",
                    str(exc),
                    target=target,
                    retryable=False,
                )
                return

    async def _process_pdf_target(
        self, state: CrawlState, client: httpx.AsyncClient, target: CrawlTarget
    ) -> None:
        final_url = normalize_url(target.url)
        base_source_key = source_key_for(state.site_key, final_url, "pdf")
        sqlite_store.upsert_source(
            base_source_key,
            state.site_key,
            final_url,
            final_url,
            "pdf",
            "fetching",
            title=guess_title_from_url(final_url),
            discovered_from=target.discovered_from,
            crawl_depth=target.depth,
        )

        for attempt in range(RETRY_BUDGET + 1):
            try:
                await self._pace_host(final_url, state)
                response = await client.get(final_url)
                content_type = response.headers.get("content-type", "").lower()
                resolved_url = normalize_url(str(response.url))

                if response.status_code in (401, 403):
                    raise CrawlStatusError(response.status_code, f"Protected PDF fetch denied for {final_url}")
                if response.status_code == 429 or response.status_code >= 500:
                    raise CrawlStatusError(response.status_code, f"Transient PDF fetch error for {final_url}")
                if response.status_code >= 400:
                    raise CrawlStatusError(response.status_code, f"PDF fetch failed for {final_url}")

                if "html" in content_type and _looks_like_challenge("", response.text[:4000], response.text[:4000]):
                    raise CrawlBlockedError("Challenge or anti-bot page detected for PDF")
                if "pdf" not in content_type and not is_pdf_url(resolved_url):
                    if is_same_site_url(resolved_url, state.root_domain):
                        await self._enqueue_html(state, resolved_url, target.depth, target.discovered_from)
                    sqlite_store.upsert_source(
                        base_source_key,
                        state.site_key,
                        final_url,
                        resolved_url,
                        "pdf",
                        "non_pdf_target",
                        title=guess_title_from_url(resolved_url),
                        discovered_from=target.discovered_from,
                        crawl_depth=target.depth,
                    )
                    return

                pdf_documents = await asyncio.to_thread(extract_pdf_documents, response.content, resolved_url)
                if not pdf_documents:
                    raise RuntimeError("No extractable text found in PDF")

                total_chunks = 0
                for page_doc in pdf_documents:
                    document = CrawlDocument(
                        source_url=page_doc.source_url,
                        source_type="pdf",
                        title=page_doc.title,
                        text=page_doc.text,
                        content_hash=page_doc.content_hash,
                        discovered_from=target.discovered_from,
                        crawl_depth=target.depth,
                        page_number=page_doc.page_number,
                    )
                    page_chunks = await asyncio.to_thread(self._store_document, state.site_key, document)
                    total_chunks += page_chunks
                    sqlite_store.upsert_source(
                        source_key_for(state.site_key, resolved_url, "pdf", page_doc.page_number),
                        state.site_key,
                        resolved_url,
                        resolved_url,
                        "pdf",
                        "indexed",
                        title=page_doc.title,
                        http_status=response.status_code,
                        content_hash=page_doc.content_hash,
                        discovered_from=target.discovered_from,
                        crawl_depth=target.depth,
                        page_number=page_doc.page_number,
                        chunk_count=page_chunks,
                        last_crawled_at=sqlite_store._utc_now(),
                    )

                sqlite_store.upsert_source(
                    base_source_key,
                    state.site_key,
                    resolved_url,
                    resolved_url,
                    "pdf",
                    "indexed",
                    title=pdf_documents[0].title,
                    http_status=response.status_code,
                    content_hash=pdf_documents[0].content_hash,
                    discovered_from=target.discovered_from,
                    crawl_depth=target.depth,
                    chunk_count=total_chunks,
                    last_crawled_at=sqlite_store._utc_now(),
                )
                async with state.progress_lock:
                    state.chunks_written += total_chunks
                    state.pdfs_processed += 1
                    await self._flush_progress(state)
                return
            except CrawlBlockedError as exc:
                await self._record_target_failure(
                    state,
                    final_url,
                    "pdf",
                    "blocked",
                    str(exc),
                    target=target,
                    retryable=False,
                    status_code=403,
                )
                return
            except CrawlStatusError as exc:
                retryable = exc.status_code == 429 or exc.status_code >= 500
                if retryable and attempt < RETRY_BUDGET:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.15, 0.75))
                    continue
                await self._record_target_failure(
                    state,
                    final_url,
                    "pdf",
                    "http_error",
                    str(exc),
                    target=target,
                    retryable=retryable,
                    status_code=exc.status_code,
                )
                return
            except Exception as exc:
                if attempt < RETRY_BUDGET:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.15, 0.75))
                    continue
                await self._record_target_failure(
                    state,
                    final_url,
                    "pdf",
                    "extract_failed",
                    str(exc),
                    target=target,
                    retryable=False,
                )
                return

    async def _fetch_html_page(
        self, state: CrawlState, context: BrowserContext, url: str
    ) -> Dict[str, Any]:
        await self._pace_host(url, state)
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
            status = response.status if response else 200
            if status in (401, 403, 429) or status >= 500:
                raise CrawlStatusError(status, f"HTML fetch failed for {url} with {status}")
            if status >= 400:
                raise CrawlStatusError(status, f"HTML fetch failed for {url} with {status}")

            await page.wait_for_timeout(1500)
            result = await page.evaluate(
                """() => {
                    const title = document.title || '';
                    const description = document.querySelector('meta[name="description"]')?.content || '';
                    const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
                      .map((el) => `${el.tagName}: ${el.innerText.trim()}`)
                      .filter(Boolean)
                      .slice(0, 120);
                    const interactive = Array.from(
                        document.querySelectorAll('input:not([type="hidden"]), select, textarea, button, a.btn, a[role="button"]')
                    )
                      .map((el) => {
                        const tag = el.tagName.toLowerCase();
                        const idStr = el.id ? `#${el.id}` : '';
                        const classStr = typeof el.className === 'string' && el.className ? `.${el.className.split(' ').join('.')}` : '';
                        const nameStr = el.getAttribute('name') ? `[name="${el.getAttribute('name')}"]` : '';
                        const selector = `${tag}${idStr}${nameStr || classStr}`;
                        const label = el.labels && el.labels[0] ? el.labels[0].innerText.trim() : '';
                        const placeholder = el.getAttribute('placeholder') || '';
                        const text = (el.innerText || el.value || '').trim();
                        return `[${tag.toUpperCase()}] selector="${selector}" label="${label}" placeholder="${placeholder}" text="${text}"`;
                      })
                      .filter(Boolean)
                      .slice(0, 160);
                    const links = Array.from(document.querySelectorAll('a[href]'))
                      .map((el) => el.href)
                      .filter(Boolean)
                      .slice(0, 500);
                    const linkSummaries = Array.from(document.querySelectorAll('a[href]'))
                      .map((el) => {
                        const text = (el.innerText || '').trim();
                        return text ? `[${text}](${el.href})` : '';
                      })
                      .filter(Boolean)
                      .slice(0, 250);
                    const bodyText = document.body ? document.body.innerText : '';
                    return {
                      title,
                      description,
                      headings,
                      interactive,
                      links,
                      linkSummaries,
                      bodyText,
                      url: location.href
                    };
                }"""
            )
            html = await page.content()
            headers = response.headers if response else {}
            return {
                "title": result.get("title") or guess_title_from_url(str(page.url)),
                "description": result.get("description") or "",
                "headings": result.get("headings") or [],
                "interactive": result.get("interactive") or [],
                "links": result.get("links") or [],
                "link_summaries": result.get("linkSummaries") or [],
                "body_text": result.get("bodyText") or "",
                "url": normalize_url(result.get("url") or str(page.url)),
                "html": html,
                "status": status,
                "content_type": headers.get("content-type", ""),
            }
        finally:
            await page.close()

    async def _enqueue_html(
        self, state: CrawlState, url: str, depth: int, discovered_from: Optional[str]
    ) -> None:
        normalized = normalize_url(url)
        if depth > MAX_DEPTH or not is_same_site_url(normalized, state.root_domain):
            return
        async with state.progress_lock:
            if normalized in state.seen_html or len(state.seen_html) >= HTML_LIMIT:
                return
            state.seen_html.add(normalized)
            state.pages_discovered += 1
            await state.html_queue.put(CrawlTarget(normalized, depth, discovered_from))
            await self._flush_progress(state)

    async def _enqueue_pdf(
        self, state: CrawlState, url: str, depth: int, discovered_from: Optional[str]
    ) -> None:
        normalized = normalize_url(url)
        if depth > MAX_DEPTH or not is_same_site_url(normalized, state.root_domain):
            return
        async with state.progress_lock:
            if normalized in state.seen_pdf or len(state.seen_pdf) >= PDF_LIMIT:
                return
            state.seen_pdf.add(normalized)
            state.pdfs_discovered += 1
            await state.pdf_queue.put(CrawlTarget(normalized, depth, discovered_from))
            await self._flush_progress(state)

    async def _flush_progress(self, state: CrawlState) -> None:
        sqlite_store.update_crawl_job(
            state.job_id,
            pages_discovered=state.pages_discovered,
            pages_crawled=state.pages_crawled,
            pdfs_discovered=state.pdfs_discovered,
            pdfs_processed=state.pdfs_processed,
            chunks_written=state.chunks_written,
            failures=state.failures,
        )

    def _store_document(self, site_key: str, document: CrawlDocument) -> int:
        chunks = chunk_text(document.text)
        if not chunks:
            return 0

        embeddings = embed_texts(chunks)
        now = sqlite_store._utc_now()
        chunk_records = []
        for index, chunk in enumerate(chunks):
            chunk_records.append(
                {
                    "id": source_key_for(
                        site_key,
                        document.source_url,
                        document.source_type,
                        document.page_number or 0,
                    )
                    + f"_{index}",
                    "text": chunk,
                    "metadata": {
                        "site_key": site_key,
                        "source_url": document.source_url,
                        "source_type": document.source_type,
                        "title": document.title,
                        "page_number": document.page_number,
                        "content_hash": document.content_hash,
                        "discovered_from": document.discovered_from or "",
                        "crawl_depth": document.crawl_depth,
                        "last_crawled_at": now,
                    },
                }
            )

        retriever.store_site_chunks(site_key, chunk_records, embeddings)
        return len(chunk_records)

    def _store_seed_page(self, state: CrawlState) -> int:
        seed_text = (state.seed_page_text or "").strip()
        if not seed_text:
            return 0

        document = CrawlDocument(
            source_url=state.start_url,
            source_type="html",
            title=state.title or guess_title_from_url(state.start_url),
            text=seed_text,
            content_hash=_hash_text(seed_text),
            discovered_from=state.start_url,
            crawl_depth=0,
        )
        chunk_count = self._store_document(state.site_key, document)
        if chunk_count > 0:
            state.navigation_entries.append(f"{document.title} | {document.source_url} | seeded from extension")
            sqlite_store.upsert_source(
                source_key_for(state.site_key, state.start_url, "html"),
                state.site_key,
                state.start_url,
                state.start_url,
                "html",
                "indexed",
                title=document.title,
                content_hash=document.content_hash,
                discovered_from=state.start_url,
                crawl_depth=0,
                chunk_count=chunk_count,
                last_crawled_at=sqlite_store._utc_now(),
            )
        return chunk_count

    async def _store_navigation_index(self, state: CrawlState) -> None:
        if not state.navigation_entries:
            return
        lines = "\n".join(state.navigation_entries[:4000])
        text = (
            f"Site Navigation Index for {state.root_domain}\n"
            f"Start URL: {state.start_url}\n\n"
            f"{lines}"
        ).strip()
        document = CrawlDocument(
            source_url=state.start_url,
            source_type="html",
            title="Site Navigation Index",
            text=text,
            content_hash=_hash_text(text),
            discovered_from=state.start_url,
            crawl_depth=0,
        )
        chunk_count = await asyncio.to_thread(self._store_document, state.site_key, document)
        async with state.progress_lock:
            state.chunks_written += chunk_count
            await self._flush_progress(state)

    async def _record_target_failure(
        self,
        state: CrawlState,
        url: str,
        source_type: str,
        failure_type: str,
        message: str,
        *,
        target: CrawlTarget,
        retryable: bool,
        status_code: Optional[int] = None,
    ) -> None:
        logger.warning("%s failed for %s: %s", source_type.upper(), url, message)
        sqlite_store.upsert_source(
            source_key_for(state.site_key, url, source_type),
            state.site_key,
            url,
            url,
            source_type,
            "failed" if failure_type != "blocked" else "blocked",
            title=guess_title_from_url(url),
            http_status=status_code,
            discovered_from=target.discovered_from,
            crawl_depth=target.depth,
            error_type=failure_type,
            error_message=message,
            last_crawled_at=sqlite_store._utc_now(),
        )
        sqlite_store.record_failure(
            state.job_id,
            state.site_key,
            url,
            failure_type,
            source_type=source_type,
            status_code=status_code,
            message=message,
            retryable=retryable,
        )
        async with state.progress_lock:
            state.failures += 1
            state.failure_messages.append(f"{source_type}:{url} -> {message}")
            await self._flush_progress(state)

    async def _pace_host(self, url: str, state: CrawlState) -> None:
        hostname = (urlparse(url).hostname or "").lower()
        if not hostname:
            return
        async with state.host_lock:
            now = time.monotonic()
            last = state.host_access.get(hostname, 0.0)
            delay = HOST_DELAY_SECONDS - (now - last)
            if delay > 0:
                await asyncio.sleep(delay + random.uniform(0.05, 0.2))
            state.host_access[hostname] = time.monotonic()


service = SiteCrawlerService()
