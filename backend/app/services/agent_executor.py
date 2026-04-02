import json
import asyncio
import logging
import base64
import aiohttp
import websockets
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional, Dict, Any, List
from supabase import Client
from bs4 import BeautifulSoup
from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

ACT_SYSTEM_PROMPT = """You are Control AI, an autonomous agent that controls a virtual computer or paired remote desktop to complete tasks for the user.

You can see the computer's screen via screenshots and perform these actions:

AVAILABLE ACTIONS:
- SCREENSHOT() — Take a screenshot to see current state. Params: {}
- CLICK(x, y) — Left click at coordinates (0-1000). Params: {"x": number, "y": number}
- DOUBLE_CLICK(x, y) — Double click. Params: {"x": number, "y": number}
- RIGHT_CLICK(x, y) — Right click. Params: {"x": number, "y": number}
- MOVE(x, y) — Move mouse. Params: {"x": number, "y": number}
- TYPE(text) — Type text. Params: {"text": string}
- KEY(key) — Press key (Enter, Tab, Escape, Backspace, Delete, Home, End, PageUp, PageDown, Up, Down, Left, Right, F1-F12). Combo keys: ctrl+c, alt+f4. Params: {"key": string}
- SCROLL(direction, amount) — Scroll. Params: {"direction": "up"|"down", "amount": number}
- DRAG(from_x, from_y, to_x, to_y) — Drag-and-drop. Params: {"from_x": number, "from_y": number, "to_x": number, "to_y": number}
- BROWSER_NAVIGATE(url) — Open URL in Firefox. Params: {"url": string}
- BROWSER_GET_CONTENT(url) — Scrape text from URL. Params: {"url": string}
- BROWSER_FIND(query) — Search for text on current page. Params: {"query": string}
- TERMINAL(command) — Run shell command. Params: {"command": string}
- LIST_APPS() — Get installed software list. Params: {}
- SECRET_LOOKUP(service_name) — Find credentials in vault. Params: {"service_name": string}
- SECRET_TYPE(secret_id, field) — Auto-type username/password from vault. Params: {"secret_id": string, "field": "username"|"password"}
- HITL(reason) — Pause for user input. Params: {"reason": string}
- DONE(summary) — Finish task. Params: {"summary": string}

CONSTRAINTS & RULES:
1. Always start with SCREENSHOT() to establish visual context.
2. After every action that changes the screen, use SCREENSHOT() to verify success.
3. Coordinates are normalized 0 to 1000. (0,0) is TOP-LEFT, (1000,1000) is BOTTOM-RIGHT.
4. For web tasks, prioritize BROWSER_NAVIGATE and UI interaction over scraping if visual feedback is needed.
5. NEVER ask the user for passwords. Use SECRET_TYPE or HITL.
6. Be autonomous. Solve the task from start to finish without asking "Should I...".
7. Environment Awareness:
   - If 'Mode' is 'VM', you are in a Linux XFCE environment. Use 'TERMINAL' for bash commands.
   - If 'Mode' is 'Remote Desktop', you are on the user's host OS (Windows/Mac/Linux). Use 'TERMINAL' for the host shell.
8. Safety: If 'Target Status' is NOT 'running' (VM) or 'Online' (Remote Desktop), stop and inform the user.
9. Greetings: For "HI", "Hello", etc., just say hi back. Do NOT use JSON actions for casual chat.

RESPONSE FORMAT (Strict JSON):
{"thought": "reasoning about current state and next step", "action": "ACTION_NAME", "params": {"key": "value"}}"""

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
        parts.insert(0, {"mime_type": "image/png", "data": image_b64})
    
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

            content: List[Dict[str, Any]] = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                {"type": "text", "text": msg["content"]}
            ]
            formatted_messages.append({
                "role": "user",
                "content": content
            })
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
            formatted_messages.append({
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
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
            formatted_messages.append({
                "role": "user",
                "content": msg["content"],
                "images": [image_b64]
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

async def _take_screenshot_vm(agent_port: Optional[int]) -> Optional[str]:

    if not agent_port: return None
    import websockets
    try:
        async with websockets.connect(f"ws://127.0.0.1:{agent_port}", open_timeout=5) as ws:
            await ws.send(json.dumps({"type": "command", "data": {"command": "screenshot", "parameters": {}}}))
            response = await asyncio.wait_for(ws.recv(), timeout=15.0)
            if not isinstance(response, str): return None
            data = json.loads(response)
            return data.get("data", {}).get("screenshot") or data.get("screenshot")
    except Exception as e:
        logger.error(f"Screenshot error from VM: {e}")
        return None

async def _execute_vm_action(agent_port: Optional[int], action: str, params: Dict[str, Any]) -> Optional[Dict]:

    if not agent_port: return {"error": "No agent port"}
    import websockets
    try:
        async with websockets.connect(f"ws://127.0.0.1:{agent_port}", open_timeout=5) as ws:

            cmd_map = {
                "click": "click", "double_click": "double_click", "right_click": "right_click",
                "move": "move", "type": "type", "key": "key_press", "key_press": "key_press",
                "scroll": "scroll", "drag": "drag",
                "browser_navigate": "browser_navigate",
                "browser_get_content": "browser_get_content",
                "browser_find": "browser_find",
                "terminal": "terminal",
            }
            cmd = cmd_map.get(action.lower(), action.lower())
            
            await ws.send(json.dumps({
                "type": "command",
                "data": {"command": cmd, "parameters": params}
            }))
            response = await asyncio.wait_for(ws.recv(), timeout=30.0)
            if not isinstance(response, str): return {"error": "Invalid response type"}
            return json.loads(response)
    except Exception as e:
        logger.error(f"VM action error ({action}): {e}")
        return {"error": str(e)}

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
                # Fallback to default Gemini if possible
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
            # 1. Fetch current usage
            res = db.table("users").select("act_count, ask_count, total_token_usage, daily_token_usage").eq("id", user_id).execute()
            if not res.data:
                return

            user = res.data[0]
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # 2. Update mode counts
            update_data = {}
            if tokens == 0: # Only increment count once per session/request start
                if mode == "act":
                    update_data["act_count"] = (user.get("act_count") or 0) + 1
                else:
                    update_data["ask_count"] = (user.get("ask_count") or 0) + 1

            # 3. Update token counts
            if tokens > 0:
                update_data["total_token_usage"] = (user.get("total_token_usage") or 0) + tokens
                # Maintain parity with desktop's token_usage map
                token_usage = user.get("token_usage") or {}
                if mode not in token_usage: token_usage[mode] = {"prompt": 0, "candidates": 0, "total": 0}
                token_usage[mode]["prompt"] += tokens // 2
                token_usage[mode]["candidates"] += tokens // 2
                token_usage[mode]["total"] += tokens
                update_data["token_usage"] = token_usage

            # 4. Update daily statistics (consolidated in daily_token_usage)
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
                # Also track raw counts for desktop-web parity
                if "prompt" not in daily_stats[today]: daily_stats[today]["prompt"] = 0
                if "candidates" not in daily_stats[today]: daily_stats[today]["candidates"] = 0
                daily_stats[today]["prompt"] += tokens // 2
                daily_stats[today]["candidates"] += tokens // 2

            update_data["daily_token_usage"] = daily_stats

            # 5. Save back to DB
            db.table("users").update(update_data).eq("id", user_id).execute()

        except Exception as e:
            logger.error(f"Failed to update user usage: {e}")

    async def execute_task(
        self, db: Client, session_id: str, user_message: str, session_data: dict, mode: str = "act"
    ) -> AsyncGenerator[dict, None]:

        vm_data = session_data.get("virtual_machines") or {}
        device_id = session_data.get("device_id")
        user_id = session_data.get("user_id", "")

        # Increment session-level usage count
        await self._update_usage(db, user_id, mode, tokens=0)

        # Estimate tokens (very rough for now)
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

            conversation: list = []

            uploaded_image = None
            if "data:image/" in user_message and ";base64," in user_message:
                try:

                    parts = user_message.split("data:image/")
                    if len(parts) > 1:
                        img_part = parts[1].split("]")[0]
                        mime = "image/" + img_part.split(";")[0]
                        b64 = img_part.split(",")[1]
                        uploaded_image = b64

                        user_message = user_message.split("[Attached file:")[0].strip()
                except:
                    pass

            target_status = "Unknown"
            if vm_data:
                target_status = vm_data.get("status", "Unknown")
            elif device_id:
                try:
                    dev_res = db.table("paired_devices").select("status").eq("id", device_id).execute()
                    if dev_res.data:
                        status_val = dev_res.data[0].get("status", "Unknown")
                        target_status = "Online" if status_val == "paired" else "Offline" if status_val == "revoked" else status_val
                except:
                    pass

            initial_msg = (
                f"Target: {target_name}\n"
                f"Target Status: {target_status}\n"
                f"Mode: {'VM' if vm_data else 'Remote Desktop' if device_id else 'Chat Only'}\n"
                f"Task: {user_message}"
            )
            conversation.append({"role": "user", "content": initial_msg})
            
            max_steps = 30
            step = 0
            last_screenshot: Optional[str] = uploaded_image # Start with uploaded image if present

            final_agent_port: Optional[int] = int(agent_port) if agent_port else None

            while step < max_steps:
                step += 1

                try:
                    session_res = db.table("chat_sessions").select("ai_status").eq("id", session_id).execute()
                    current_status = session_res.data[0].get("ai_status", "running") if session_res.data else "running"
                except Exception:
                    current_status = "running"

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
                    try:
                        if mode == "workflow":
                            provider_config["system_prompt"] = WORKFLOW_SYSTEM_PROMPT
                        else:
                            provider_config["system_prompt"] = ASK_SYSTEM_PROMPT

                        response_text = await self._call_ai(
                            provider_config, conversation, last_screenshot
                        )

                        # Add tokens to usage
                        await self._update_usage(db, user_id, mode, tokens=tokens_per_turn)

                        yield {"type": "message", "content": response_text}
                        yield {"type": "done"}
                        
                        db.table("chat_messages").insert({
                            "session_id": session_id, "role": "assistant", "content": response_text
                        }).execute()
                        return
                    except Exception as e:
                        yield {"type": "error", "content": f"AI error: {str(e)}"}
                        return

                try:
                    provider_config["system_prompt"] = ACT_SYSTEM_PROMPT
                    response_text = await self._call_ai(
                        provider_config, conversation, last_screenshot
                    )
                except ValueError as e:
                    msg = str(e)
                    db.table("chat_messages").insert({"session_id": session_id, "role": "system", "content": msg}).execute()
                    yield {"type": "message", "content": f"⚠️ {msg}"}
                    yield {"type": "done"}
                    return
                except Exception as e:
                    yield {"type": "error", "content": f"AI error: {str(e)}"}
                    break

                try:

                    clean_text = response_text
                    if "```json" in clean_text:
                        clean_text = clean_text.split("```json")[1].split("```")[0].strip()
                    elif "```" in clean_text:
                        clean_text = clean_text.split("```")[1].split("```")[0].strip()
                    
                    action_data = json.loads(clean_text)
                    thought = action_data.get("thought", "")
                    action = action_data.get("action", "").upper()
                    params = action_data.get("params", {})

                except (json.JSONDecodeError, ValueError):

                    db.table("chat_messages").insert({
                        "session_id": session_id, "role": "assistant", "content": response_text
                    }).execute()
                    yield {"type": "message", "content": response_text}
                    conversation.append({"role": "assistant", "content": response_text})
                    break

                if thought:
                    yield {"type": "thought", "content": thought}
                yield {"type": "action", "action": action, "params": params}

                # Update token usage for each ACT step
                await self._update_usage(db, user_id, "act", tokens=tokens_per_turn)

                if not session_id.startswith("wf_gen_"):
                    db.table("chat_messages").insert({
                        "session_id": session_id,
                        "role": "assistant",
                        "content": thought,
                        "action_type": action.lower(),
                        "action_data": params,
                    }).execute()

                conversation.append({"role": "assistant", "content": response_text})

                action_result = ""
                last_screenshot = None  # Reset screenshot each step

                if action == "DONE":
                    summary = params.get("summary", "Task completed.")
                    db.table("chat_messages").insert({
                        "session_id": session_id, "role": "assistant", "content": f"✅ {summary}"
                    }).execute()
                    yield {"type": "message", "content": f"✅ {summary}"}
                    break

                elif action == "HITL":
                    reason = params.get("reason", "Please complete the requested action.")
                    db.table("chat_messages").insert({
                        "session_id": session_id, "role": "assistant",
                        "content": f"🔐 Human input needed: {reason}"
                    }).execute()
                    yield {"type": "hitl", "content": reason}
                    break

                elif action == "SECRET_LOOKUP":
                    service = params.get("service_name", "")
                    try:
                        res = db.table("secrets").select("id, name, service, username")\
                            .eq("user_id", user_id).ilike("service", f"%{service}%").execute()
                        action_result = f"Found {len(res.data)} secrets in vault:\n{json.dumps(res.data, indent=2)}"
                    except Exception as e:
                        action_result = f"Vault lookup failed: {e}"

                elif action == "SECRET_TYPE":
                    sid = params.get("secret_id")
                    field = params.get("field", "password") # or username
                    try:
                        res = db.table("secrets").select("*").eq("id", sid).eq("user_id", user_id).execute()
                        if res.data:
                            secret = res.data[0]
                            val = secret.get("password") if field == "password" else secret.get("username")
                            if val:

                                if agent_port:
                                    await _execute_vm_action(agent_port, "type", {"text": val})
                                elif device_id:
                                    db.channel(f"remote_control:{device_id}").send({
                                        "type": "broadcast", "event": "action", 
                                        "payload": {"type": "type", "text": val}
                                    })
                                action_result = f"Successfully typed {field} from secret '{secret['name']}'."
                            else:
                                action_result = f"Field '{field}' not found in secret."
                        else:
                            action_result = "Secret not found."
                    except Exception as e:
                        action_result = f"Vault operation failed: {e}"

                elif action == "SCREENSHOT":

                    if final_agent_port:
                        screenshot = await _take_screenshot_vm(final_agent_port)
                        if screenshot:
                            last_screenshot = screenshot
                            action_result = "Screenshot taken."
                        else:
                            action_result = "Screenshot failed — VM agent may not be running."
                    elif device_id:

                        try:
                            # Realtime broadcast from server to client
                            import asyncio
                            from app.auth import get_async_service_client

                            async def send_and_cleanup():
                                adb = await get_async_service_client()
                                channel = adb.channel(f"remote_control:{device_id}")
                                await channel.subscribe()
                                await channel.send_broadcast("action", {"type": "screenshot"})
                                await adb.remove_channel(channel)

                            asyncio.create_task(send_and_cleanup())
                            action_result = "Screenshot requested from desktop."
                        except Exception as e:
                            action_result = f"Screenshot request failed: {e}"
                    else:
                        action_result = "No target connected for screenshot."

                elif action == "BROWSER_GET_CONTENT":
                    url = params.get("url", "")
                    if url:
                        content = await _web_scrape(url)
                        action_result = f"Page content from {url}:\n{content}"
                    else:
                        action_result = "No URL provided."

                elif action == "LIST_APPS":
                    if final_agent_port:
                        # Linux VM: Search for .desktop files
                        cmd = "find /usr/share/applications /home/controluser/.local/share/applications -name '*.desktop' -exec grep -l 'Exec=' {} + | xargs grep -h '^Name=' | cut -d'=' -f2 | sort -u"
                        result = await _execute_vm_action(final_agent_port, "terminal", {"command": cmd})
                        apps = result.get("data", {}).get("output", "") if isinstance(result, dict) else str(result)
                        action_result = f"Installed Applications:\n{apps}"
                    elif device_id:
                        # Desktop: Send broadcast to request apps
                        try:
                            import asyncio
                            from app.auth import get_async_service_client

                            async def send_and_cleanup():
                                adb = await get_async_service_client()
                                channel = adb.channel(f"remote_control:{device_id}")
                                await channel.subscribe()
                                await channel.send_broadcast("action", {"type": "list_apps"})
                                await adb.remove_channel(channel)

                            asyncio.create_task(send_and_cleanup())
                            action_result = "Application list requested from desktop. Please wait for the response in the next turn if not immediately available."
                        except Exception as e:
                            action_result = f"Error: {e}"
                    else:
                        action_result = "No target connected."

                elif action == "TERMINAL":
                    command = params.get("command", "")
                    if device_id:

                        try:
                            perm_res = db.table("app_config").select("value")\
                                .eq("key", f"terminal_permission_{user_id}").execute()
                            term_perm = "ask" if not perm_res.data else perm_res.data[0].get("value", {}).get("permission", "ask")
                        except Exception:
                            term_perm = "ask"
                        
                        if term_perm == "never":
                            action_result = "Terminal execution is disabled. Change in Settings > Security."
                        elif term_perm == "ask":

                            yield {"type": "terminal_permission", "command": command}
                            break
                        else:

                            try:
                                db.channel(f"remote_control:{device_id}").send({
                                    "type": "broadcast",
                                    "event": "action",
                                    "payload": {"type": "terminal", "command": command}
                                })
                                action_result = f"Terminal command sent: {command}"
                            except Exception as e:
                                action_result = f"Error: {e}"
                    elif final_agent_port:

                        result = await _execute_vm_action(final_agent_port, "terminal", {"command": command})
                        action_result = result.get("data", {}).get("output", "") if isinstance(result, dict) else str(result)
                    else:
                        action_result = "No target connected for terminal commands."

                else:

                    action_lower = action.lower()
                    
                    if final_agent_port:
                        result = await _execute_vm_action(final_agent_port, action_lower, params)
                        action_result = json.dumps(result) if result else "Action executed."

                        await asyncio.sleep(0.8)
                        screenshot = await _take_screenshot_vm(final_agent_port)
                        if screenshot:
                            last_screenshot = screenshot
                    
                    elif device_id:
                        try:
                            import asyncio
                            from app.auth import get_async_service_client

                            async def send_and_cleanup():
                                adb = await get_async_service_client()
                                channel = adb.channel(f"remote_control:{device_id}")
                                await channel.subscribe()
                                await channel.send_broadcast("action", {"type": action_lower, **params})
                                await adb.remove_channel(channel)

                            asyncio.create_task(send_and_cleanup())
                            action_result = f"Action '{action}' sent to remote desktop."
                        except Exception as e:
                            action_result = f"Error dispatching action: {e}"
                    else:
                        action_result = "No target connected. Attach a VM or paired device first."

                if action_result:
                    conversation.append({"role": "user", "content": f"Action result: {action_result}"})

                await asyncio.sleep(0.5)

            yield {"type": "done"}

        except Exception as e:
            error_msg = f"Agent error: {str(e)}"
            logger.exception(error_msg)
            try:
                if not session_id.startswith("wf_gen_") and 'response_text' in locals():
                    db.table("chat_messages").insert({
                        "session_id": session_id, "role": "assistant", "content": response_text
                    }).execute()
            except Exception:
                pass
            yield {"type": "error", "content": error_msg}
            yield {"type": "done"}

agent_executor = AgentExecutor()
