"""
Agent Executor — orchestrates AI-driven computer automation.

Uses persistent WebSocket connections via VMControlService and
comprehensive system prompts inspired by open-computer-use.
"""

import json
import asyncio
import logging
import base64
import os
import aiohttp
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional, Dict, Any, List, Union
from supabase import Client
from app.config import GEMINI_API_KEY
from app.services.vm_control import vm_control_service, DOCKER_HOST_IP
from app.routes.remote_relay import send_device_action, get_device_screenshot

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# AUTO-DETECT MODE (ask vs act)
# ═══════════════════════════════════════════════════════════════════════

# Keywords that indicate the user wants a text answer, not a computer action
_ASK_PATTERNS = [
    # Questions
    "what is", "what are", "what's", "who is", "who are", "how does", "how do",
    "why is", "why do", "why does", "when is", "when do", "where is", "where do",
    "can you explain", "explain", "tell me about", "describe", "define",
    "what does", "how can i", "how to", "difference between",
    # Greetings
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "sup", "yo", "greetings", "howdy", "what's up", "how are you",
    # Help / meta
    "help", "what can you do", "list your", "your capabilities",
    # Opinion / knowledge
    "do you think", "opinion on", "recommend", "suggestion", "advice",
    "best way to", "should i",
]

# Keywords that strongly indicate the user wants a computer action
_ACT_PATTERNS = [
    "open", "click", "go to", "navigate", "search for", "type", "download",
    "install", "run", "execute", "create a file", "write a file", "save",
    "close", "minimize", "maximize", "screenshot", "find on", "scroll",
    "browse to", "fill in", "log in", "sign in", "sign up", "submit",
    "copy", "paste", "delete", "rename", "move", "drag",
    "on the vm", "on the computer", "on the screen", "on the desktop",
    "on my computer", "on my desktop",
]

def _auto_detect_mode(message: str, has_target: bool) -> str:
    """Determine whether a message is a question (ask) or a task (act)."""
    msg_lower = message.strip().lower()

    # Very short messages that are just greetings
    if len(msg_lower) < 15:
        greetings = {"hi", "hello", "hey", "yo", "sup", "thanks", "thank you",
                     "ok", "okay", "cool", "nice", "great", "good", "bye",
                     "good morning", "good evening", "good afternoon",
                     "how are you", "what's up"}
        if msg_lower.rstrip('!.,? ') in greetings:
            return "ask"

    # Check for strong act signals first (these take priority)
    for pattern in _ACT_PATTERNS:
        if pattern in msg_lower:
            return "act"

    # Check for ask signals
    ask_score = 0
    for pattern in _ASK_PATTERNS:
        if msg_lower.startswith(pattern) or f" {pattern}" in msg_lower:
            ask_score += 1

    # Ends with question mark = likely a question
    if msg_lower.rstrip().endswith("?"):
        ask_score += 2

    # No target connected = probably just chatting
    if not has_target:
        ask_score += 1

    return "ask" if ask_score >= 2 else "act"

# ═══════════════════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ═══════════════════════════════════════════════════════════════════════

