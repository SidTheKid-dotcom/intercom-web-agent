from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.crawler import service as crawl_service
from services.url_ingest import ingest_url_list

router = APIRouter()


class CookiePayload(BaseModel):
    name: str
    value: str
    domain: Optional[str] = None
    path: Optional[str] = "/"
    secure: Optional[bool] = False
    httpOnly: Optional[bool] = False
    sameSite: Optional[str] = None
    expirationDate: Optional[float] = None


class SiteIngestRequest(BaseModel):
    start_url: str
    title: Optional[str] = None
    page_text: Optional[str] = None
    user_agent: str
    cookies: List[CookiePayload] = Field(default_factory=list)
    force_refresh: bool = False


class SiteIngestResponse(BaseModel):
    job_id: Optional[str] = None
    site_key: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    site_key: str
    status: str
    pages_discovered: int = 0
    pages_crawled: int = 0
    pdfs_discovered: int = 0
    pdfs_processed: int = 0
    chunks_written: int = 0
    failures: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class BrowserCrawlStartRequest(BaseModel):
    start_url: str
    title: Optional[str] = None
    force_refresh: bool = False


class BrowserCrawlPageRequest(BaseModel):
    job_id: str
    url: str
    title: Optional[str] = None
    page_text: str
    discovered_from: Optional[str] = None
    crawl_depth: int = 0
    discovered_count: Optional[int] = None


class BrowserCrawlFinishRequest(BaseModel):
    job_id: str
    status: str = "completed"
    error_summary: Optional[str] = None
    discovered_count: Optional[int] = None


class UrlListIngestRequest(BaseModel):
    urls: List[str]
    title: Optional[str] = None
    user_agent: str = "IntercomDemoIngest/1.0"
    cookies: List[CookiePayload] = Field(default_factory=list)
    force_refresh: bool = True


class UrlListIngestResponse(BaseModel):
    job_id: str
    site_key: str
    status: str
    html_pages: int = 0
    pdf_count: int = 0
    chunks_written: int = 0
    failures: int = 0


@router.post("/ingest/site", response_model=SiteIngestResponse)
async def ingest_site(request: SiteIngestRequest):
    if not request.start_url or not request.start_url.strip():
        raise HTTPException(status_code=400, detail="Missing start_url")

    result = await crawl_service.start_site_crawl(
        start_url=request.start_url,
        title=request.title,
        page_text=request.page_text,
        user_agent=request.user_agent,
        cookies=[cookie.dict(exclude_none=True) for cookie in request.cookies],
        force_refresh=request.force_refresh,
    )
    return SiteIngestResponse(**result)


@router.post("/ingest/urls", response_model=UrlListIngestResponse)
async def ingest_urls(request: UrlListIngestRequest):
    if not request.urls:
        raise HTTPException(status_code=400, detail="Missing urls")
    try:
        result = await ingest_url_list(
            urls=request.urls,
            title=request.title,
            user_agent=request.user_agent,
            cookies=[cookie.dict(exclude_none=True) for cookie in request.cookies],
            force_refresh=request.force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UrlListIngestResponse(**result)


@router.post("/ingest/browser-crawl/start", response_model=SiteIngestResponse)
async def start_browser_crawl(request: BrowserCrawlStartRequest):
    if not request.start_url or not request.start_url.strip():
        raise HTTPException(status_code=400, detail="Missing start_url")

    result = await crawl_service.start_browser_crawl(
        start_url=request.start_url,
        title=request.title,
        force_refresh=request.force_refresh,
    )
    return SiteIngestResponse(**result)


@router.post("/ingest/browser-crawl/page")
async def ingest_browser_crawl_page(request: BrowserCrawlPageRequest):
    if not request.job_id:
        raise HTTPException(status_code=400, detail="Missing job_id")
    if not request.url:
        raise HTTPException(status_code=400, detail="Missing url")
    if not request.page_text or not request.page_text.strip():
        raise HTTPException(status_code=400, detail="Missing page_text")

    try:
        return await crawl_service.ingest_browser_page(
            job_id=request.job_id,
            url=request.url,
            title=request.title,
            page_text=request.page_text,
            discovered_from=request.discovered_from,
            crawl_depth=request.crawl_depth,
            discovered_count=request.discovered_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ingest/browser-crawl/finish", response_model=SiteIngestResponse)
async def finish_browser_crawl(request: BrowserCrawlFinishRequest):
    if not request.job_id:
        raise HTTPException(status_code=400, detail="Missing job_id")
    try:
        result = await crawl_service.finish_browser_crawl(
            job_id=request.job_id,
            status=request.status,
            error_summary=request.error_summary,
            discovered_count=request.discovered_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SiteIngestResponse(**result)


@router.get("/ingest/site/{job_id}", response_model=JobStatusResponse)
async def get_site_job(job_id: str):
    job = crawl_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Crawl job not found")

    return JobStatusResponse(
        job_id=job["job_id"],
        site_key=job["site_key"],
        status=job["status"],
        pages_discovered=int(job.get("pages_discovered") or 0),
        pages_crawled=int(job.get("pages_crawled") or 0),
        pdfs_discovered=int(job.get("pdfs_discovered") or 0),
        pdfs_processed=int(job.get("pdfs_processed") or 0),
        chunks_written=int(job.get("chunks_written") or 0),
        failures=int(job.get("failures") or 0),
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
    )


@router.get("/check")
async def check_url(url: str) -> Dict[str, Any]:
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    return crawl_service.check_site(url)
