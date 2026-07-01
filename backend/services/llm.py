import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

try:
    from groq import Groq
except ImportError:
    Groq = None

logger = logging.getLogger(__name__)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
LIVE_AGENT_GROQ_MODEL = os.getenv(
    "LIVE_AGENT_GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"
)
CHAT_CONTEXT_CHARS = int(os.getenv("CHAT_CONTEXT_CHARS", "9000"))
CHAT_LIVE_CONTEXT_CHARS = int(os.getenv("CHAT_LIVE_CONTEXT_CHARS", "3500"))
CHAT_CHUNK_CHARS = int(os.getenv("CHAT_CHUNK_CHARS", "850"))
CHAT_HISTORY_CHARS = int(os.getenv("CHAT_HISTORY_CHARS", "1800"))
LIVE_AGENT_CONTEXT_CHARS = int(os.getenv("LIVE_AGENT_CONTEXT_CHARS", "4200"))
LIVE_AGENT_HISTORY_CHARS = int(os.getenv("LIVE_AGENT_HISTORY_CHARS", "1400"))

_gemini_client = None
_groq_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return _gemini_client


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"), max_retries=0)
    return _groq_client


SYSTEM_PROMPT = """You are Intercom, an AI assistant that answers questions about a crawled website and helps users navigate it.

You will receive:
- retrieved chunks from the full-site knowledge base
- an optional live snapshot of the current page
- the user's question

Rules:
- Use the provided context only. Never invent names, numbers, departments, links, policies, or PDF facts.
- Treat the live page snapshot as the strongest evidence for the currently opened page/PDF. Use indexed chunks as supporting context, not as a replacement for the live page.
- If indexed chunks are partial or appear to be about a different page/department than the user's request, say that clearly and use live page evidence or navigation actions instead.
- Prefer exact quoted facts from the site's own pages and PDFs. Mention when a fact came from a PDF page if that is important.
- For mathematical questions over PDFs or tables, calculate only from numbers explicitly present in the context. Show the arithmetic briefly. If required numbers are missing, say which number is missing.
- For browser actions, only use URLs and selectors grounded in the provided context.
- If the user asks to go to a page or open a PDF, prefer a navigate action with the exact URL from context. PDFs may include #page=N deep links.
- If the user asks to click, type, scroll, or highlight on the current page, use the live page snapshot when available.
- If the user asks for data that is not on the current live page but a grounded link/action can get closer, return that action and say you are opening the likely source instead of pretending to know the answer.
- If the answer is not found after using live page + indexed chunks, return a concise “I could not find that in the available page/site context” and no fabricated answer.

Return JSON only, with this shape:
{
  "answer": "Your response",
  "actions": [
    { "type": "navigate", "url": "https://example.com" }
  ]
}

Available actions:
- navigate: { "type": "navigate", "url": "https://example.com/page" }
- scroll_to: { "type": "scroll_to", "selector": "css selector string" }
- highlight: { "type": "highlight", "text": "text to find and highlight on page" }
- click: { "type": "click", "selector": "css selector string" }
- type_text: { "type": "type_text", "selector": "css selector string", "text": "text to type into input" }
"""