ACT_SYSTEM_PROMPT = """You are Control AI, an autonomous agent that controls a virtual computer or paired remote desktop to complete tasks for the user.

You can see the computer's screen via screenshots and perform actions. You must be fully autonomous — solve the task from start to finish without asking "Should I...".

## AVAILABLE ACTIONS

**Screen & Observation:**
- SCREENSHOT() — Take a screenshot to see the current screen state. ALWAYS do this first and after every action. Params: {}

**Mouse Actions (coordinates are normalized 0-1000, where 0,0=top-left, 1000,1000=bottom-right):**
- CLICK(x, y) — Left click at coordinates. Params: {"x": number, "y": number}
- DOUBLE_CLICK(x, y) — Double click. Params: {"x": number, "y": number}
- RIGHT_CLICK(x, y) — Right click for context menus. Params: {"x": number, "y": number}
- MOVE(x, y) — Move mouse cursor. Params: {"x": number, "y": number}
- DRAG(from_x, from_y, to_x, to_y) — Drag and drop between two points. Params: {"from_x": number, "from_y": number, "to_x": number, "to_y": number}
- SCROLL(direction, amount) — Scroll the page. Params: {"direction": "up"|"down", "amount": number (1-20)}

**Keyboard Actions:**
- TYPE(text) — Type text at current cursor position. Click the target field first! Params: {"text": string}
- KEY(key) — Press a single key: Enter, Tab, Escape, Backspace, Delete, Home, End, PageUp, PageDown, Up, Down, Left, Right, F1-F12. For key combos use KEY_COMBO. Params: {"key": string}
- KEY_COMBO(keys) — Press multiple keys simultaneously: ctrl+c, ctrl+v, ctrl+a, alt+f4, ctrl+shift+t, etc. Params: {"keys": string (e.g. "ctrl+c")}

**Browser Actions:**
- BROWSER_NAVIGATE(url) — Open URL in Firefox. Params: {"url": string}
- BROWSER_GET_CONTENT(url) — Scrape text content from URL (without browser). Params: {"url": string}
- BROWSER_FIND(query) — Find/search text on the current page (Ctrl+F). Params: {"query": string}

**Terminal & Files:**
- TERMINAL(command) — Run a shell command and get output. Params: {"command": string}
- FILE_READ(filepath) — Read a file. Params: {"filepath": string}
- FILE_WRITE(filepath, content) — Write content to a file. Params: {"filepath": string, "content": string}

**System:**
- LIST_APPS() — List installed applications. Params: {}
- SECRET_LOOKUP(service_name) — Find credentials in vault. Params: {"service_name": string}
- SECRET_TYPE(secret_id, field) — Auto-type stored credentials. Params: {"secret_id": string, "field": "username"|"password"}
- HITL(reason) — Pause and ask for human input when blocked. Params: {"reason": string}
- DONE(summary) — Task is complete. Params: {"summary": string}

## CRITICAL OPERATING RULES

1. **ALWAYS start with SCREENSHOT()** to see what's on screen.
2. **After EVERY action that changes the screen**, take a SCREENSHOT() to verify success.
3. **Coordinates are normalized 0-1000.** (0,0) is TOP-LEFT, (1000,1000) is BOTTOM-RIGHT.
4. **Click before typing.** Always CLICK on the target input field before using TYPE.
5. **Be precise with coordinates.** Look at the screenshot carefully to identify exact locations of buttons, fields, and UI elements.
6. **Retry on failure.** If a click doesn't work, try nearby coordinates or a different approach.
7. **Use SCROLL to find elements** that might be below the visible area.
8. **For web tasks:**
   - Use BROWSER_NAVIGATE to open URLs
   - Use BROWSER_GET_CONTENT for quick text scraping without browser
   - Navigate, observe, then interact
9. **For typing in forms:**
   - CLICK the field first
   - Then TYPE the text
   - Use KEY("Tab") to move to next field
   - Use KEY("Enter") to submit
10. **Key combos:** Use KEY_COMBO for shortcuts like "ctrl+c" (copy), "ctrl+v" (paste), "ctrl+a" (select all), "alt+tab" (switch window).

## ENVIRONMENT AWARENESS
- If 'Mode' is 'VM': You're in a Linux XFCE desktop. Use TERMINAL for shell commands.
- If 'Mode' is 'Remote Desktop': You're on the user's actual OS. Be careful with destructive actions.
- If 'Target Status' is NOT 'running'/'Online', inform the user and DONE.

## RESPONSE FORMAT (Strict JSON only)
{"thought": "reasoning about current state and next step", "action": "ACTION_NAME", "params": {"key": "value"}}

## GREETING HANDLING
For casual messages like "Hi", "Hello", etc. — just respond naturally in plain text. Do NOT use JSON actions.

## STOP POLICY
- If blocked by CAPTCHA, paywall, or hard login → use HITL
- If action fails 3 times → try alternative approach or DONE with explanation
- Never enter credentials unless the user explicitly provides them or you use SECRET_TYPE"""

ASK_SYSTEM_PROMPT = """You are Control AI, a highly knowledgeable assistant. The user is asking you a question — they do NOT want you to control a computer or perform actions.

Respond naturally and helpfully, like a knowledgeable expert. Use markdown formatting for code blocks, lists, and emphasis where appropriate.

RULES:
1. Answer the question directly and thoroughly.
2. Use clear, well-structured responses with markdown.
3. If the question is about code, include code examples.
4. Do NOT output JSON action commands — just respond in plain text/markdown.
5. Be concise but complete.
6. If the user asks something you're unsure about, say so honestly.
7. You may reference the user's computer setup context if relevant to answering their question."""

