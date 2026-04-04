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
from app.services.vm_service import vm_service
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
    "copy", "paste", "delete", "rename", "move", "drag", "start", "launch",
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

ACT_SYSTEM_PROMPT = """You are Control AI, a robotic autonomous computer control agent.
Your ONLY purpose is to perform actions on a computer screen to complete the user's task.

## STRICT OUTPUT RULES:
1. NEVER speak to the user. NO conversational text. NO explanations. NO greetings.
2. YOU MUST ONLY RESPOND WITH A SINGLE JSON OBJECT.
3. ALWAYS start with SCREENSHOT() if you do not have the current screen state.
4. If the task is finished, use DONE(summary="Final summary of what was accomplished").
5. If you are stuck after 3 attempts, use HITL(reason="Explanation of why you are stuck").

## AVAILABLE ACTIONS (Strict JSON format)

{"thought": "reasoning", "action": "SCREENSHOT", "params": {}}
{"thought": "reasoning", "action": "CLICK", "params": {"x": 500, "y": 500, "button": "left"}}
{"thought": "reasoning", "action": "DOUBLE_CLICK", "params": {"x": 500, "y": 500}}
{"thought": "reasoning", "action": "RIGHT_CLICK", "params": {"x": 500, "y": 500}}
{"thought": "reasoning", "action": "TYPE", "params": {"text": "hello"}}
{"thought": "reasoning", "action": "KEY", "params": {"key": "Enter"}}
{"thought": "reasoning", "action": "KEY_COMBO", "params": {"keys": "ctrl+c"}}
{"thought": "reasoning", "action": "SCROLL", "params": {"direction": "down", "amount": 10}}
{"thought": "reasoning", "action": "TERMINAL", "params": {"command": "ls -la"}}
{"thought": "reasoning", "action": "BROWSER_OPEN", "params": {}}
{"thought": "reasoning", "action": "BROWSER_NAVIGATE", "params": {"url": "https://google.com"}}
{"thought": "reasoning", "action": "DONE", "params": {"summary": "Task description result"}}

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

## PROFESSIONAL & CREATIVE SOFTWARE (CAD, Blender, DCC, IDEs)
- Treat toolbars, modifiers, and node editors as precision workflows: confirm the active mode (Object/Edit, etc.) via SCREENSHOT before destructive edits.
- Prefer keyboard shortcuts documented for the app when faster than mouse (Blender: G/R/S, Tab, Space; CAD: ortho/snaps where applicable).
- For complex UIs, work in small loops: SCREENSHOT → one focused action → SCREENSHOT to verify.
- If viewport navigation is unclear, use middle-mouse / view menus via TERMINAL only when CLI exists; otherwise click conservatively with HITL if credentials or licensing block progress.

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
7. You may reference the user's computer setup context if relevant to answering their question.
8. For CAD, 3D (Blender, Maya), and creative suites: explain concepts, shortcuts, and safe workflows; do not pretend you are executing clicks unless the user switched to Act mode."""

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

async def _call_gemini(model: str, api_key: str, messages: list, image_b64: Optional[str] = None, system_prompt: str = ACT_SYSTEM_PROMPT, stream: bool = False):
    """Call Google Gemini API with system_instruction and optional streaming."""
    import google.generativeai as genai
    genai.configure(api_key=api_key)

    # Use the system_instruction parameter in GenerativeModel
    gemini_model = genai.GenerativeModel(model, system_instruction=system_prompt)

    parts = []
    # If we have an image, it MUST go before text in the current turn for some Gemini versions
    if image_b64:
        raw_b64 = image_b64
        if "base64," in raw_b64: raw_b64 = raw_b64.split("base64,")[1]
        parts.append({"mime_type": "image/jpeg", "data": raw_b64})

    # Add the last few user messages (limit context for efficiency)
    for msg in messages[-5:]:
        if msg["role"] == "user":
            parts.append(msg["content"])

    try:
        if stream:
            async def gen():
                response = await asyncio.to_thread(gemini_model.generate_content, parts, stream=True)
                for chunk in response:
                    if chunk.text: yield chunk.text
            return gen()
        else:
            response = await asyncio.to_thread(gemini_model.generate_content, parts)
            return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return f"Gemini connection error: {str(e)}"

