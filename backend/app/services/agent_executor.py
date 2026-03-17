import json
import asyncio
import logging
import base64
import aiohttp
import websockets
from typing import AsyncGenerator, Optional, Dict, Any, List
from supabase import Client
from bs4 import BeautifulSoup
from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Control AI, an autonomous agent that controls a virtual computer or paired remote desktop to complete tasks for the user.

You can see the computer's screen via screenshots and perform these actions:

COMPUTER CONTROL:
- SCREENSHOT() — Take a screenshot to see the current state (always start here)
- CLICK(x, y) — Click at screen coordinates (normalized 0-1000). (0,0) = top-left, (1000,1000) = bottom-right
- DOUBLE_CLICK(x, y) — Double click at coordinates
- RIGHT_CLICK(x, y) — Right click at coordinates
- MOVE(x, y) — Move mouse to coordinates without clicking
- TYPE(text) — Type text at the current cursor position
- KEY(key) — Press keyboard key (e.g., Enter, Tab, Escape, ctrl+c, ctrl+v, ctrl+a, alt+F4)
- SCROLL(direction, amount) — Scroll up or down (amount in lines, default 3)
- DRAG(from_x, from_y, to_x, to_y) — Click and drag from one position to another

BROWSER/WEB:
- BROWSER_NAVIGATE(url) — Open a URL in the Firefox browser
- BROWSER_GET_CONTENT(url) — Fetch and read the text content of a webpage (no UI interaction needed)
- BROWSER_FIND(query) — Find text or element on current browser page

TERMINAL:
- TERMINAL(command) — Execute a terminal/shell command on the target machine
- TERMINAL_READ() — Read recent terminal output

HUMAN IN THE LOOP:
- HITL(reason) — Pause and ask user to perform a sensitive action (e.g., enter credentials). User will click "Done" when finished.

COMPLETION:
- DONE(summary) — Task complete. Provide a clear summary of what was accomplished.

RULES:
1. Always start with SCREENSHOT() to see the current state.
2. After every action, use SCREENSHOT() to verify the result.
3. Coordinates are normalized 0-1000 (not pixels). Be precise.
4. For web tasks, always use Firefox. Navigate with BROWSER_NAVIGATE, not the terminal.
5. For login forms, use SECRET_TYPE if available, otherwise use HITL — never ask the user to type credentials in chat.
6. If an action fails, try an alternative approach.
7. Be autonomous — complete the full task without asking for permission unless absolutely necessary.
8. When using TERMINAL for VM, commands run in the VM's bash shell.
9. When using TERMINAL for Remote Desktop, commands run on the user's machine.

SECURE VAULT:
- SECRET_LOOKUP(service_name) — Search for saved credentials (username/metadata). Does NOT show passwords.
- SECRET_TYPE(secret_id, field) — Automatically type a secret's field (username or password) into the target machine.

RESPONSE FORMAT (strict JSON):
{"thought": "your step-by-step reasoning", "action": "ACTION_NAME", "params": {"key": "value"}}
"""

# ─── Provider Adapters ────────────────────────────────────────────────────────

async def _call_gemini(model: str, api_key: str, messages: list, image_b64: Optional[str] = None) -> str:
    """Call Google Gemini API."""
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    
    gemini_model = genai.GenerativeModel(model)
    
    # Build parts
    parts = []
    for msg in messages:
        if msg["role"] == "user":
            parts.append(msg["content"])
    
    # Add screenshot if provided
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
    base_url: str = "https://api.openai.com/v1"
) -> str:
    """Call OpenAI-compatible API (OpenAI, OpenRouter, xAI, etc.)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    # Build messages for OpenAI format
    formatted_messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    for msg in messages:
        if msg["role"] == "user" and image_b64 and msg == messages[-1]:
            # Add image to last user message
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
    """Call Anthropic Claude API."""
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
    """Call local Ollama instance."""
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


# ─── Screenshot Tool ──────────────────────────────────────────────────────────

async def _take_screenshot_vm(agent_port: Optional[int]) -> Optional[str]:
    """Take screenshot from VM agent, returns base64."""
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
    """Execute an action on VM via WebSocket agent."""
    if not agent_port: return {"error": "No agent port"}
    import websockets
    try:
        async with websockets.connect(f"ws://127.0.0.1:{agent_port}", open_timeout=5) as ws:
            # Map action names to agent commands
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
    """Fetch and return readable text content of a URL."""
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


# ─── Agent Executor ───────────────────────────────────────────────────────────

