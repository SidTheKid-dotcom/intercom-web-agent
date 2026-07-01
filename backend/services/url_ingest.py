import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import async_playwright

from services import retriever, sqlite_store
from services.embedder import chunk_text, embed_texts
from services.pdf_utils import extract_pdf_documents
from services.site_utils import (
    BOUNDARY_MODE,
    build_site_key,
    get_registrable_domain,
    guess_title_from_url,
    is_pdf_url,
    normalize_url,
    source_key_for,
)

logger = logging.getLogger(__name__)

URL_INGEST_CONCURRENCY = 4
REQUEST_TIMEOUT = 35


@dataclass
class UrlIngestDocument:
    source_url: str
    source_type: str
    title: str
    text: str
    content_hash: str
    page_number: Optional[int] = None
    section_id: Optional[str] = None
    section_selector: Optional[str] = None


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _clean_text(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value or "")).strip()


def _cookies_for_httpx(cookies: List[Dict[str, Any]]) -> Dict[str, str]:
    return {
        cookie["name"]: cookie.get("value", "")
        for cookie in cookies or []
        if cookie.get("name")
    }


def _section_chunks(
    *,
    source_url: str,
    title: str,
    sections: List[Dict[str, Any]],
    fallback_text: str,
) -> List[UrlIngestDocument]:
    documents: List[UrlIngestDocument] = []
    for section in sections:
        text = _clean_text(section.get("text") or "")
        heading = _clean_text(section.get("heading") or "")
        if len(text) < 80:
            continue
        section_url = source_url
        if section.get("id"):
            section_url = f"{source_url}#{section['id']}"
        documents.append(
            UrlIngestDocument(
                source_url=section_url,
                source_type="html",
                title=f"{title} - {heading}" if heading else title,
                text=(
                    f"Title: {title}\n"
                    f"URL: {source_url}\n"
                    f"Section: {heading}\n"
                    f"Selector: {section.get('selector') or ''}\n\n"
                    f"{text}"
                ),
                content_hash=_hash_text(text),
                section_id=section.get("id"),
                section_selector=section.get("selector"),
            )
        )

    if documents:
        return documents

    text = _clean_text(fallback_text)
    if not text:
        return []
    return [
        UrlIngestDocument(
            source_url=source_url,
            source_type="html",
            title=title,
            text=f"Title: {title}\nURL: {source_url}\n\n{text}",
            content_hash=_hash_text(text),
            section_selector="body",
        )
    ]


async def _extract_html_documents(page, url: str) -> tuple[List[UrlIngestDocument], Dict[str, Any]]:
    await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(700)

    result = await page.evaluate(
        """
        () => {
          const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
          const abs = (value) => {
            try { return new URL(value, location.href).href; } catch (e) { return ''; }
          };
          const selectorFor = (el) => {
            if (!el) return '';
            if (el.id) return `#${CSS.escape(el.id)}`;
            const data = ['data-testid', 'data-test', 'data-cy'].find((attr) => el.getAttribute(attr));
            if (data) return `${el.tagName.toLowerCase()}[${data}="${CSS.escape(el.getAttribute(data))}"]`;
            let node = el;
            const parts = [];
            while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
              const tag = node.tagName.toLowerCase();
              const parent = node.parentElement;
              if (!parent) {
                parts.unshift(tag);
                break;
              }
              const siblings = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
              const index = siblings.indexOf(node) + 1;
              parts.unshift(`${tag}:nth-of-type(${index})`);
              node = parent;
            }
            return parts.join(' > ');
          };
          const title = document.title || location.href;
          const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
          const sections = [];
          for (const heading of headings) {
            const parts = [];
            let node = heading.nextElementSibling;
            while (node && !/^H[1-6]$/.test(node.tagName) && parts.join(' ').length < 5000) {
              const text = clean(node.innerText || node.textContent || '');
              if (text) parts.push(text);
              node = node.nextElementSibling;
            }
            const headingText = clean(heading.innerText || heading.textContent || '');
            const sectionText = [headingText, ...parts].join('\\n');
            if (sectionText.length > 80) {
              if (!heading.id && headingText) {
                heading.id = headingText.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 64);
              }
              sections.push({
                heading: headingText,
                id: heading.id || '',
                selector: selectorFor(heading),
                text: sectionText,
              });
            }
          }
          const links = Array.from(document.querySelectorAll('a[href]')).map((a) => ({
            href: abs(a.getAttribute('href')),
            text: clean(a.innerText || a.textContent || a.getAttribute('title') || ''),
          })).filter((item) => item.href);
          const images = Array.from(document.querySelectorAll('img[src]')).map((img) => ({
            src: abs(img.getAttribute('src')),
            alt: clean(img.getAttribute('alt') || img.getAttribute('title') || ''),
          })).filter((item) => item.src);
          return {
            url: location.href,
            title,
            bodyText: clean(document.body?.innerText || ''),
            sections,
            links,
            images,
          };
        }
        """
    )
    final_url = normalize_url(result.get("url") or url)
    docs = _section_chunks(
        source_url=final_url,
        title=result.get("title") or guess_title_from_url(final_url),
        sections=result.get("sections") or [],
        fallback_text=result.get("bodyText") or "",
    )
    metadata = {
        "final_url": final_url,
        "title": result.get("title") or guess_title_from_url(final_url),
        "links": result.get("links") or [],
        "images": result.get("images") or [],
    }
    return docs, metadata


