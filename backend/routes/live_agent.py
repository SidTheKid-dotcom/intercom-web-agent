import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.llm import live_agent

router = APIRouter()
logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    role: str
    content: str


class LiveAgentRequest(BaseModel):
    url: str
    goal: str
    live_page_context: str
    live_page_snapshot: Optional[Dict[str, Any]] = None
    live_page_screenshot: Optional[Dict[str, Any]] = None
    action_history: List[dict] = Field(default_factory=list)
    conversation_history: List[ChatMessage] = Field(default_factory=list)


class LiveAgentResponse(BaseModel):
    answer: str
    status: str = "working"
    actions: List[dict] = Field(default_factory=list)


@router.post("/live-agent", response_model=LiveAgentResponse)
async def live_agent_endpoint(request: LiveAgentRequest):
    if not request.goal or not request.goal.strip():
        raise HTTPException(status_code=400, detail="Empty live-agent goal")
    if not request.live_page_context or not request.live_page_context.strip():
        raise HTTPException(status_code=400, detail="No live page context available")

    history = [
        {"role": msg.role, "content": msg.content}
        for msg in request.conversation_history
    ]
    try:
        result = live_agent(
            goal=request.goal.strip(),
            current_url=request.url,
            live_page_context=request.live_page_context,
            live_page_snapshot=request.live_page_snapshot,
            live_page_screenshot=request.live_page_screenshot,
            action_history=request.action_history[-6:],
            conversation_history=history[-4:],
        )
    except Exception as exc:
        logger.exception("Live agent decision failed")
        message = str(exc)
        if "429" in message or "rate_limit" in message or "Too Many Requests" in message:
            return LiveAgentResponse(
                answer="The live agent hit the model rate limit. Wait a moment and try again, or use a smaller/faster Groq model for live actions.",
                status="blocked",
                actions=[],
            )
        if "413" in message or "Request too large" in message:
            return LiveAgentResponse(
                answer="The current page is too large for the live agent request. I reduced future snapshots; try the instruction again.",
                status="blocked",
                actions=[],
            )
        return LiveAgentResponse(
            answer=f"Live agent could not decide the next step: {message}",
            status="blocked",
            actions=[],
        )

    return LiveAgentResponse(
        answer=result.get("answer", ""),
        status=result.get("status", "working"),
        actions=result.get("actions", []),
    )
