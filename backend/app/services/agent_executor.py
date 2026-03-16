"""AI Agent executor — sends tasks to Gemini, executes actions on VM."""
import json
import asyncio
import logging
import google.generativeai as genai
import websockets
from typing import AsyncGenerator
from supabase import Client
from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Control AI, an agent that controls a virtual computer to complete tasks for the user. (Note: Always use Firefox browser for web tasks).

You can see the computer's screen via screenshots and you can perform these actions:
- CLICK(x, y) — Click at screen coordinates (normalized 0-1000).
- TYPE(text) — Type text at the current cursor position
- KEY(key) — Press a keyboard key (e.g., Enter, Tab, Escape, ctrl+c)
- SCROLL(direction) — Scroll up or down
- TERMINAL(command) — Execute a terminal command
- SCREENSHOT() — Take a screenshot to see the current state
- HITL(reason) — Request human intervention for sensitive info (logins, secrets). Provide a reason.
- DONE(summary) — Task is complete, provide a summary

Always start by taking a screenshot to see the current state.
Think step by step. After each action, take a screenshot to verify the result.
When clicking, use x and y normalized from 0 to 1000. (0,0) is top-left, (1000,1000) is bottom-right.
Respond with your reasoning followed by the action in this exact JSON format:
{"thought": "your reasoning", "action": "ACTION_NAME", "params": {"key": "value"}}
"""


class AgentExecutor:
    def __init__(self):
        self.configured = False
        self.model = None

    async def _ensure_configured(self, db: Client):
        """Fetch config from DB if not already done."""
        if self.configured:
            return

        try:
            config_res = db.table("app_config").select("value").eq("key", "api_keys").execute()
            if config_res.data:
                config_val = config_res.data[0].get("value", {})
                gemini_model = config_val.get("gemini_model", "gemini-2.5-flash")
                gemini_keys = config_val.get("gemini_keys", [])
                
                # Use first key if available
                key = gemini_keys[0] if gemini_keys else GEMINI_API_KEY
                
                if key:
                    genai.configure(api_key=key)
                    self.model = genai.GenerativeModel(gemini_model)
                    self.configured = True
                    logger.info(f"Agent configured with model: {gemini_model}")
                else:
                    self.model = None
            else:
                self.model = None
        except Exception as e:
            logger.error(f"Failed to fetch config from DB: {e}")
            self.model = None

    async def execute_task(
        self, db: Client, session_id: str, user_message: str, session_data: dict
    ) -> AsyncGenerator[dict, None]:
        """Execute a task and stream results back."""
        await self._ensure_configured(db)
        vm_data = session_data.get("virtual_machines") or {}
        device_id = session_data.get("device_id")
        
        # Save user message
        db.table("chat_messages").insert({
            "session_id": session_id,
            "role": "user",
            "content": user_message,
        }).execute()

        # Update session title if first message
        session = db.table("chat_sessions").select("*").eq("id", session_id).execute()
        if session.data and session.data[0].get("title") == "New Chat":
            title = user_message[:50] + ("..." if len(user_message) > 50 else "")
            db.table("chat_sessions").update({"title": title}).eq("id", session_id).execute()

        if not self.model:
            # No API key — return helpful message
            msg = "⚠️ Gemini API key not configured. Set GEMINI_API_KEY in your backend .env file to enable AI agent execution."
            db.table("chat_messages").insert({
                "session_id": session_id,
                "role": "assistant",
                "content": msg,
            }).execute()
            yield {"type": "message", "content": msg}
            yield {"type": "done"}
            return

        try:
            # Build context
            target_name = vm_data.get('name') or session_data.get('device_name') or "Remote Desktop"
            context = f"Target Info: {target_name}\nUser request: {user_message}"

            chat = self.model.start_chat(history=[
                {"role": "user", "parts": [SYSTEM_PROMPT]},
                {"role": "model", "parts": ["Understood. I'll control the virtual computer to complete tasks. I'll start by taking a screenshot to see the current state. Ready for instructions."]},
            ])

            # Autonomous loop
            max_steps = 15
            step = 0
            last_status = "running"

            while step < max_steps:
                step += 1
                
                # Check for stop/pause signals from DB
                session_res = db.table("chat_sessions").select("ai_status").eq("id", session_id).execute()
                current_status = session_res.data[0].get("ai_status", "running") if session_res.data else "running"

                if current_status == "stopped":
                    yield {"type": "message", "content": "🛑 AI execution stopped by user."}
                    break
                
                if current_status == "paused":
                    if last_status != "paused":
                        yield {"type": "message", "content": "⏸️ AI paused. Waiting for you to continue."}
                    last_status = "paused"
                    await asyncio.sleep(2) # Poll for change
                    step -= 1 # Don't count pause as a step
                    continue

                if last_status == "paused" and current_status == "running":
                    yield {"type": "message", "content": "▶️ Resuming... taking a fresh look at the screen."}
                    # Force a screenshot after unpause to ensure state accuracy
                    instruction = "Take a screenshot to refresh your state after pause."
                else:
                    instruction = context if step == 1 else "Look at the screen and decide the next step to reach the goal."

                last_status = "running"
                yield {"type": "thinking", "content": f"Step {step}: Analyzing state..."}

                response = chat.send_message(instruction)
                response_text = response.text.strip()

                # Try to parse as action
                try:
                    action_data = json.loads(response_text)
                    thought = action_data.get("thought", "")
                    action = action_data.get("action", "")
                    params = action_data.get("params", {})

                    yield {"type": "thought", "content": thought}
                    yield {"type": "action", "action": action, "params": params}

                    # Save the AI response
                    db.table("chat_messages").insert({
                        "session_id": session_id,
                        "role": "assistant",
                        "content": thought,
                        "action_type": action.lower(),
                        "action_data": params,
                    }).execute()

                    # Execute action
                    if action == "DONE":
                        yield {"type": "message", "content": f"✅ {params.get('summary', 'Task completed')}"}
                        break
                    
                    # Execution logic
                    if device_id:
                        # Broadcast action to the paired desktop
                        db.channel(f"remote_control:{device_id}").send({
                            "type": "broadcast",
                            "event": "action",
                            "payload": {"type": action.lower(), **params}
                        })
                    elif vm_data:
                        agent_port = vm_data.get('agent_port')
                        if agent_port:
                            try:
                                async with websockets.connect(f"ws://127.0.0.1:{agent_port}") as ws:
                                    await ws.send(json.dumps({
                                        "type": "command",
                                        "data": {
                                            "command": action.lower(),
                                            "parameters": params
                                        }
                                    }))
                                    await asyncio.wait_for(ws.recv(), timeout=10.0)
                            except Exception as e:
                                logger.error(f"VM Agent error: {e}")
                    
                    await asyncio.sleep(2) # Wait for action to take effect/screen to update

                except json.JSONDecodeError:
                    # Plain text response
                    db.table("chat_messages").insert({
                        "session_id": session_id,
                        "role": "assistant",
                        "content": response_text,
                    }).execute()
                    yield {"type": "message", "content": response_text}
                    break

            yield {"type": "done"}

        except Exception as e:
            error_msg = f"Agent error: {str(e)}"
            logger.error(error_msg)
            db.table("chat_messages").insert({
                "session_id": session_id,
                "role": "system",
                "content": error_msg,
            }).execute()
            yield {"type": "error", "content": error_msg}
            yield {"type": "done"}


# Singleton
agent_executor = AgentExecutor()
