"""
Agent Executor — orchestrates AI-driven computer automation.

Uses persistent WebSocket connections via VMControlService and
comprehensive system prompts inspired by open-computer-use.

Enhanced with:
- Response truncation to prevent context overflow
- frontendScreenshot filtering from tool results
- Enhanced error handling and connection recovery
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

MAX_TOOL_RESPONSE_LENGTH = 5000

ERROR_MESSAGES = {
    "quota": "AI service temporarily unavailable. Please try again.",
    "rate_limit": "AI service is busy. Please wait a moment and retry.",
    "connection": "Failed to connect to AI service. Please check your connection.",
    "authentication": "AI service authentication failed. Please check your API key.",
    "timeout": "AI request timed out. Please try again.",
    "default": "Agent encountered an error. Please try again.",
}

def sanitize_error(error: str) -> str:
    """Sanitize error messages to hide sensitive API details from users."""
    error_lower = error.lower()
    
    if any(x in error_lower for x in ["429", "quota", "rate limit", "exceeded", "limit:"]):
        logger.warning(f"Rate limit/quota error sanitized: {error[:100]}...")
        return ERROR_MESSAGES["quota"]
    
    if any(x in error_lower for x in ["timeout", "timed out"]):
        return ERROR_MESSAGES["timeout"]
    
    if any(x in error_lower for x in ["auth", "unauthorized", "api key", "invalid"]):
        return ERROR_MESSAGES["authentication"]
    
    if any(x in error_lower for x in ["connection", "connect", "network", "dns"]):
        return ERROR_MESSAGES["connection"]
    
    return ERROR_MESSAGES["default"]


def truncate_tool_response(result: Any, max_length: int = MAX_TOOL_RESPONSE_LENGTH) -> str:
    """Truncate tool response to prevent context overflow, preserving screenshot references"""
    if isinstance(result, dict):
        filtered = {k: v for k, v in result.items() if k != "frontendScreenshot"}
        text = json.dumps(filtered, default=str)
    else:
        text = str(result)
    
    if len(text) > max_length:
        return text[:max_length] + "\n... [response truncated]"
    return text

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

ACT_SYSTEM_PROMPT = """You are Control AI, a powerful autonomous computer control agent with FULL ACCESS to the target machine.
Your mission is to complete ANY user task efficiently, quickly, and accurately by controlling the computer directly.

## YOUR CAPABILITIES - YOU CAN DO ANYTHING:

### 🎯 CORE ACTIONS
- CLICK, DOUBLE_CLICK, RIGHT_CLICK at any coordinates
- TYPE text anywhere
- KEY press (single keys like Enter, Tab, Escape)
- KEY_COMBO for shortcuts (ctrl+c, ctrl+v, ctrl+a, alt+tab, etc.)
- SCROLL up/down
- DRAG and drop

### 📁 FILE OPERATIONS - HOW TO USE:
- **FILE_READ**: Read file content
  - Params: {"filepath": "/home/controluser/Desktop/example.txt"}
  - Use absolute paths starting with /home/controluser/

- **FILE_WRITE**: Write/create new files
  - Params: {"filepath": "/home/controluser/Desktop/test.py", "content": "print('hello world')"}
  - Creates new file or overwrites existing

- **FILE_EDIT**: Edit files (find and replace)
  - Params: {"filepath": "/home/controluser/Desktop/test.py", "old_text": "old", "new_text": "new"}
  - Replaces first occurrence of old_text with new_text

- **FILE_APPEND**: Append to files
  - Params: {"filepath": "/home/controluser/Desktop/test.py", "content": "\nmore content"}

- **FILE_DELETE**: Delete files
  - Params: {"filepath": "/home/controluser/Desktop/test.py"}

- **FILE_EXISTS**: Check if file exists
  - Params: {"filepath": "/home/controluser/Desktop/test.py"}