WORKFLOW_SYSTEM_PROMPT = """You are Control AI Workflow Designer. Your goal is to help the user create a visual automation workflow.

The workflow consists of NODES and EDGES.
Available Node Types:
- start_time: Trigger based on time. params: {"value": "HH:mm", "days": ["Mon", "Tue"...]}
- start_keyword: Trigger based on a voice keyword. params: {"value": "keyword"}
- app: Open an application. params: {"value": "app name"}
- file: Open a file. params: {"value": "file path"}
- web_search: Perform a web search. params: {"value": "query"}
- browser_search: Agentic browser search. params: {"value": "instruction"}
- nl_task: Natural language task for AI. params: {"value": "task description"}

RESPONSE RULES:
1. Ask clarifying questions to understand the user's automation needs.
2. Once you have enough information, generate the workflow.
3. Your final output must include a JSON block representing the workflow.
4. The JSON must have: "name", "trigger", "nodes", "edges".
5. Nodes must have: "id", "type", "position": {"x", "y"}, "data": {"value", "description"}.
6. Edges must have: "id", "source", "target".

Example JSON:
```json
{
  "name": "Morning Routine",
  "trigger": {"type": "time", "value": "08:00"},
  "nodes": [
    {"id": "n1", "type": "start_time", "position": {"x": 100, "y": 100}, "data": {"value": "08:00"}},
    {"id": "n2", "type": "app", "position": {"x": 400, "y": 100}, "data": {"value": "Slack"}}
  ],
  "edges": [
    {"id": "e1", "source": "n1", "target": "n2"}
  ]
}
```"""

# Alias for backward compat
SYSTEM_PROMPT = ACT_SYSTEM_PROMPT

# ═══════════════════════════════════════════════════════════════════════
# AI PROVIDER CALLS
# ═══════════════════════════════════════════════════════════════════════