LIVE_AGENT_PROMPT = """You are Intercom Live Agent, a browser automation assistant that helps users complete tasks on the current web page.

You will receive:
- the user's goal
- the current page URL
- a structured live snapshot of visible page text, forms, fields, buttons, links, refs, geometry, and diffs
- actions already attempted in this live run

Decide the next small set of browser actions needed to progress the goal.

Rules:
- Prefer refs from the live snapshot. Use selectors only when an older snapshot does not include refs.
- Use only refs, selectors, and URLs present in the live page snapshot.
- Prefer direct navigate actions for links with href. Do not click a top-level menu link repeatedly if it does not change the page.
- Use hover on menus/dropdowns when a submenu must be revealed.
- Hidden nav links marked hidden_nav_link may be used for navigate actions because their URLs are grounded in the page.
- Prefer filling visible form fields before clicking a submit/continue button.
- Keep each response small: at most 3 actions.
- When uncertain, return one high-confidence action instead of a large batch.
- Do not repeat the same action, ref, or selector if it already failed or did not visibly progress the page.
- If repeated clicks do not change the page, switch strategy or return status "needs_user" with a brief explanation.
- If the page asks for missing personal details, payment details, credentials, legal consent, medical/legal/financial attestations, or any destructive/irreversible action, stop and ask the user for confirmation or missing information.
- Do not invent personal data. If the user did not provide a required field value and it cannot be inferred from the page or prior messages, ask for it.
- Do not submit final forms that create accounts, place orders, make payments, sign contracts, or accept terms unless the user explicitly asked for that exact action.
- If the goal is complete, return status "done" and no actions.
- If you cannot safely continue, return status "needs_user" or "blocked" and explain why.

Return JSON only, with this shape:
{
  "answer": "Short plain-English update for the user",
  "status": "working",
  "actions": [
    { "type": "type_text", "ref": "e3", "text": "user@example.com" }
  ]
}

Statuses:
- working: actions are needed now
- done: the user goal appears complete
- needs_user: more user input or confirmation is required
- blocked: the page prevents progress or the task cannot be completed safely

Available actions:
- navigate: { "type": "navigate", "url": "https://example.com/page" }
- scroll_to: { "type": "scroll_to", "ref": "e12" }
- highlight: { "type": "highlight", "text": "text to find and highlight on page" }
- click: { "type": "click", "ref": "e5" }
- hover: { "type": "hover", "ref": "e5" }
- type_text: { "type": "type_text", "ref": "e3", "text": "text to type into input" }
- select_value: { "type": "select_value", "ref": "e8", "value": "option value or visible label" }
- check: { "type": "check", "ref": "e9", "checked": true }
- submit: { "type": "submit", "ref": "f1" }

If you must use a selector fallback, include both ref (if known) and selector.
"""


def _format_context_chunks(context_chunks: List[Dict], *, max_total_chars: int = CHAT_CONTEXT_CHARS) -> str:
    if not context_chunks:
        return "No indexed site context available."

    formatted = []
    for index, item in enumerate(context_chunks, start=1):
        metadata = item.get("metadata") or {}
        source_type = metadata.get("source_type") or "html"
        page_number = metadata.get("page_number")
        page_suffix = f" (page {page_number})" if page_number else ""
        formatted.append(
            f"[Source {index}] {metadata.get('title') or 'Untitled'}\n"
            f"Type: {source_type}{page_suffix}\n"
            f"URL: {metadata.get('source_url') or ''}\n"
            f"Content:\n{_truncate_text(item.get('document') or '', CHAT_CHUNK_CHARS)}"
        )
        if len("\n\n---\n\n".join(formatted)) >= max_total_chars:
            break
    return _truncate_text("\n\n---\n\n".join(formatted), max_total_chars)


def _compact_history(conversation_history: Optional[List[Dict]], max_chars: int = CHAT_HISTORY_CHARS) -> List[Dict]:
    if not conversation_history:
        return []
    compacted = []
    used = 0
    for msg in reversed(conversation_history[-8:]):
        content = _truncate_text(str(msg.get("content") or ""), 500)
        used += len(content)
        if used > max_chars:
            break
        compacted.append({"role": msg.get("role") or "user", "content": content})
    compacted.reverse()
    return compacted


def chat(
    context_chunks: List[Dict],
    message: str,
    conversation_history: Optional[List[Dict]] = None,
    *,
    current_url: str,
    live_page_context: Optional[str] = None,
) -> Dict:
    site_context = _format_context_chunks(context_chunks)
    live_context = _truncate_text(
        live_page_context.strip() if live_page_context else "No live page snapshot provided.",
        CHAT_LIVE_CONTEXT_CHARS,
    )

    user_message = f"""Current page URL: {current_url}

Indexed site context:
{site_context}

Live page snapshot:
{live_context}

User question: {message}
"""

    if LLM_PROVIDER == "groq":
        if not Groq:
            raise RuntimeError("groq package is not installed. Run `pip install groq`")
        logger.info("Using Groq model: %s", GROQ_MODEL)
        return _chat_groq(user_message, _compact_history(conversation_history))

    logger.info("Using Gemini model: %s", GEMINI_MODEL)
    return _chat_gemini(user_message, _compact_history(conversation_history))


