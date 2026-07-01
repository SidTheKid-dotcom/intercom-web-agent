import asyncio
import re
from typing import Any, Dict, List, Optional

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.embedder import embed_texts
from services.llm import chat
from services.pdf_utils import extract_pdf_documents
from services.retriever import query_site, resolve_site_for_url
from services.site_utils import append_pdf_page, is_pdf_url

router = APIRouter()
logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    url: str
    message: str
    conversation_history: List[ChatMessage] = Field(default_factory=list)
    live_page_context: Optional[str] = None
    live_page_snapshot: Optional[Dict[str, Any]] = None
    user_agent: Optional[str] = None
    cookies: List[dict] = Field(default_factory=list)


class SourceItem(BaseModel):
    type: str
    title: str
    url: str
    snippet: str
    page_number: Optional[int] = None
    section_id: Optional[str] = None
    section_selector: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    actions: List[dict] = Field(default_factory=list)
    sources: List[SourceItem] = Field(default_factory=list)


def _build_sources(context_chunks: List[dict]) -> List[SourceItem]:
    seen = set()
    sources: List[SourceItem] = []
    for item in context_chunks:
        metadata = item.get("metadata") or {}
        source_url = metadata.get("source_url") or ""
        page_number = metadata.get("page_number")
        key = (source_url, metadata.get("source_type"), page_number)
        if key in seen:
            continue
        seen.add(key)
        snippet = (item.get("document") or "").strip().replace("\n", " ")
        snippet = snippet[:220] + ("..." if len(snippet) > 220 else "")
        sources.append(
            SourceItem(
                type=metadata.get("source_type") or "html",
                title=metadata.get("title") or "Untitled",
                url=append_pdf_page(source_url, page_number)
                if (metadata.get("source_type") == "pdf" and source_url)
                else source_url,
                snippet=snippet,
                page_number=page_number,
                section_id=metadata.get("section_id"),
                section_selector=metadata.get("section_selector"),
            )
        )
        if len(sources) >= 6:
            break
    return sources


def _build_live_source(url: str, live_page_context: Optional[str]) -> List[SourceItem]:
    if not live_page_context or not live_page_context.strip():
        return []
    title = "Current page"
    for line in live_page_context.splitlines()[:12]:
        if line.lower().startswith("title:"):
            title = line.split(":", 1)[1].strip() or title
            break
        if line.lower().startswith("current pdf:"):
            title = line.split(":", 1)[1].strip() or "Current PDF"
            break
    snippet = re.sub(r"\s+", " ", live_page_context.strip())[:220]
    return [
        SourceItem(
            type="live",
            title=title,
            url=url,
            snippet=snippet + ("..." if len(snippet) >= 220 else ""),
        )
    ]


def _tokens(text: str) -> List[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in {"the", "and", "for", "with", "from", "that", "this", "show", "list", "find", "what", "where"}
    ]


def _rank_lines(question: str, blocks: List[str], limit: int = 8) -> List[str]:
    query_tokens = set(_tokens(question))
    if not query_tokens:
        return []
    candidates = []
    for block in blocks:
        for raw_line in (block or "").splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if len(line) < 20:
                continue
            line_tokens = set(_tokens(line))
            score = len(query_tokens & line_tokens)
            if score:
                candidates.append((score, len(line), line))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    output = []
    seen = set()
    for _, _, line in candidates:
        lowered = line.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(line)
        if len(output) >= limit:
            break
    return output


def _fallback_answer(question: str, live_page_context: Optional[str], context_chunks: List[dict]) -> Dict[str, Any]:
    blocks = []
    if live_page_context:
        blocks.append(live_page_context)
    blocks.extend(item.get("document") or "" for item in context_chunks)
    lines = _rank_lines(question, blocks)
    if not lines:
        return {
            "answer": "I could not find an exact answer in the current live page or indexed site chunks. I will avoid guessing; try navigating to the specific page/PDF or ask Live Agent to open the relevant source.",
            "actions": [],
        }

    calculation = _maybe_calculate(question, lines)
    prefix = "The model is unavailable right now, so here is the best exact evidence I found from the live page and indexed chunks:"
    evidence = "\n".join(f"- {line[:500]}" for line in lines[:6])
    answer = f"{prefix}\n{evidence}"
    if calculation:
        answer = f"{answer}\n\n{calculation}"
    return {"answer": answer, "actions": []}