- **DIRECTORY_LIST**: List directory contents
  - Params: {"path": "/home/controluser/Desktop"}
  - Returns: list of files and folders with names, sizes, and whether they're directories

- **DIRECTORY_DELETE**: Delete directories
  - Params: {"dirpath": "/home/controluser/Desktop/foldername"}

- **FILE_ZIP**: Download entire folders as ZIP
  - Params: {"path": "/home/controluser/Desktop/foldername"}
  - Returns base64 encoded ZIP file

- **FILE_DOWNLOAD**: Download individual files
  - Params: {"path": "/home/controluser/Desktop/file.txt"}
  - Returns base64 encoded file

### 💻 APPLICATION CONTROL:
- **OPEN_TERMINAL**: Open terminal - {"action": "OPEN_TERMINAL", "params": {}}
- **OPEN_CODE_EDITOR**: Open code editor (micro) - {"action": "OPEN_CODE_EDITOR", "params": {}}
- **OPEN_FILE_MANAGER**: Open file manager (Thunar) - {"action": "OPEN_FILE_MANAGER", "params": {}}
- **LIST_APPS**: List available applications - {"action": "LIST_APPS", "params": {}}

### 🌐 BROWSER AUTOMATION:
- **BROWSER_OPEN**: Open Firefox - {"action": "BROWSER_OPEN", "params": {}}
- **BROWSER_NAVIGATE**: Navigate to URL - {"action": "BROWSER_NAVIGATE", "params": {"url": "https://example.com"}}
- **BROWSER_GET_CONTENT**: Get page content - {"action": "BROWSER_GET_CONTENT", "params": {}}

### 🪟 WINDOW MANAGEMENT:
- **LIST_WINDOWS**: List all open windows - {"action": "LIST_WINDOWS", "params": {}}
- **SWITCH_WINDOW**: Switch to window - {"action": "SWITCH_WINDOW", "params": {"window": "window title or ID"}}
- **CLOSE_WINDOW**: Close window - {"action": "CLOSE_WINDOW", "params": {"window_title": "title"}}
- **MINIMIZE_WINDOW**: Minimize - {"action": "MINIMIZE_WINDOW", "params": {}}
- **MAXIMIZE_WINDOW**: Maximize - {"action": "MAXIMIZE_WINDOW", "params": {}}
- **RESTORE_WINDOW**: Restore - {"action": "RESTORE_WINDOW", "params": {}}
- **MOVE_WINDOW**: Move window - {"action": "MOVE_WINDOW", "params": {"x": 100, "y": 100}}

### 🖥️ TERMINAL/SHELL:
- **TERMINAL**: Execute shell command
  - Params: {"command": "ls -la /home/controluser/Desktop"}
  - Returns command output

### 🔍 ADVANCED TOOLS:
- **SCREENSHOT**: Capture screen - {"action": "SCREENSHOT", "params": {}}
- **DETECT_ELEMENTS**: Detect UI elements - {"action": "DETECT_ELEMENTS", "params": {}}
- **OCR**: Extract text from screenshot - {"action": "OCR", "params": {}}

## CRITICAL OPERATING RULES:

1. **ALWAYS start with SCREENSHOT()** to see current screen state
2. **After EVERY action**, take SCREENSHOT to verify the result
3. **Coordinates are normalized 0-1000** - (0,0) is top-left, (1000,1000) is bottom-right
4. **Click before typing** - Always CLICK on target input field first
5. **Be precise with coordinates** - Look at screenshot carefully to identify exact locations
6. **Retry on failure** - If click doesn't work, try nearby coordinates or different approach
7. **Use SCROLL** to find elements below visible area
8. **For web tasks:**
   - Use BROWSER_NAVIGATE to open URLs
   - Use BROWSER_GET_CONTENT for quick text scraping
   - Navigate, observe, then interact
9. **For typing in forms:**
   - CLICK the field first
   - Then TYPE the text
   - Use KEY("Tab") to move to next field
   - Use KEY("Enter") to submit