def live_agent(
    *,
    goal: str,
    current_url: str,
    live_page_context: str,
    live_page_snapshot: Optional[Dict[str, Any]] = None,
    live_page_screenshot: Optional[Dict[str, Any]] = None,
    action_history: Optional[List[Dict]] = None,
    conversation_history: Optional[List[Dict]] = None,
) -> Dict:
    attempted_actions = _truncate_text(
        json.dumps(action_history or [], ensure_ascii=False, indent=2),
        LIVE_AGENT_HISTORY_CHARS,
    )
    live_context = _format_live_snapshot(live_page_snapshot, live_page_context)
    screenshot_context = _format_screenshot_context(live_page_screenshot)
    user_message = f"""Goal: {goal}

Current page URL: {current_url}

Live page snapshot:
{live_context}

Screenshot mode:
{screenshot_context}

Actions already attempted in this live run:
{attempted_actions}
"""

    if LLM_PROVIDER == "groq":
        if not Groq:
            raise RuntimeError("groq package is not installed. Run `pip install groq`")
        logger.info("Using Groq model for live agent: %s", LIVE_AGENT_GROQ_MODEL)
        return _chat_groq(
            user_message,
            conversation_history,
            system_prompt=LIVE_AGENT_PROMPT,
            model=LIVE_AGENT_GROQ_MODEL,
        )

    logger.info("Using Gemini model for live agent: %s", GEMINI_MODEL)
    return _chat_gemini(user_message, conversation_history, system_prompt=LIVE_AGENT_PROMPT)


def _truncate_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _format_live_snapshot(snapshot: Optional[Dict[str, Any]], fallback_text: Optional[str]) -> str:
    if not snapshot:
        return _compact_live_context(fallback_text)

    page = snapshot.get("page") or {}
    summary = snapshot.get("summary") or {}
    elements = snapshot.get("elements") or []
    forms = snapshot.get("forms") or []
    links = snapshot.get("links") or []
    diff = snapshot.get("diff") or {}

    heading_lines = []
    for heading in (summary.get("headings") or [])[:16]:
        text = _truncate_text(str(heading.get("text") or ""), 140)
        if text:
            heading_lines.append(f"{heading.get('level', 'h')}: {text}")

    form_lines = []
    for form in forms[:10]:
        controls = ", ".join(form.get("controls") or [])
        form_lines.append(
            f"ref={form.get('ref')} label=\"{_truncate_text(str(form.get('label') or ''), 100)}\" controls={controls}"
        )

    element_lines = []
    for item in elements[:80]:
        label = _truncate_text(str(item.get("label") or ""), 120)
        text = _truncate_text(str(item.get("text") or ""), 120)
        value = _truncate_text(str(item.get("value") or ""), 80)
        bits = [
            f"ref={item.get('ref')}",
            f"<{item.get('tag')}>",
            f"role={item.get('role')}" if item.get("role") else "",
            f"type={item.get('type')}" if item.get("type") else "",
            f"label=\"{label}\"" if label else "",
            f"text=\"{text}\"" if text else "",
            f"value=\"{value}\"" if value else "",
            f"href=\"{item.get('href')}\"" if item.get("href") else "",
            "required" if item.get("required") else "",
            "disabled" if item.get("disabled") else "",
            "in_viewport" if item.get("inViewport") else "offscreen",
        ]
        options = item.get("options") or []
        if options:
            option_text = ", ".join(
                f"\"{_truncate_text(str(opt.get('label') or opt.get('value') or ''), 50)}\" value=\"{opt.get('value', '')}\""
                for opt in options[:16]
            )
            bits.append(f"options=[{option_text}]")
        element_lines.append(" | ".join(bit for bit in bits if bit))

    link_lines = []
    for link in links[:24]:
        link_lines.append(
            f"ref={link.get('ref')} text=\"{_truncate_text(str(link.get('text') or ''), 90)}\" href=\"{link.get('href', '')}\""
        )

    diff_line = (
        f"fingerprint_changed={diff.get('fingerprintChanged')} "
        f"changed_refs={', '.join(diff.get('changedRefs') or [])} "
        f"added_refs={', '.join(diff.get('addedRefs') or [])} "
        f"removed_refs={', '.join(diff.get('removedRefs') or [])}"
    )

    formatted = f"""Page:
title={page.get('title') or ''}
url={page.get('url') or ''}
path={page.get('path') or ''}
ready_state={page.get('readyState') or ''}
viewport={page.get('viewport') or {}}
snapshot_id={snapshot.get('id') or ''}
fingerprint={summary.get('fingerprint') or ''}

Headings:
{chr(10).join(heading_lines)}

Forms:
{chr(10).join(form_lines)}

Interactive elements:
{chr(10).join(element_lines)}

Links:
{chr(10).join(link_lines)}

Visible text:
{_truncate_text(str(summary.get('visibleText') or ''), 900)}

Diff:
{diff_line}
"""
    return _truncate_text(formatted, LIVE_AGENT_CONTEXT_CHARS)