async def _extract_pdf_documents(client: httpx.AsyncClient, url: str) -> tuple[List[UrlIngestDocument], Dict[str, Any]]:
    response = await client.get(url)
    response.raise_for_status()
    final_url = normalize_url(str(response.url))
    pdf_pages = await asyncio.to_thread(extract_pdf_documents, response.content, final_url)
    docs = [
        UrlIngestDocument(
            source_url=final_url,
            source_type="pdf",
            title=page.title,
            text=f"Title: {page.title}\nURL: {final_url}\nPDF page: {page.page_number}\n\n{page.text}",
            content_hash=page.content_hash,
            page_number=page.page_number,
            section_selector=f"pdf-page-{page.page_number}",
        )
        for page in pdf_pages
    ]
    return docs, {"final_url": final_url, "title": docs[0].title if docs else guess_title_from_url(final_url)}


def _store_documents(site_key: str, documents: List[UrlIngestDocument]) -> int:
    chunk_records: List[Dict[str, Any]] = []
    for document in documents:
        chunks = chunk_text(document.text, chunk_size=1400, overlap=350)
        for index, chunk in enumerate(chunks):
            chunk_records.append(
                {
                    "id": source_key_for(
                        site_key,
                        document.source_url,
                        document.source_type,
                        document.page_number,
                    )
                    + f":url:{index}",
                    "text": chunk,
                    "metadata": {
                        "site_key": site_key,
                        "source_url": document.source_url,
                        "source_type": document.source_type,
                        "title": document.title,
                        "page_number": document.page_number,
                        "section_id": document.section_id,
                        "section_selector": document.section_selector,
                        "chunk_index": index,
                    },
                }
            )
    if not chunk_records:
        return 0
    embeddings = embed_texts([record["text"] for record in chunk_records])
    retriever.store_site_chunks(site_key, chunk_records, embeddings)
    return len(chunk_records)