10. **Key combos:** Use KEY_COMBO for shortcuts like "ctrl+c", "ctrl+v", "ctrl+a", "alt+tab"

## ENVIRONMENT AWARENESS:
- If 'Mode' is 'VM': You're in a Linux XFCE desktop. Use TERMINAL for shell commands.
- If 'Mode' is 'Remote Desktop': You're on the user's actual OS. Be careful with destructive actions.
- If 'Target Status' is NOT 'running'/'Online', inform the user and DONE.

## PROFESSIONAL & CREATIVE SOFTWARE (CAD, Blender, DCC, IDEs):
- Treat toolbars, modifiers, and node editors as precision workflows: confirm active mode via SCREENSHOT before destructive edits
- Prefer keyboard shortcuts documented for the app when faster than mouse (Blender: G/R/S, Tab, Space)
- For complex UIs, work in small loops: SCREENSHOT → one focused action → SCREENSHOT to verify
- If viewport navigation is unclear, use middle-mouse / view menus via TERMINAL

## STOP POLICY:
- If blocked by CAPTCHA, paywall, or hard login → use HITL
- If action fails 3 times → try alternative approach or DONE with explanation
- Never enter credentials unless the user explicitly provides them

## RESPONSE FORMAT - JSON examples:

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
{"thought": "reasoning", "action": "FILE_READ", "params": {"filepath": "/home/controluser/Desktop/test.py"}}
{"thought": "reasoning", "action": "FILE_WRITE", "params": {"filepath": "/home/controluser/Desktop/test.py", "content": "print('hello')"}}
{"thought": "reasoning", "action": "DIRECTORY_LIST", "params": {"path": "/home/controluser/Desktop"}}
{"thought": "reasoning", "action": "OPEN_CODE_EDITOR", "params": {}}
{"thought": "reasoning", "action": "OPEN_TERMINAL", "params": {}}
{"thought": "reasoning", "action": "DONE", "params": {"summary": "Task completed successfully"}}
{"thought": "reasoning", "action": "HITL", "params": {"reason": "Need user credentials to proceed"}}

## OUTPUT FORMAT - JSON ONLY (no conversational text):
All parameters MUST be passed inside the "params" object:

{"thought": "reasoning", "action": "ACTION_NAME", "params": {"param_name": "param_value"}}

EXAMPLES:
- Read a file: {"thought": "Reading test.py", "action": "FILE_READ", "params": {"filepath": "/home/controluser/Desktop/test.py"}}
- Write a file: {"thought": "Creating Python file", "action": "FILE_WRITE", "params": {"filepath": "/home/controluser/Desktop/test.py", "content": "print('hello')"}}
- List directory: {"thought": "Listing Desktop", "action": "DIRECTORY_LIST", "params": {"path": "/home/controluser/Desktop"}}
- Open editor: {"thought": "Opening editor", "action": "OPEN_CODE_EDITOR", "params": {}}
- Run terminal: {"thought": "Running ls command", "action": "TERMINAL", "params": {"command": "ls -la"}}
- Click: {"thought": "Clicking button", "action": "CLICK", "params": {"x": 500, "y": 300}}
- Navigate browser: {"thought": "Opening Google", "action": "BROWSER_NAVIGATE", "params": {"url": "https://google.com"}}

If task is finished: {"thought": "reasoning", "action": "DONE", "params": {"summary": "description"}}
If stuck and need help: {"thought": "reasoning", "action": "HITL", "params": {"reason": "explanation"}}

## ENVIRONMENT:
- You're on a Linux XFCE desktop VM
- User home: /home/controluser
- Desktop files: /home/controluser/Desktop
- Available apps: Firefox, Terminal, File Manager, Code Editor (micro)
- Terminal: xfce4-terminal
- File manager: Thunar
- Code editor: micro