def _format_screenshot_context(screenshot: Optional[Dict[str, Any]]) -> str:
    if not screenshot:
        return "No screenshot attached. Use text refs only."
    refs = ", ".join(screenshot.get("refs") or [])
    return (
        "A ref-overlay screenshot was captured for recovery/debugging. "
        f"Use the structured refs as source of truth. Visible overlay refs: {refs or 'not provided'}."
    )


def _compact_live_context(text: Optional[str]) -> str:
    if not text:
        return "No live page snapshot provided."

    raw = text.strip()
    sections = {}
    current = "Summary"
    sections[current] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("--- ") and stripped.endswith(" ---"):
            current = stripped.strip("- ").strip()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    preferred = []
    for section_name, limit in (
        ("Summary", 300),
        ("Headings", 450),
        ("Interactive Elements", 1500),
        ("Links on Page", 450),
        ("Page Content", 250),
    ):
        content = "\n".join(sections.get(section_name) or []).strip()
        if content:
            preferred.append(f"--- {section_name} ---\n{_truncate_text(content, limit)}")

    compact = "\n\n".join(preferred) if preferred else raw
    return _truncate_text(compact, LIVE_AGENT_CONTEXT_CHARS)


def _chat_gemini(
    user_message: str,
    conversation_history: Optional[List[Dict]] = None,
    *,
    system_prompt: str = SYSTEM_PROMPT,
) -> Dict:
    contents = []

    if conversation_history:
        for msg in conversation_history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))

    contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    response = _get_gemini_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.3,
            response_mime_type="application/json",
        ),
    )

    return _parse_response(response.text)


def _chat_groq(
    user_message: str,
    conversation_history: Optional[List[Dict]] = None,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    model: Optional[str] = None,
) -> Dict:
    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        for msg in conversation_history:
            role = "user" if msg["role"] == "user" else "assistant"
            messages.append({"role": role, "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    completion = _get_groq_client().chat.completions.create(
        model=model or GROQ_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=700,
        response_format={"type": "json_object"},
    )
    return _parse_response(completion.choices[0].message.content)


def _parse_response(response_text: str) -> Dict:
    if not response_text:
        return {"answer": "Error: Empty response from model.", "actions": []}

    response_text = response_text.strip()
    try:
        json_match = re.search(r"(\{.*\})", response_text, re.DOTALL)
        parsed = json.loads(json_match.group(1) if json_match else response_text)
        return {
            "answer": parsed.get("answer", response_text),
            "actions": parsed.get("actions", []),
            "status": parsed.get("status", "working"),
        }
    except Exception:
        return {"answer": response_text, "actions": []}
