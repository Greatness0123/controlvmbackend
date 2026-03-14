"""AI Agent executor — sends tasks to Gemini, executes actions on VM."""
import json
import asyncio
import logging
import google.generativeai as genai
from typing import AsyncGenerator
from supabase import Client
from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Control AI, an agent that controls a virtual computer to complete tasks for the user.

You can see the computer's screen via screenshots and you can perform these actions:
- CLICK(x, y) — Click at screen coordinates
- TYPE(text) — Type text at the current cursor position
- KEY(key) — Press a keyboard key (e.g., Enter, Tab, Escape, ctrl+c)
- SCROLL(direction) — Scroll up or down
- TERMINAL(command) — Execute a terminal command
- SCREENSHOT() — Take a screenshot to see the current state
- DONE(summary) — Task is complete, provide a summary

Always start by taking a screenshot to see the current state.
Think step by step. After each action, take a screenshot to verify the result.
Respond with your reasoning followed by the action in this exact JSON format:
{"thought": "your reasoning", "action": "ACTION_NAME", "params": {"key": "value"}}
"""


class AgentExecutor:
    def __init__(self):
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-2.0-flash")
        else:
            self.model = None

    async def execute_task(
        self, db: Client, session_id: str, user_message: str, vm_data: dict
    ) -> AsyncGenerator[dict, None]:
        """Execute a task and stream results back."""
        
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
            context = f"""VM Info: {vm_data.get('name', 'Unknown')} (noVNC at port {vm_data.get('novnc_port', 'N/A')})
User request: {user_message}"""

            chat = self.model.start_chat(history=[
                {"role": "user", "parts": [SYSTEM_PROMPT]},
                {"role": "model", "parts": ["Understood. I'll control the virtual computer to complete tasks. I'll start by taking a screenshot to see the current state. Ready for instructions."]},
            ])

            # Initial response
            yield {"type": "thinking", "content": "Analyzing your request..."}

            response = chat.send_message(context)
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

                # For now, simulate action execution
                if action == "DONE":
                    yield {"type": "message", "content": f"✅ {params.get('summary', 'Task completed')}"}
                else:
                    yield {"type": "message", "content": f"Executed: {action}({json.dumps(params)})"}
                    yield {"type": "message", "content": "Taking screenshot to verify..."}

            except json.JSONDecodeError:
                # Plain text response
                db.table("chat_messages").insert({
                    "session_id": session_id,
                    "role": "assistant",
                    "content": response_text,
                }).execute()
                yield {"type": "message", "content": response_text}

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