async def _call_gemini(model: str, api_key: str, messages: list, image_b64: Optional[str] = None) -> str:
    """Call Google Gemini API."""
    import google.generativeai as genai
    genai.configure(api_key=api_key)

    gemini_model = genai.GenerativeModel(model)

    parts = []
    for msg in messages:
        if msg["role"] == "user":
            parts.append(msg["content"])

    if image_b64:
        # Strip data URL prefix if present
        raw_b64 = image_b64
        if "base64," in raw_b64:
            raw_b64 = raw_b64.split("base64,")[1]
        parts.insert(0, {"mime_type": "image/jpeg", "data": raw_b64})

    try:
        response = await asyncio.to_thread(
            gemini_model.generate_content, parts
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return f"Gemini connection error: {str(e)}"

async def _call_openai_compat(
    model: str, api_key: str, messages: list, image_b64: Optional[str] = None,
    base_url: str = "https://api.openai.com/v1", **kwargs
) -> str:

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    system_prompt = kwargs.get("system_prompt", SYSTEM_PROMPT)
    formatted_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    for msg in messages:
        if msg["role"] == "user" and image_b64 and msg == messages[-1]:
            raw_b64 = image_b64
            if not raw_b64.startswith("data:image"):
                raw_b64 = f"data:image/jpeg;base64,{raw_b64}"
            content: List[Dict[str, Any]] = [
                {"type": "image_url", "image_url": {"url": raw_b64}},
                {"type": "text", "text": msg["content"]}
            ]
            formatted_messages.append({"role": "user", "content": content})
        else:
            formatted_messages.append({"role": msg["role"], "content": msg["content"]})

    payload = {
        "model": model,
        "messages": formatted_messages,
        "max_tokens": 2048,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return f"API error {resp.status}: {text[:200]}"
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()

async def _call_anthropic(
    model: str, api_key: str, messages: list, image_b64: Optional[str] = None
) -> str:

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    formatted_messages = []
    for msg in messages:
        if msg["role"] == "user" and image_b64 and msg == messages[-1]:
            raw_b64 = image_b64
            if "base64," in raw_b64:
                raw_b64 = raw_b64.split("base64,")[1]
            formatted_messages.append({
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": raw_b64}},
                    {"type": "text", "text": msg["content"]}
                ]
            })
        else:
            formatted_messages.append({"role": msg["role"], "content": msg["content"]})

    payload = {
        "model": model,
        "max_tokens": 2048,
        "system": SYSTEM_PROMPT,
        "messages": formatted_messages,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return f"Anthropic error {resp.status}: {text[:200]}"
            data = await resp.json()
            return data["content"][0]["text"].strip()

async def _call_ollama(model: str, messages: list, image_b64: Optional[str] = None) -> str:

    formatted_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in messages:
        if msg["role"] == "user" and image_b64 and msg == messages[-1]:
            raw_b64 = image_b64
            if "base64," in raw_b64:
                raw_b64 = raw_b64.split("base64,")[1]
            formatted_messages.append({
                "role": "user",
                "content": msg["content"],
                "images": [raw_b64]
            })
        else:
            formatted_messages.append({"role": msg["role"], "content": msg["content"]})

    payload = {"model": model, "messages": formatted_messages, "stream": False}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://localhost:11434/api/chat",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
            if resp.status != 200:
                 return f"Ollama error: {resp.status}"
            data = await resp.json()
            return data["message"]["content"].strip()

# ═══════════════════════════════════════════════════════════════════════
# VM & DEVICE ACTIONS (via persistent connection/relay)
# ═══════════════════════════════════════════════════════════════════════

async def _take_screenshot_vm(machine_id: str) -> Optional[str]:
    """Take a screenshot using persistent connection."""
    return await vm_control_service.take_screenshot(machine_id)

async def _execute_vm_action(machine_id: str, action: str, params: Dict[str, Any]) -> Optional[Dict]:
    """Execute an action on the VM using persistent connection."""
    # Map high-level action names to VM agent commands
    CMD_MAP = {
        "click": "click",
        "double_click": "double_click",
        "right_click": "right_click",
        "move": "move",
        "mouse_move": "move",
        "type": "type",
        "key": "key_press",
        "key_press": "key_press",
        "key_combo": "key_combo",
        "scroll": "scroll",
        "drag": "drag",
        "browser_navigate": "browser_navigate",
        "browser_get_content": "browser_get_content",
        "browser_find": "browser_find",
        "terminal": "terminal",
        "terminal_execute": "terminal",
        "file_read": "file_read",
        "file_write": "file_write",
        "file_exists": "file_exists",
        "directory_list": "directory_list",
        "list_apps": "list_apps",
        "screenshot": "screenshot",
    }

    cmd = CMD_MAP.get(action.lower(), action.lower())
    result = await vm_control_service.execute_command(machine_id, cmd, params)
    return result

async def _execute_device_action(device_id: str, action: str, params: Optional[dict] = None) -> dict:
    """Execute an action on a paired device using the high-speed relay."""
    success = await send_device_action(device_id, action.lower(), params)
    return {
        "status": "success" if success else "error",
        "message": "Action sent" if success else "Device not connected to relay"
    }

async def _take_screenshot_device(device_id: str) -> Optional[str]:
    """Take a screenshot using the relay's cached frame."""
    frame_bytes = get_device_screenshot(device_id)
    if not frame_bytes:
        return None
    b64 = base64.b64encode(frame_bytes).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

async def _web_scrape(url: str) -> str:
    try:
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                return text[:5000]
    except Exception as e:
        return f"Error fetching {url}: {e}"

# ═══════════════════════════════════════════════════════════════════════
# AGENT EXECUTOR
# ═══════════════════════════════════════════════════════════════════════

class AgentExecutor:
    def __init__(self):
        pass

    async def _get_provider_config(self, db: Client, user_id: str) -> Dict:
        try:
            res = db.table("app_config").select("value").eq("key", f"api_keys_{user_id}").execute()
            if res.data:
                return res.data[0].get("value", {})

            res = db.table("app_config").select("value").eq("key", "api_keys").execute()
            if res.data:
                return res.data[0].get("value", {})
        except Exception as e:
            logger.error(f"Error fetching provider config: {e}")

        return {"provider": "gemini", "gemini_model": "gemini-2.5-flash"}

    async def _call_ai(
        self, config: Dict, messages: list, image_b64: Optional[str] = None
    ) -> str:

        provider = config.get("provider", "gemini")

        if provider == "gemini":
            key = config.get("gemini_api_key") or GEMINI_API_KEY
            model = config.get("gemini_model", "gemini-2.5-flash")
            if not key:
                if GEMINI_API_KEY:
                    return await _call_gemini(model, GEMINI_API_KEY, messages, image_b64)
                raise ValueError("No Gemini API key configured. Go to Settings > AI to add your key.")
            return await _call_gemini(model, key, messages, image_b64)

        elif provider == "openai":
            key = config.get("openai_api_key", "")
            model = config.get("openai_model", "gpt-4o")
            if not key:
                if GEMINI_API_KEY:
                    logger.info("OpenAI key missing, falling back to default Gemini")
                    return await _call_gemini("gemini-2.5-flash", GEMINI_API_KEY, messages, image_b64)
                raise ValueError("No OpenAI API key configured. Go to Settings > AI to add your key.")
            return await _call_openai_compat(model, key, messages, image_b64)

        elif provider == "anthropic":
            key = config.get("anthropic_api_key", "")
            model = config.get("anthropic_model", "claude-3-5-sonnet-20241022")
            if not key:
                if GEMINI_API_KEY:
                    logger.info("Anthropic key missing, falling back to default Gemini")
                    return await _call_gemini("gemini-2.5-flash", GEMINI_API_KEY, messages, image_b64)
                raise ValueError("No Anthropic API key configured. Go to Settings > AI to add your key.")
            return await _call_anthropic(model, key, messages, image_b64)

        elif provider == "openrouter":
            key = config.get("openrouter_api_key", "")
            model = config.get("openrouter_model", "anthropic/claude-3.5-sonnet")
            if not key:
                if GEMINI_API_KEY:
                    logger.info("OpenRouter key missing, falling back to default Gemini")
                    return await _call_gemini("gemini-2.5-flash", GEMINI_API_KEY, messages, image_b64)
                raise ValueError("No OpenRouter API key configured. Go to Settings > AI to add your key.")
            return await _call_openai_compat(
                model, key, messages, image_b64,
                base_url="https://openrouter.ai/api/v1"
            )

        elif provider == "xai":
            key = config.get("xai_api_key", "")
            model = config.get("xai_model", "grok-2-vision-1212")
            if not key:
                if GEMINI_API_KEY:
                    logger.info("xAI key missing, falling back to default Gemini")
                    return await _call_gemini("gemini-2.5-flash", GEMINI_API_KEY, messages, image_b64)
                raise ValueError("No xAI API key configured. Go to Settings > AI to add your key.")
            return await _call_openai_compat(
                model, key, messages, image_b64,
                base_url="https://api.x.ai/v1"
            )

        elif provider == "ollama":
            model = config.get("ollama_model", "llava")
            return await _call_ollama(model, messages, image_b64)

        else:
            key = GEMINI_API_KEY
            if not key:
                raise ValueError("No AI provider configured. Go to Settings > AI to set up a provider.")
            return await _call_gemini("gemini-2.5-flash", key, messages, image_b64)

    async def _update_usage(self, db: Client, user_id: str, mode: str, tokens: int = 0):
        """Update user usage statistics in the database."""
        try:
            res = db.table("users").select("act_count, ask_count, total_token_usage, daily_token_usage").eq("id", user_id).execute()
            if not res.data:
                return

            user = res.data[0]
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            update_data = {}
            if tokens == 0:
                if mode == "act":
                    update_data["act_count"] = (user.get("act_count") or 0) + 1
                else:
                    update_data["ask_count"] = (user.get("ask_count") or 0) + 1

            if tokens > 0:
                update_data["total_token_usage"] = (user.get("total_token_usage") or 0) + tokens
                token_usage = user.get("token_usage") or {}
                if mode not in token_usage: token_usage[mode] = {"prompt": 0, "candidates": 0, "total": 0}
                token_usage[mode]["prompt"] += tokens // 2
                token_usage[mode]["candidates"] += tokens // 2
                token_usage[mode]["total"] += tokens
                update_data["token_usage"] = token_usage

            daily_stats = user.get("daily_token_usage") or {}
            if today not in daily_stats:
                daily_stats[today] = {"ask": 0, "act": 0, "total": 0}

            if tokens == 0:
                if mode == "act":
                    daily_stats[today]["act"] = (daily_stats[today].get("act") or 0) + 1
                else:
                    daily_stats[today]["ask"] = (daily_stats[today].get("ask") or 0) + 1

            if tokens > 0:
                daily_stats[today]["total"] = (daily_stats[today].get("total") or 0) + tokens
                if "prompt" not in daily_stats[today]: daily_stats[today]["prompt"] = 0
                if "candidates" not in daily_stats[today]: daily_stats[today]["candidates"] = 0
                daily_stats[today]["prompt"] += tokens // 2
                daily_stats[today]["candidates"] += tokens // 2

            update_data["daily_token_usage"] = daily_stats
            db.table("users").update(update_data).eq("id", user_id).execute()

        except Exception as e:
            logger.error(f"Failed to update user usage: {e}")

    async def execute_task(
        self, db: Client, session_id: str, user_message: str, session_data: dict, mode: str = "act"
    ) -> AsyncGenerator[dict, None]:

        vm_data = session_data.get("virtual_machines") or {}
        device_id = session_data.get("device_id")
        user_id = session_data.get("user_id", "")

        # Auto-detect mode if 'act'
        has_target = bool(vm_data or device_id)
        if mode == "act":
            detected = _auto_detect_mode(user_message, has_target)
            if detected == "ask":
                mode = "ask"

        await self._update_usage(db, user_id, mode, tokens=0)
        tokens_per_turn = len(user_message) // 4 + 200
        provider_config = await self._get_provider_config(db, user_id)

        try:
            session = db.table("chat_sessions").select("title").eq("id", session_id).execute()
            if session.data and session.data[0].get("title") == "New Chat":
                title = user_message[:50] + ("..." if len(user_message) > 50 else "")
                db.table("chat_sessions").update({"title": title}).eq("id", session_id).execute()
        except Exception:
            pass

        try:
            target_name = vm_data.get("name") or (f"Remote Desktop ({device_id[:8]})" if device_id else "No Target")
            agent_port = vm_data.get("agent_port")

            machine_id: Optional[str] = None
            if agent_port:
                machine_id = f"vm_{agent_port}"
                await vm_control_service.connect(int(agent_port), machine_id)

            conversation: list = []
            uploaded_image = None
            if "data:image/" in user_message and ";base64," in user_message:
                try:
                    parts = user_message.split("data:image/")
                    if len(parts) > 1:
                        img_part = parts[1].split("]")[0]
                        uploaded_image = img_part.split(",")[1]
                        user_message = user_message.split("[Attached file:")[0].strip()
                except Exception: pass

            target_status = "Unknown"
            if vm_data:
                target_status = vm_data.get("status", "Unknown")
            elif device_id:
                try:
                    dev_res = db.table("paired_devices").select("status").eq("id", device_id).execute()
                    if dev_res.data:
                        s = dev_res.data[0].get("status", "Unknown")
                        target_status = "Online" if s == "paired" else "Offline" if s == "revoked" else s
                except Exception: pass

            initial_msg = (
                f"Target: {target_name}\n"
                f"Target Status: {target_status}\n"
                f"Mode: {'VM' if vm_data else 'Remote Desktop' if device_id else 'Chat Only'}\n"
                f"Task: {user_message}"
            )
            conversation.append({"role": "user", "content": initial_msg})

            max_steps = 30
            step = 0
            last_screenshot: Optional[str] = uploaded_image

            while step < max_steps:
                step += 1
                try:
                    session_res = db.table("chat_sessions").select("ai_status").eq("id", session_id).execute()
                    current_status = session_res.data[0].get("ai_status", "running") if session_res.data else "running"
                except Exception: current_status = "running"

                if current_status == "stopped":
                    yield {"type": "message", "content": "🛑 AI stopped by user."}
                    break
                if current_status == "paused":
                    yield {"type": "thinking", "content": "⏸️ Paused..."}
                    await asyncio.sleep(2)
                    step -= 1
                    continue

                yield {"type": "thinking", "content": f"Step {step} — thinking..."}

                if mode == "ask" or mode == "workflow":
                    provider_config["system_prompt"] = WORKFLOW_SYSTEM_PROMPT if mode == "workflow" else ASK_SYSTEM_PROMPT
                    response_text = await self._call_ai(provider_config, conversation, last_screenshot)
                    await self._update_usage(db, user_id, mode, tokens=tokens_per_turn)
                    yield {"type": "message", "content": response_text}
                    yield {"type": "done"}
                    db.table("chat_messages").insert({"session_id": session_id, "role": "assistant", "content": response_text}).execute()
                    return

                # ACT mode
                provider_config["system_prompt"] = ACT_SYSTEM_PROMPT
                response_text = await self._call_ai(provider_config, conversation, last_screenshot)

                try:
                    clean_text = response_text
                    if "```json" in clean_text: clean_text = clean_text.split("```json")[1].split("```")[0].strip()
                    elif "```" in clean_text: clean_text = clean_text.split("```")[1].split("```")[0].strip()
                    action_data = json.loads(clean_text)
                    thought = action_data.get("thought", "")
                    action = action_data.get("action", "").upper()
                    params = action_data.get("params", {})
                except (json.JSONDecodeError, ValueError):
                    db.table("chat_messages").insert({"session_id": session_id, "role": "assistant", "content": response_text}).execute()
                    yield {"type": "message", "content": response_text}
                    break

                if thought: yield {"type": "thought", "content": thought}
                yield {"type": "action", "action": action, "params": params}
                await self._update_usage(db, user_id, "act", tokens=tokens_per_turn)

                if not session_id.startswith("wf_gen_"):
                    db.table("chat_messages").insert({
                        "session_id": session_id, "role": "assistant", "content": thought,
                        "action_type": action.lower(), "action_data": params
                    }).execute()

                conversation.append({"role": "assistant", "content": response_text})
                action_result = ""
                last_screenshot = None

                if action == "DONE":
                    yield {"type": "message", "content": f"✅ {params.get('summary', 'Task completed.')}"}
                    break
                elif action == "HITL":
                    yield {"type": "hitl", "content": params.get("reason", "Action needed.")}
                    break
                elif action == "SECRET_LOOKUP":
                    res = db.table("secrets").select("id, name, service, username").eq("user_id", user_id).ilike("service", f"%{params.get('service_name', '')}%").execute()
                    action_result = f"Secrets found:\n{json.dumps(res.data, indent=2)}"
                elif action == "SECRET_TYPE":
                    sid, field = params.get("secret_id"), params.get("field", "password")
                    res = db.table("secrets").select("*").eq("id", sid).eq("user_id", user_id).execute()
                    if res.data:
                        val = res.data[0].get("password") if field == "password" else res.data[0].get("username")
                        if val:
                            if machine_id: await _execute_vm_action(machine_id, "type", {"text": val})
                            elif device_id: await _execute_device_action(device_id, "key_press", {"key": val})
                            action_result = f"Typed {field} from secret."
                        else: action_result = "Secret field missing."
                    else: action_result = "Secret not found."
                elif action == "SCREENSHOT":
                    shot = await _take_screenshot_vm(machine_id) if machine_id else await _take_screenshot_device(device_id) if device_id else None
                    if shot:
                        last_screenshot = shot
                        action_result = "Screenshot taken."
                    else: action_result = "Screenshot failed."
                elif action == "TERMINAL":
                    cmd = params.get("command", "")
                    if device_id:
                        await _execute_device_action(device_id, "terminal", {"command": cmd})
                        action_result = f"Command sent to desktop."
                    elif machine_id:
                        result = await _execute_vm_action(machine_id, "terminal", {"command": cmd})
                        action_result = json.dumps(result)
                    else: action_result = "No target."
                elif action == "CLICK":
                    p = {"x": params.get("x"), "y": params.get("y"), "button": params.get("button", "left")}
                    if machine_id: await _execute_vm_action(machine_id, "click", p)
                    elif device_id: await _execute_device_action(device_id, "click", p)
                    action_result = "Click sent."
                elif action == "TYPE":
                    txt = params.get("text", "")
                    if machine_id: await _execute_vm_action(machine_id, "type", {"text": txt})
                    elif device_id: await _execute_device_action(device_id, "key_press", {"key": txt})
                    action_result = "Text typed."
                elif action == "KEY_COMBO":
                    keys = params.get("keys", "")
                    if machine_id: await _execute_vm_action(machine_id, "key_combo", {"keys": keys.split("+")})
                    elif device_id: await _execute_device_action(device_id, "key_combo", {"keys": keys})
                    action_result = "Keys pressed."
                else:
                    a_low = action.lower()
                    if machine_id: result = await _execute_vm_action(machine_id, a_low, params)
                    elif device_id: result = await _execute_device_action(device_id, a_low, params)
                    else: result = {"error": "No target"}
                    action_result = json.dumps(result)

                if action_result:
                    conversation.append({"role": "user", "content": f"Action result: {action_result}"})
                await asyncio.sleep(0.3)

            yield {"type": "done"}
        except Exception as e:
            logger.exception("Agent error")
            yield {"type": "error", "content": str(e)}
            yield {"type": "done"}

agent_executor = AgentExecutor()