## QUICK REFERENCE:
- Open browser: BROWSER_OPEN → BROWSER_NAVIGATE
- Read file: FILE_READ with filepath
- Write file: FILE_WRITE with filepath and content
- List files: DIRECTORY_LIST with path
- Download folder: FILE_ZIP with path
- Open editor: OPEN_CODE_EDITOR
- Open terminal: OPEN_TERMINAL
- Open files: OPEN_FILE_MANAGER
- Run command: TERMINAL with command"""

ASK_SYSTEM_PROMPT = """You are Control AI, a highly knowledgeable assistant with access to the target machine.

The user is asking you a question - they may want you to:
1. Answer a question about their computer/files
2. Explain something about their system
3. Help them understand what they're seeing on screen

You CAN access the machine to help answer questions:
- Take SCREENSHOT to see what's on screen
- Use FILE_READ to read files they ask about
- Use DIRECTORY_LIST to show folder contents
- Use TERMINAL to run commands and show output
- Use OCR to extract text from screenshots

RULES:
1. If they ask about files/code on their machine, access it and show them
2. If they ask about the screen, take a screenshot and describe it
3. Use markdown formatting for clear responses
4. Be helpful and thorough
5. If you need to see the screen to answer, take a screenshot first
6. Don't output JSON action commands - respond in plain text

You have full access to help answer any question about the user's machine."""

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
        # Mouse actions
        "click": "click",
        "double_click": "double_click",
        "right_click": "right_click",
        "move": "move",
        "mouse_move": "move",
        "drag": "drag",
        "scroll": "scroll",
        
        # Keyboard actions
        "type": "type",
        "key": "key_press",
        "key_press": "key_press",
        "key_combo": "key_combo",
        
        # Browser actions
        "browser_open": "browser_open",
        "browser_connect": "browser_connect",
        "browser_navigate": "browser_go",
        "browser_go": "browser_go",
        "browser_get_content": "browser_get_content",
        "browser_find": "browser_find",
        "browser_get_dom": "browser_get_dom",
        "browser_get_clickables": "browser_get_clickables",
        "browser_click": "browser_click",
        "browser_type": "browser_type",
        "browser_execute": "browser_execute",
        "browser_wait": "browser_wait",
        "browser_info": "browser_info",
        "browser_state": "browser_state",
        "browser_get_context": "browser_get_context",
        "browser_tabs": "browser_tabs",
        "browser_new_tab": "browser_new_tab",
        "browser_close_tab": "browser_close_tab",
        "browser_switch_tab": "browser_switch_tab",
        
        # Terminal actions
        "terminal": "terminal",
        "terminal_execute": "terminal",
        "terminal_connect": "terminal_connect",
        "terminal_read": "terminal_read",
        "terminal_clear": "terminal_clear",
        "terminal_close": "terminal_close",
        "open_terminal": "open_terminal",
        
        # File operations
        "file_read": "file_read",
        "file_write": "file_write",
        "file_edit": "file_edit",
        "file_append": "file_append",
        "file_delete": "file_delete",
        "file_exists": "file_exists",
        "directory_list": "directory_list",
        "directory_delete": "directory_delete",
        "file_zip": "file_zip",
        "file_download": "file_download",
        
        # Window management
        "list_windows": "list_windows",
        "switch_window": "switch_to_window",
        "switch_to_window": "switch_to_window",
        "arrange_windows": "arrange_windows",
        "close_window": "close_window",
        "minimize_window": "minimize_window",
        "maximize_window": "maximize_window",
        "restore_window": "restore_window",
        "move_window": "move_window",
        
        # App launcher
        "open_code_editor": "open_code_editor",
        "open_file_manager": "open_file_manager",
        "open_application": "open_application",
        "open_terminal": "open_terminal",
        
        # Other
        "list_apps": "list_apps",
        "screenshot": "screenshot",
        "detect_elements": "detect_elements",
        "ocr": "ocr",
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

    async def _update_usage(self, db: Client, user_id: str, mode: str, tokens: int = 0, session_id: Optional[str] = None):
        """Update user usage statistics in the database."""
        try:
            logger.info(f"[USAGE] Updating usage for user_id={user_id}, mode={mode}, tokens={tokens}, session_id={session_id}")
            res = db.table("users").select("act_count, ask_count, total_token_usage, daily_token_usage, token_usage").eq("id", user_id).execute()
            if not res.data:
                logger.warning(f"[USAGE] User not found: {user_id}")
                return

            user = res.data[0]
            logger.info(f"[USAGE] Current user data: act_count={user.get('act_count')}, ask_count={user.get('ask_count')}")
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
            logger.info(f"[USAGE] Sending update to DB: {update_data}")
            db.table("users").update(update_data).eq("id", user_id).execute()
            logger.info(f"[USAGE] Successfully updated usage for user {user_id}")