def _maybe_calculate(question: str, lines: List[str]) -> Optional[str]:
    lowered = question.lower()
    if not any(word in lowered for word in ("sum", "total", "average", "mean", "calculate", "difference", "how many")):
        return None
    numbers = []
    for line in lines:
        for match in re.findall(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?", line):
            try:
                numbers.append(float(match.replace(",", "")))
            except ValueError:
                pass
    if not numbers:
        return "Calculation: I found relevant text, but no explicit numbers to calculate from."
    if "average" in lowered or "mean" in lowered:
        value = sum(numbers) / len(numbers)
        return f"Calculation from extracted numbers: ({' + '.join(_fmt_number(n) for n in numbers[:20])}) / {len(numbers)} = {_fmt_number(value)}"
    if "difference" in lowered and len(numbers) >= 2:
        value = numbers[0] - numbers[1]
        return f"Calculation from extracted numbers: {_fmt_number(numbers[0])} - {_fmt_number(numbers[1])} = {_fmt_number(value)}"
    value = sum(numbers)
    return f"Calculation from extracted numbers: {' + '.join(_fmt_number(n) for n in numbers[:20])} = {_fmt_number(value)}"


def _fmt_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _cookies_for_httpx(cookies: List[dict]) -> Dict[str, str]:
    output: Dict[str, str] = {}
    for cookie in cookies or []:
        name = cookie.get("name")
        if name:
            output[name] = cookie.get("value", "")
    return output


async def _extract_current_pdf_context(request: ChatRequest) -> Optional[str]:
    if not is_pdf_url(request.url):
        return None

    headers = {"User-Agent": request.user_agent or "Intercom/1.0"}
    try:
        async with httpx.AsyncClient(
            headers=headers,
            cookies=_cookies_for_httpx(request.cookies),
            follow_redirects=True,
            timeout=30,
        ) as client:
            response = await client.get(request.url)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("Could not fetch current PDF %s: %s", request.url, exc)
        return None

    content_type = (response.headers.get("content-type") or "").lower()
    if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
        return None

    try:
        documents = await asyncio.to_thread(extract_pdf_documents, response.content, request.url)
    except Exception as exc:
        logger.warning("Could not extract current PDF %s: %s", request.url, exc)
        return None

    if not documents:
        return None

    parts = [
        f"Current PDF: {documents[0].title}",
        f"URL: {request.url}",
        "",
        "--- PDF Pages ---",
    ]
    for doc in documents[:25]:
        parts.append(f"[Page {doc.page_number}]\n{doc.text[:2800]}")
    return "\n\n".join(parts)


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    resolved = resolve_site_for_url(request.url)
    site = resolved.get("site")
    live_page_context = request.live_page_context
    if not live_page_context or len(live_page_context.strip()) < 120:
        pdf_context = await _extract_current_pdf_context(request)
        if pdf_context:
            live_page_context = pdf_context

    context_chunks: List[dict] = []
    has_indexed_site = bool(
        site
        and site.get("status") in ("completed", "completed_with_issues")
        and int(site.get("chunk_count") or 0) > 0
    )
    if has_indexed_site:
        try:
            query_embedding = embed_texts([request.message])[0]
            context_chunks = query_site(resolved["site_key"], query_embedding, top_k=18)
        except Exception as exc:
            logger.warning("Indexed retrieval failed; continuing with live page context if available: %s", exc)
            if not live_page_context or not live_page_context.strip():
                raise
    elif not live_page_context or not live_page_context.strip():
        raise HTTPException(
            status_code=409,
            detail="This page is not indexed and no live page text could be read. Try Live Agent mode, a normal rendered page, or a directly accessible PDF URL.",
        )

    history = [
        {"role": msg.role, "content": msg.content}
        for msg in request.conversation_history
    ]
    try:
        result = chat(
            context_chunks,
            request.message,
            history,
            current_url=request.url,
            live_page_context=live_page_context,
        )
    except Exception as exc:
        message = str(exc)
        if "429" in message or "rate_limit" in message or "Too Many Requests" in message:
            logger.warning("Chat model quota hit; returning extractive fallback: %s", message)
            result = _fallback_answer(request.message, live_page_context, context_chunks)
        elif "413" in message or "Request too large" in message:
            logger.warning("Chat model payload too large; returning extractive fallback: %s", message)
            result = _fallback_answer(request.message, live_page_context, context_chunks)
        else:
            logger.exception("Chat request failed")
            result = _fallback_answer(request.message, live_page_context, context_chunks)
    sources = _build_live_source(request.url, live_page_context) + _build_sources(context_chunks)

    return ChatResponse(
        answer=result["answer"],
        actions=result.get("actions", []),
        sources=sources,
    )