async def ingest_url_list(
    *,
    urls: List[str],
    title: Optional[str] = None,
    user_agent: str = "IntercomDemoIngest/1.0",
    cookies: Optional[List[Dict[str, Any]]] = None,
    force_refresh: bool = True,
) -> Dict[str, Any]:
    sqlite_store.init_db()
    normalized_urls = []
    seen = set()
    for raw_url in urls:
        if not raw_url or not raw_url.strip():
            continue
        normalized = normalize_url(raw_url.strip())
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_urls.append(normalized)
    if not normalized_urls:
        raise ValueError("No URLs provided")

    start_url = normalized_urls[0]
    parsed = urlparse(start_url)
    root_domain = get_registrable_domain(parsed.hostname or "")
    site_key = build_site_key(start_url, BOUNDARY_MODE)
    job_id = str(uuid.uuid4())

    sqlite_store.upsert_site(
        site_key,
        start_url,
        root_domain,
        BOUNDARY_MODE,
        title or guess_title_from_url(start_url),
        "running",
        html_pages=0 if force_refresh else None,
        pdf_count=0 if force_refresh else None,
        chunk_count=0 if force_refresh else None,
        last_job_id=job_id,
    )
    sqlite_store.create_crawl_job(job_id, site_key, start_url, "running", force_refresh)
    sqlite_store.update_crawl_job(job_id, started_at=sqlite_store._utc_now(), pages_discovered=len(normalized_urls))
    if force_refresh:
        retriever.reset_site_collection(site_key)
        sqlite_store.clear_site_sources(site_key)

    totals = {"html_pages": 0, "pdf_count": 0, "chunks_written": 0, "failures": 0}
    sem = asyncio.Semaphore(URL_INGEST_CONCURRENCY)
    headers = {"User-Agent": user_agent or "IntercomDemoIngest/1.0"}

    async with httpx.AsyncClient(
        headers=headers,
        cookies=_cookies_for_httpx(cookies or []),
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    ) as client:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=headers["User-Agent"], ignore_https_errors=True)

            async def process_url(url: str) -> None:
                async with sem:
                    source_type = "pdf" if is_pdf_url(url) else "html"
                    source_key = source_key_for(site_key, url, source_type)
                    sqlite_store.upsert_source(
                        source_key,
                        site_key,
                        url,
                        url,
                        source_type,
                        "running",
                        title=guess_title_from_url(url),
                        discovered_from="url_list",
                        crawl_depth=0,
                    )
                    try:
                        if source_type == "pdf":
                            documents, metadata = await _extract_pdf_documents(client, url)
                        else:
                            page = await context.new_page()
                            try:
                                documents, metadata = await _extract_html_documents(page, url)
                            finally:
                                await page.close()
                        chunks = await asyncio.to_thread(_store_documents, site_key, documents)
                        final_url = metadata.get("final_url") or url
                        sqlite_store.upsert_source(
                            source_key_for(site_key, final_url, source_type),
                            site_key,
                            final_url,
                            final_url,
                            source_type,
                            "completed",
                            title=metadata.get("title") or guess_title_from_url(final_url),
                            http_status=200,
                            content_hash=_hash_text("\n".join(doc.text for doc in documents)),
                            discovered_from="url_list",
                            crawl_depth=0,
                            chunk_count=chunks,
                            last_crawled_at=sqlite_store._utc_now(),
                        )
                        if source_type == "pdf":
                            totals["pdf_count"] += 1
                        else:
                            totals["html_pages"] += 1
                        totals["chunks_written"] += chunks
                    except Exception as exc:
                        logger.warning("URL ingest failed for %s: %s", url, exc)
                        totals["failures"] += 1
                        sqlite_store.upsert_source(
                            source_key,
                            site_key,
                            url,
                            url,
                            source_type,
                            "failed",
                            title=guess_title_from_url(url),
                            error_type=exc.__class__.__name__,
                            error_message=str(exc),
                            discovered_from="url_list",
                            crawl_depth=0,
                            last_crawled_at=sqlite_store._utc_now(),
                        )

            await asyncio.gather(*(process_url(url) for url in normalized_urls))
            await browser.close()

    status = "completed_with_issues" if totals["failures"] else "completed"
    sqlite_store.upsert_site(
        site_key,
        start_url,
        root_domain,
        BOUNDARY_MODE,
        title or guess_title_from_url(start_url),
        status,
        html_pages=totals["html_pages"],
        pdf_count=totals["pdf_count"],
        chunk_count=totals["chunks_written"],
        last_job_id=job_id,
    )
    sqlite_store.update_crawl_job(
        job_id,
        status=status,
        pages_crawled=totals["html_pages"],
        pdfs_processed=totals["pdf_count"],
        chunks_written=totals["chunks_written"],
        failures=totals["failures"],
        finished_at=sqlite_store._utc_now(),
    )
    return {
        "job_id": job_id,
        "site_key": site_key,
        "status": status,
        "html_pages": totals["html_pages"],
        "pdf_count": totals["pdf_count"],
        "chunks_written": totals["chunks_written"],
        "failures": totals["failures"],
    }