async def _call_openai_compat(
    model: str, api_key: str, messages: list, image_b64: Optional[str] = None,
    base_url: str = "https://api.openai.com/v1", system_prompt: str = ACT_SYSTEM_PROMPT, stream: bool = False
) -> Union[str, AsyncGenerator[str, None]]:

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    formatted_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    for msg in messages[-10:]:
        if msg["role"] == "user" and image_b64 and msg == messages[-1]:
            raw_b64 = image_b64
            if not raw_b64.startswith("data:image"): raw_b64 = f"data:image/jpeg;base64,{raw_b64}"
            formatted_messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": raw_b64}},
                    {"type": "text", "text": msg["content"]}
                ]
            })
        else:
            formatted_messages.append({"role": msg["role"], "content": msg["content"]})

    payload = {"model": model, "messages": formatted_messages, "max_tokens": 2048, "stream": stream}

    if stream:
        async def gen():
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        yield f"API error {resp.status}"
                        return
                    async for line in resp.content:
                        l = line.decode("utf-8").strip()
                        if l.startswith("data: ") and l != "data: [DONE]":
                            try:
                                chunk = json.loads(l[6:])
                                content = chunk["choices"][0]["delta"].get("content", "")
                                if content: yield content
                            except: pass
        return gen()
    else:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
                if resp.status != 200: return f"API error {resp.status}"
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
        "browser_open": "browser_open",
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
        self, config: Dict, messages: list, image_b64: Optional[str] = None, stream: bool = False
    ) -> Union[str, AsyncGenerator[str, None]]:

        provider = config.get("provider", "gemini")
        system_prompt = config.get("system_prompt", ACT_SYSTEM_PROMPT)

        if provider == "gemini":
            key = config.get("gemini_api_key") or GEMINI_API_KEY
            model = config.get("gemini_model", "gemini-2.5-flash")
            return await _call_gemini(model, key, messages, image_b64, system_prompt=system_prompt, stream=stream)

        elif provider == "openai":
            key = config.get("openai_api_key", "")
            model = config.get("openai_model", "gpt-4o")
            return await _call_openai_compat(model, key, messages, image_b64, system_prompt=system_prompt, stream=stream)

        elif provider == "anthropic":
            key = config.get("anthropic_api_key", "")
            model = config.get("anthropic_model", "claude-3-5-sonnet-20241022")
            return await _call_openai_compat( # Anthropic doesn't have a stream-helper yet, use generic
                model, key, messages, image_b64, 
                base_url="https://api.anthropic.com/v1", 
                system_prompt=system_prompt, stream=stream
            )

        elif provider == "openrouter":
            key = config.get("openrouter_api_key", "")
            model = config.get("openrouter_model", "anthropic/claude-3.5-sonnet")
            return await _call_openai_compat(
                model, key, messages, image_b64,
                base_url="https://openrouter.ai/api/v1",
                system_prompt=system_prompt, stream=stream
            )

        elif provider == "xai":
            key = config.get("xai_api_key", "")
            model = config.get("xai_model", "grok-2-vision-1212")
            return await _call_openai_compat(
                model, key, messages, image_b64,
                base_url="https://api.x.ai/v1",
                system_prompt=system_prompt, stream=stream
            )

        elif provider == "ollama":
            model = config.get("ollama_model", "llava")
            return await _call_ollama(model, messages, image_b64) # Add stream support later if needed

        else:
            key = GEMINI_API_KEY
            return await _call_gemini("gemini-2.5-flash", key, messages, image_b64, system_prompt=system_prompt, stream=stream)

    async def _update_usage(self, db: Client, user_id: str, mode: str, tokens: int = 0):
        """Update user usage statistics in the database."""
        try:
            res = db.table("users").select("act_count, ask_count, total_token_usage, daily_token_usage, token_usage").eq("id", user_id).execute()
            if not res.data:
                return

            user = res.data[0]
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            update_data: Dict[str, Any] = {}

            # Always increment the interaction count for the mode
            if mode == "act":
                update_data["act_count"] = (user.get("act_count") or 0) + 1
            else:
                update_data["ask_count"] = (user.get("ask_count") or 0) + 1

            # Update lifetime token usage if provided
            if tokens > 0:
                update_data["total_token_usage"] = (user.get("total_token_usage") or 0) + tokens
                
                # Update detailed token breakdown
                token_usage = user.get("token_usage")
                if not isinstance(token_usage, dict):
                    token_usage = {}
                
                if mode not in token_usage:
                    token_usage[mode] = {"prompt": 0, "candidates": 0, "total": 0}
                
                m_usage = token_usage[mode]
                m_usage["prompt"] = m_usage.get("prompt", 0) + (tokens // 2)
                m_usage["candidates"] = m_usage.get("candidates", 0) + (tokens // 2)
                m_usage["total"] = m_usage.get("total", 0) + tokens
                update_data["token_usage"] = token_usage

            # Update daily statistics
            daily_stats = user.get("daily_token_usage")
            if not isinstance(daily_stats, dict):
                daily_stats = {}
                
            if today not in daily_stats:
                daily_stats[today] = {"ask": 0, "act": 0, "total": 0, "prompt": 0, "candidates": 0}

            # Increment daily interaction count
            if mode == "act":
                daily_stats[today]["act"] = (daily_stats[today].get("act") or 0) + 1
            else:
                daily_stats[today]["ask"] = (daily_stats[today].get("ask") or 0) + 1

            # Increment daily token totals
            if tokens > 0:
                daily_stats[today]["total"] = (daily_stats[today].get("total") or 0) + tokens
                daily_stats[today]["prompt"] = daily_stats[today].get("prompt", 0) + (tokens // 2)
                daily_stats[today]["candidates"] = daily_stats[today].get("candidates", 0) + (tokens // 2)

            update_data["daily_token_usage"] = daily_stats
            db.table("users").update(update_data).eq("id", user_id).execute()

        except Exception as e:
            logger.error(f"Failed to update user usage: {e}")


    async def execute_task(
        self,
        db: Client,
        session_id: str,
        user_id: str,
        user_message: str,
        machine_id: Optional[str] = None,
        device_id: Optional[str] = None,
        uploaded_image: Optional[str] = None,
        forced_mode: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Orchestrate AI interaction loop with vision and automation."""
        try:
            # 1. Setup session data
            session_result = db.table("chat_sessions").select("*").eq("id", session_id).execute()
            if not session_result.data:
                yield {"type": "error", "content": "Session not found."}
                return
            session_data = session_result.data[0]
            
            target_name = "Cloud VM"
            target_status = "Online"
            vm_id = session_data.get("vm_id")
            device_sid = session_data.get("device_id")
            # Always bind targets from the chat session (request body does not carry vm_id/device_id).
            machine_id = machine_id or vm_id
            device_id = device_id or device_sid

            vm_data = None
            if vm_id:
                vm_res = db.table("virtual_machines").select("*").eq("id", vm_id).execute()
                if vm_res.data:
                    vm_data = vm_res.data[0]
                    target_name = vm_data.get("name", target_name)
                    target_status = vm_data.get("status", "running")

            # 2. Determine mode (ASK, ACT, or WORKFLOW)
            mode = forced_mode or _auto_detect_mode(user_message, bool(machine_id or device_id))
            
            # Fetch existing history
            history_res = db.table("chat_messages").select("*").eq("session_id", session_id).order("created_at").execute()
            conversation = []
            for m in history_res.data:
                conversation.append({"role": m["role"], "content": m["content"]})
            
            # Provider config
            provider_config = await self._get_provider_config(db, user_id)
            
            max_steps = 30
            step = 0
            tokens_per_turn = len(user_message) // 4 + 200
            last_screenshot: Optional[str] = uploaded_image

            # ═══════════════════════════════════════════════════════════════════
            # AUTO-SCREENSHOT ON START
            # If we're in ACT mode and have no visual context, grab it now
            # ═══════════════════════════════════════════════════════════════════
            if mode == "act" and not last_screenshot:
                try:
                    if machine_id:
                        yield {"type": "thought", "content": "Initializing vision (VM)…"}
                        shot = await _take_screenshot_vm(machine_id)
                        if shot: last_screenshot = shot
                    elif device_id:
                        yield {"type": "thought", "content": "Initializing vision (device)…"}
                        shot = await _take_screenshot_device(device_id)
                        if shot: last_screenshot = shot
                    
                    if last_screenshot:
                        logger.info(f"Auto-captured initial screenshot for session {session_id}")
                except Exception as e:
                    logger.warning(f"Failed to auto-capture screenshot: {e}")

            if mode in ["ask", "workflow"]:
                provider_config["system_prompt"] = WORKFLOW_SYSTEM_PROMPT if mode == "workflow" else ASK_SYSTEM_PROMPT
                
                yield {"type": "thought", "content": "Assistant is typing…"}
                full_response = ""
                
                # Streaming implementation for ASK mode
                result = await self._call_ai(provider_config, conversation, last_screenshot, stream=True)
                if isinstance(result, AsyncGenerator):
                    async for chunk in result:
                        full_response += chunk
                        yield {"type": "stream", "content": chunk}
                else:
                    full_response = str(result)
                    yield {"type": "message", "content": full_response}

                # Save to DB
                db.table("chat_messages").insert({
                    "session_id": session_id,
                    "role": "assistant",
                    "content": full_response
                }).execute()
                await self._update_usage(db, user_id, mode, tokens=tokens_per_turn)
                yield {"type": "done"}
                return

            # ─── ACT MODE ──────────────────────────────────────────────────
            provider_config["system_prompt"] = ACT_SYSTEM_PROMPT

            if not machine_id and not device_id:
                yield {
                    "type": "message",
                    "content": "No VM or paired device is assigned to this chat. Open the session header, pick a running VM or an online device, then send your task again.",
                }
                yield {"type": "done"}
                return

            if machine_id:
                await vm_service.ensure_vm_agent_connected(db, machine_id)

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
                    yield {"type": "thought", "content": "⏸️ Paused…"}
                    await asyncio.sleep(2)
                    step -= 1
                    continue

                yield {"type": "thought", "content": f"Step {step} — planning next action…"}

                result = await self._call_ai(provider_config, conversation, last_screenshot, stream=False)
                response_text = str(result)

                try:
                    clean_text = response_text
                    # Robust JSON extraction
                    if "```json" in clean_text:
                        clean_text = clean_text.split("```json")[1].split("```")[0].strip()
                    elif "```" in clean_text:
                        clean_text = clean_text.split("```")[1].split("```")[0].strip()
                    
                    if not clean_text.startswith("{") and "{" in clean_text:
                        clean_text = "{" + clean_text.split("{", 1)[1]
                    if not clean_text.endswith("}") and "}" in clean_text:
                        clean_text = clean_text.rsplit("}", 1)[0] + "}"
                        
                    action_data = json.loads(clean_text)
                    thought = action_data.get("thought", "")
                    action = action_data.get("action", "").upper()
                    params = action_data.get("params", {})
                except (json.JSONDecodeError, ValueError, IndexError):
                    if step == 1 and mode == "act":
                        logger.warning(f"AI failed JSON format on step 1. Response: {response_text[:200]}...")
                        conversation.append({"role": "assistant", "content": response_text})
                        conversation.append({"role": "user", "content": "ERROR: You must respond ONLY with JSON. No conversational text. Perform the next action."})
                        step -= 1 
                        continue

                    db.table("chat_messages").insert({"session_id": session_id, "role": "assistant", "content": response_text}).execute()
                    yield {"type": "message", "content": response_text}
                    break

                if thought: yield {"type": "thought", "content": thought}
                yield {"type": "action", "action": action, "params": params}
                await self._update_usage(db, user_id, "act", tokens=tokens_per_turn)

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
                elif action == "SCREENSHOT":
                    shot = None
                    if machine_id: shot = await _take_screenshot_vm(machine_id)
                    elif device_id: shot = await _take_screenshot_device(device_id)
                    if shot:
                        last_screenshot = shot
                        action_result = "Screenshot taken successfully."
                    else: action_result = (
                        "Screenshot failed: the automation agent inside the VM did not respond. "
                        "The viewer uses VNC in the browser; actions use a separate agent connection from this server. "
                        "Wait for the VM to finish booting, confirm it is running on the Machines page, then try again."
                    )
                elif action == "TERMINAL":
                    cmd = params.get("command", "")
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "terminal", {"command": cmd})
                        action_result = f"Command output: {json.dumps(result)}"
                    elif device_id:
                        success = await _execute_device_action(device_id, "terminal", {"command": cmd})
                        action_result = f"Terminal command sent to device. Result: {success['status']}"
                    else: action_result = "Error: No target (VM or Device) specified."
                elif action == "CLICK":
                    p = {"x": params.get("x"), "y": params.get("y"), "button": params.get("button", "left")}
                    if machine_id: 
                        result = await _execute_vm_action(machine_id, "click", p)
                        action_result = f"Click executed. Result: {json.dumps(result)}"
                    elif device_id: 
                        result = await _execute_device_action(device_id, "click", p)
                        action_result = f"Click sent to device. Status: {result['status']}"
                    else: action_result = "Error: No target."
                elif action == "TYPE":
                    txt = params.get("text", "")
                    if machine_id: 
                        result = await _execute_vm_action(machine_id, "type", {"text": txt})
                        action_result = f"Type action result: {json.dumps(result)}"
                    elif device_id: 
                        result = await _execute_device_action(device_id, "type", {"text": txt})
                        action_result = f"Text sent to device. Status: {result['status']}"
                    else: action_result = "Error: No target."
                elif action == "KEY_COMBO":
                    keys = params.get("keys", "")
                    if machine_id: 
                        result = await _execute_vm_action(machine_id, "key_combo", {"keys": keys.split("+")})
                        action_result = f"Key combo result: {json.dumps(result)}"
                    elif device_id: 
                        result = await _execute_device_action(device_id, "key_combo", {"keys": keys})
                        action_result = f"Key combo sent. Status: {result['status']}"
                    else: action_result = "Error: No target."
                else:
                    a_low = action.lower()
                    if machine_id: result = await _execute_vm_action(machine_id, a_low, params)
                    elif device_id: result = await _execute_device_action(device_id, a_low, params)
                    else: result = {"error": "No target or unknown action"}
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