class AgentExecutor:
    def __init__(self):
        pass

    async def _get_provider_config(self, db: Client, user_id: str) -> Dict:
        """Fetch AI provider configuration for this user from DB."""
        try:
            # Try user-specific config first
            res = db.table("app_config").select("value").eq("key", f"api_keys_{user_id}").execute()
            if res.data:
                return res.data[0].get("value", {})
            
            # Fall back to global config
            res = db.table("app_config").select("value").eq("key", "api_keys").execute()
            if res.data:
                return res.data[0].get("value", {})
        except Exception as e:
            logger.error(f"Error fetching provider config: {e}")
        
        # Defaults
        return {"provider": "gemini", "gemini_model": "gemini-2.5-flash"}

    async def _call_ai(
        self, config: Dict, messages: list, image_b64: Optional[str] = None
    ) -> str:
        """Route to the correct AI provider based on config."""
        provider = config.get("provider", "gemini")
        
        if provider == "gemini":
            key = config.get("gemini_api_key") or GEMINI_API_KEY
            model = config.get("gemini_model", "gemini-2.5-flash")
            if not key:
                raise ValueError("No Gemini API key configured. Go to Settings > AI to add your key.")
            return await _call_gemini(model, key, messages, image_b64)
        
        elif provider == "openai":
            key = config.get("openai_api_key", "")
            model = config.get("openai_model", "gpt-4o")
            if not key:
                raise ValueError("No OpenAI API key configured. Go to Settings > AI to add your key.")
            return await _call_openai_compat(model, key, messages, image_b64)
        
        elif provider == "anthropic":
            key = config.get("anthropic_api_key", "")
            model = config.get("anthropic_model", "claude-3-5-sonnet-20241022")
            if not key:
                raise ValueError("No Anthropic API key configured. Go to Settings > AI to add your key.")
            return await _call_anthropic(model, key, messages, image_b64)
        
        elif provider == "openrouter":
            key = config.get("openrouter_api_key", "")
            model = config.get("openrouter_model", "anthropic/claude-3.5-sonnet")
            if not key:
                raise ValueError("No OpenRouter API key configured. Go to Settings > AI to add your key.")
            return await _call_openai_compat(
                model, key, messages, image_b64,
                base_url="https://openrouter.ai/api/v1"
            )
        
        elif provider == "xai":
            key = config.get("xai_api_key", "")
            model = config.get("xai_model", "grok-2-vision-1212")
            if not key:
                raise ValueError("No xAI API key configured. Go to Settings > AI to add your key.")
            return await _call_openai_compat(
                model, key, messages, image_b64,
                base_url="https://api.x.ai/v1"
            )
        
        elif provider == "ollama":
            model = config.get("ollama_model", "llava")
            return await _call_ollama(model, messages, image_b64)
        
        else:
            # Default to Gemini with server key
            key = GEMINI_API_KEY
            if not key:
                raise ValueError("No AI provider configured. Go to Settings > AI to set up a provider.")
            return await _call_gemini("gemini-2.5-flash", key, messages, image_b64)

    async def execute_task(
        self, db: Client, session_id: str, user_message: str, session_data: dict
    ) -> AsyncGenerator[dict, None]:
        """Execute a task and stream results back."""
        
        vm_data = session_data.get("virtual_machines") or {}
        device_id = session_data.get("device_id")
        user_id = session_data.get("user_id", "")
        
        # Get provider config
        provider_config = await self._get_provider_config(db, user_id)
        
        # Save user message
        db.table("chat_messages").insert({
            "session_id": session_id,
            "role": "user",
            "content": user_message,
        }).execute()

        # Auto-title session
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
            
            # Conversation history for multi-turn context
            conversation: list = []
            
            # Detect data URLs in user message for multimodal support
            uploaded_image = None
            if "data:image/" in user_message and ";base64," in user_message:
                try:
                    # Extract from [Attached file: data:image/png;base64,...] or similar
                    parts = user_message.split("data:image/")
                    if len(parts) > 1:
                        img_part = parts[1].split("]")[0]
                        mime = "image/" + img_part.split(";")[0]
                        b64 = img_part.split(",")[1]
                        uploaded_image = b64
                        # Clean up message for cleaner conversation
                        user_message = user_message.split("[Attached file:")[0].strip()
                except:
                    pass

            # Add initial context
            initial_msg = (
                f"Target: {target_name}\n"
                f"Mode: {'VM' if vm_data else 'Remote Desktop' if device_id else 'Chat Only'}\n"
                f"Task: {user_message}"
            )
            conversation.append({"role": "user", "content": initial_msg})
            
            max_steps = 30
            step = 0
            last_screenshot: Optional[str] = uploaded_image # Start with uploaded image if present
            
            # Ensure agent_port is int or None
            final_agent_port: Optional[int] = int(agent_port) if agent_port else None

            while step < max_steps:
                step += 1

                # Check stop/pause signals
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

                # Call AI with screenshot context
                try:
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

                # Parse action
                try:
                    # Clean up JSON if wrapped in markdown
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
                    # Plain text response — save and continue
                    db.table("chat_messages").insert({
                        "session_id": session_id, "role": "assistant", "content": response_text
                    }).execute()
                    yield {"type": "message", "content": response_text}
                    conversation.append({"role": "assistant", "content": response_text})
                    break

                # Yield thought
                if thought:
                    yield {"type": "thought", "content": thought}
                yield {"type": "action", "action": action, "params": params}

                # Save to DB
                db.table("chat_messages").insert({
                    "session_id": session_id,
                    "role": "assistant",
                    "content": thought,
                    "action_type": action.lower(),
                    "action_data": params,
                }).execute()

                # Add AI response to conversation
                conversation.append({"role": "assistant", "content": response_text})

                # ── Handle actions ──────────────────────────────────────────
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
                                # Execute type action
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
                    # Take screenshot from VM or signal desktop
                    if final_agent_port:
                        screenshot = await _take_screenshot_vm(final_agent_port)
                        if screenshot:
                            last_screenshot = screenshot
                            action_result = "Screenshot taken."
                        else:
                            action_result = "Screenshot failed — VM agent may not be running."
                    elif device_id:
                        # Request screenshot from desktop via Supabase channel
                        try:
                            db.channel(f"remote_control:{device_id}").send({
                                "type": "broadcast",
                                "event": "action",
                                "payload": {"type": "screenshot"}
                            })
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

                elif action == "TERMINAL":
                    command = params.get("command", "")
                    if device_id:
                        # Check terminal permissions
                        try:
                            perm_res = db.table("app_config").select("value")\
                                .eq("key", f"terminal_permission_{user_id}").execute()
                            term_perm = "ask" if not perm_res.data else perm_res.data[0].get("value", {}).get("permission", "ask")
                        except Exception:
                            term_perm = "ask"
                        
                        if term_perm == "never":
                            action_result = "Terminal execution is disabled. Change in Settings > Security."
                        elif term_perm == "ask":
                            # Signal frontend to ask for permission
                            yield {"type": "terminal_permission", "command": command}
                            break
                        else:
                            # Always run — broadcast to desktop
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
                        # Run in VM
                        result = await _execute_vm_action(final_agent_port, "terminal", {"command": command})
                        action_result = result.get("data", {}).get("output", "") if isinstance(result, dict) else str(result)
                    else:
                        action_result = "No target connected for terminal commands."

                else:
                    # All other actions (CLICK, TYPE, MOVE, SCROLL, KEY, BROWSER_NAVIGATE, etc.)
                    action_lower = action.lower()
                    
                    if final_agent_port:
                        result = await _execute_vm_action(final_agent_port, action_lower, params)
                        action_result = json.dumps(result) if result else "Action executed."
                        
                        # Auto-screenshot after action for visual verification
                        await asyncio.sleep(0.8)
                        screenshot = await _take_screenshot_vm(final_agent_port)
                        if screenshot:
                            last_screenshot = screenshot
                    
                    elif device_id:
                        try:
                            db.channel(f"remote_control:{device_id}").send({
                                "type": "broadcast",
                                "event": "action",
                                "payload": {"type": action_lower, **params}
                            })
                            action_result = f"Action '{action}' sent to remote desktop."
                        except Exception as e:
                            action_result = f"Error dispatching action: {e}"
                    else:
                        action_result = "No target connected. Attach a VM or paired device first."

                # Add action result to conversation for next iteration
                if action_result:
                    conversation.append({"role": "user", "content": f"Action result: {action_result}"})

                await asyncio.sleep(0.5)

            yield {"type": "done"}

        except Exception as e:
            error_msg = f"Agent error: {str(e)}"
            logger.exception(error_msg)
            try:
                db.table("chat_messages").insert({
                    "session_id": session_id, "role": "system", "content": error_msg
                }).execute()
            except Exception:
                pass
            yield {"type": "error", "content": error_msg}
            yield {"type": "done"}


# Singleton
agent_executor = AgentExecutor()