# Log billing metrics to a dedicated table for reliable billing/monitoring
            # This ensures token/ask/act counts are captured even if UI caching or dashboards differ.
            if tokens:
                try:
                    await self._log_billing_metrics(db, user_id, mode, tokens, session_id=session_id)
                    logger.info(f"[USAGE] Billing metrics logged successfully")
                except Exception as log_err:
                    logger.error(f"Failed to log billing metrics: {log_err}")

        except Exception as e:
            logger.error(f"Failed to update user usage: {e}", exc_info=True)

    async def _log_billing_metrics(self, db: Client, user_id: str, mode: str, tokens: int, session_id: Optional[str] = None):
        """Persist billing metrics to a dedicated table for analytics."""
        try:
            data = {
                "user_id": user_id,
                "mode": mode,
                "tokens": int(tokens),
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            if session_id:
                data["session_id"] = session_id
            db.table("billing_metrics").insert(data).execute()
        except Exception as e:
            # Do not crash the main flow if logging fails
            logger.error(f"Error writing billing_metrics: {e}")


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
            
            # Fetch only recent context - DON'T include past user messages
            # Each new request should be treated as a new request
            # Only include action results from previous turns for context
            history_res = db.table("chat_messages").select("*").eq("session_id", session_id).order("created_at").execute()
            
            conversation = []
            
            # Only include the LAST assistant message (action result) as context
            # Do NOT include past user requests - they are independent
            assistant_context = None
            for m in history_res.data:
                if m["role"] == "assistant" and m.get("action_type"):
                    # Store latest action result
                    assistant_context = m
            
            # Add current user message
            conversation.append({"role": "user", "content": user_message})
            
            # If there's a previous action result, include it as context
            if assistant_context:
                action_type = assistant_context.get("action_type", "")
                content = assistant_context.get("content", "")
                conversation.append({
                    "role": "assistant", 
                    "content": f"[Previous action: {action_type.upper()}] {content}"
                })
            
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
                
                full_response = ""
                
                # For ASK mode, optionally allow access to target machine for questions about the system
                # Take a screenshot if available for context
                if last_screenshot:
                    pass  # Already have screenshot from auto-capture
                elif machine_id or device_id:
                    try:
                        if machine_id:
                            shot = await _take_screenshot_vm(machine_id)
                            if shot: last_screenshot = shot
                        elif device_id:
                            shot = await _take_screenshot_device(device_id)
                            if shot: last_screenshot = shot
                    except Exception as e:
                        logger.warning(f"Could not capture screenshot for ASK mode: {e}")
                
                # Streaming implementation for ASK mode
                result = await self._call_ai(provider_config, conversation, last_screenshot, stream=True)
                if isinstance(result, AsyncGenerator):
                    async for chunk in result:
                        full_response += chunk
                        yield {"type": "stream", "content": chunk}
                else:
                    full_response = str(result)
                    yield {"type": "message", "content": full_response}

                # Save to DB - for ASK mode, save without action_type (no thought process stored)
                db.table("chat_messages").insert({
                    "session_id": session_id,
                    "role": "assistant",
                    "content": full_response
                }).execute()
                logger.info(f"[EXEC] Calling _update_usage for ASK mode: user_id={user_id}, mode={mode}, tokens={tokens_per_turn}")
                await self._update_usage(db, user_id, mode, tokens=tokens_per_turn, session_id=session_id)
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
                logger.info(f"🔗 Ensuring VM agent is connected for machine {machine_id}")
                connected = await vm_service.ensure_vm_agent_connected(db, machine_id)
                if not connected:
                    logger.error(f"❌ Failed to connect to VM agent for machine {machine_id}")
                    yield {"type": "error", "content": "Failed to connect to VM agent. Please check that the VM is running and try again."}
                    yield {"type": "done"}
                    return
                logger.info(f"✅ VM agent connected successfully for machine {machine_id}")

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
                logger.info(f"[EXEC] Calling _update_usage for ACT mode: user_id={user_id}, mode=act, tokens={tokens_per_turn}")
                await self._update_usage(db, user_id, "act", tokens=tokens_per_turn, session_id=session_id)

                # Build a descriptive message for the action
                action_description = f"{action}"
                if action == "CLICK":
                    action_description = f"Click at ({params.get('x')}, {params.get('y')})"
                elif action == "TYPE":
                    text = params.get('text', '')
                    action_description = f"Type: {text[:50]}{'...' if len(text) > 50 else ''}"
                elif action == "SCREENSHOT":
                    action_description = "Take screenshot"
                elif action == "TERMINAL":
                    cmd = params.get('command', '')
                    action_description = f"Terminal: {cmd[:50]}{'...' if len(cmd) > 50 else ''}"
                elif action == "SCROLL":
                    action_description = f"Scroll {params.get('direction', 'down')}"
                elif action == "KEY_COMBO":
                    action_description = f"Key combo: {params.get('keys', '')}"
                elif action == "BROWSER_NAVIGATE":
                    action_description = f"Navigate to: {params.get('url', '')}"
                
                db.table("chat_messages").insert({
                    "session_id": session_id, "role": "assistant", "content": action_description,
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
                elif action == "OPEN_CODE_EDITOR":
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "open_code_editor", {})
                        action_result = f"Code editor opened: {json.dumps(result)}"
                    else: action_result = "Error: No target VM."
                elif action == "OPEN_FILE_MANAGER":
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "open_file_manager", {})
                        action_result = f"File manager opened: {json.dumps(result)}"
                    else: action_result = "Error: No target VM."
                elif action == "OPEN_TERMINAL":
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "open_terminal", {})
                        action_result = f"Terminal opened: {json.dumps(result)}"
                    else: action_result = "Error: No target VM."
                elif action == "FILE_READ":
                    filepath = params.get("filepath", params.get("path", ""))
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "file_read", {"filepath": filepath})
                        action_result = f"File content: {json.dumps(result)}"
                    else: action_result = "Error: No target VM."
                elif action == "FILE_WRITE":
                    filepath = params.get("filepath", params.get("path", ""))
                    content = params.get("content", "")
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "file_write", {"filepath": filepath, "content": content})
                        action_result = f"File write result: {json.dumps(result)}"
                    else: action_result = "Error: No target VM."
                elif action == "FILE_ZIP":
                    path = params.get("path", "")
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "file_zip", {"path": path})
                        action_result = f"Zip created: {json.dumps(result)}"
                    else: action_result = "Error: No target VM."
                elif action == "DIRECTORY_LIST":
                    path = params.get("path", "/home/controluser")
                    if machine_id:
                        result = await _execute_vm_action(machine_id, "directory_list", {"path": path})
                        action_result = f"Directory contents: {json.dumps(result)}"
                    else: action_result = "Error: No target VM."
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
            yield {"type": "error", "content": sanitize_error(str(e))}
            yield {"type": "done"}

agent_executor = AgentExecutor()
