"""
VM Tools - Comprehensive tool registry for VM automation.
Based on open-computer-use's chat_vm_tools.py architecture.

Phase 2: Proper tool registry with detailed schemas, ensure_connection helper,
and integration with the enhanced VMControlService.
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def create_vm_tools(machine_id: str, connection_info: Optional[Dict] = None) -> Dict:
    """
    Create comprehensive VM control tools for computer automation.
    Each tool has a detailed schema and async execution function.
    """
    tools = {}
    
    vm_info = connection_info or {}
    
    from app.services.vm_control import vm_control_service
    
    def filter_screenshot_for_model(result: Dict) -> Dict:
        """Remove frontendScreenshot from result so model doesn't see it"""
        if isinstance(result, dict) and "frontendScreenshot" in result:
            filtered = {k: v for k, v in result.items() if k != "frontendScreenshot"}
            return filtered
        return result
    
    def truncate_response(text: str, max_length: int = 5000) -> str:
        """Truncate response to prevent context overflow, preserving screenshots"""
        if len(text) <= max_length:
            return text
        return text[:max_length] + "\n... [truncated]"
    
    async def ensure_connection() -> bool:
        """Ensure we have an active connection to the VM agent"""
        if machine_id in vm_control_service.connections:
            ws = vm_control_service.connections[machine_id]
            if not ws.closed:
                return True
            del vm_control_service.connections[machine_id]
        
        if vm_info.get("agent_host"):
            logger.info(f"Establishing connection to VM {machine_id}")
            return await vm_control_service.connect(
                vm_info.get("agent_port", 8080),
                machine_id,
                vm_info.get("agent_host")
            )
        
        logger.error(f"No connection info available for machine {machine_id}")
        return False
    
    async def screenshot() -> Dict:
        """Take a screenshot of the current desktop"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        result = await vm_control_service.execute_command(machine_id, "screenshot", {})
        if result.get("success") and result.get("screenshot"):
            return {
                "success": True,
                "screenshot": result["screenshot"],
                "format": "base64",
                "resolution": result.get("resolution", "unknown")
            }
        return result
    
    tools["screenshot"] = {
        "name": "screenshot",
        "description": "Capture a screenshot of the current desktop display. Returns base64-encoded image data that can be analyzed for UI elements, text, and visual state.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        },
        "execute": screenshot
    }
    
    async def detect_elements(include_text: bool = True) -> Dict:
        """Detect UI elements on the screen"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "detect_elements", {"include_text": include_text})
    
    tools["detect_elements"] = {
        "name": "detect_elements",
        "description": "Detect interactive UI elements on the current screen. Returns list of clickable elements with their coordinates, text, and properties.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_text": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include text content in element detection"
                }
            },
            "required": [],
            "additionalProperties": False
        },
        "execute": detect_elements
    }
    
    async def browser_state() -> Dict:
        """Get comprehensive browser state including focus, cursor, scroll, and interaction state"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        result = await vm_control_service.execute_command(machine_id, "browser_state", {})
        return filter_screenshot_for_model(result)
    
    tools["browser_state"] = {
        "name": "browser_state",
        "description": "Get comprehensive browser state including: active/focused element details (where cursor/focus is), mouse position, scroll position, forms on page, interactive elements count, loading indicators, and alerts. This provides complete situational awareness of the browser's current state.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        },
        "execute": browser_state
    }
    
    async def ocr() -> Dict:
        """Extract text from screen using OCR"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "ocr", {})
    
    tools["ocr"] = {
        "name": "ocr",
        "description": "Extract readable text from the current screen using OCR (Optical Character Recognition). Useful for reading text from images or non-selectable content.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        },
        "execute": ocr
    }
    
    async def click(x: int, y: int, button: str = "left", double: bool = False) -> Dict:
        """Click at specific coordinates"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        command = "double_click" if double else "click"
        params = {"x": x, "y": y}
        if button != "left":
            params["button"] = button
        
        return await vm_control_service.execute_command(machine_id, command, params)
    
    tools["click"] = {
        "name": "click",
        "description": "Perform a mouse click at specific screen coordinates. Use detect_elements or screenshot first to identify target coordinates.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Horizontal position in pixels from left edge"},
                "y": {"type": "integer", "description": "Vertical position in pixels from top edge"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "double": {"type": "boolean", "default": False, "description": "Perform double-click"}
            },
            "required": ["x", "y"],
            "additionalProperties": False
        },
        "execute": click
    }
    
    async def type_text(text: str) -> Dict:
        """Type text at current cursor position"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "type", {"text": text})
    
    tools["type"] = {
        "name": "type",
        "description": "Type text at the current cursor position. Simulates keyboard input. Ensure target field is focused first.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text string to type"}
            },
            "required": ["text"],
            "additionalProperties": False
        },
        "execute": type_text
    }
    
    async def key_press(keys: list) -> Dict:
        """Press specific keyboard keys"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "key_press", {"keys": keys})
    
    tools["key"] = {
        "name": "key",
        "description": "Press special keyboard keys sequentially. Use for navigation (Tab, Enter, Escape) or editing.",
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Special keys to press: enter, tab, escape, backspace, delete, up, down, left, right, etc."
                }
            },
            "required": ["keys"],
            "additionalProperties": False
        },
        "execute": key_press
    }
    
    async def key_combo_tool(keys: list) -> Dict:
        """Press keyboard key combination (e.g., Ctrl+C)"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "key_combo", {"keys": keys})
    
    tools["key_combo"] = {
        "name": "key_combo",
        "description": "Press keyboard shortcuts like Ctrl+C, Ctrl+V, Alt+Tab. Use for copy, paste, and window switching.",
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key combination: ctrl, alt, shift, cmd, plus key names"
                }
            },
            "required": ["keys"],
            "additionalProperties": False
        },
        "execute": key_combo_tool
    }
    
    async def scroll(direction: str = "down", amount: int = 100) -> Dict:
        """Scroll the screen"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "scroll", {"direction": direction, "amount": amount})
    
    tools["scroll"] = {
        "name": "scroll",
        "description": "Scroll the screen up or down. Use to reveal hidden content.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                "amount": {"type": "integer", "default": 100, "description": "Scroll amount in pixels"}
            },
            "required": [],
            "additionalProperties": False
        },
        "execute": scroll
    }
    
    async def terminal_cmd(command: str) -> Dict:
        """Execute terminal command"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "terminal", {"command": command})
    
    tools["terminal"] = {
        "name": "terminal",
        "description": "Execute a terminal/shell command. Returns command output. Use for file operations, system info, and running scripts.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"}
            },
            "required": ["command"],
            "additionalProperties": False
        },
        "execute": terminal_cmd
    }
    
    async def browser_open() -> Dict:
        """Open Firefox browser"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "browser_open", {})
    
    tools["browser_open"] = {
        "name": "browser_open",
        "description": "Open the Firefox web browser.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "execute": browser_open
    }
    
    async def browser_navigate(url: str) -> Dict:
        """Navigate browser to URL"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "browser_go", {"url": url})
    
    tools["browser_navigate"] = {
        "name": "browser_navigate",
        "description": "Navigate the browser to a specific URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to navigate to"}
            },
            "required": ["url"],
            "additionalProperties": False
        },
        "execute": browser_navigate
    }
    
    async def file_read(path: str) -> Dict:
        """Read file content"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "file_read", {"filepath": path})
    
    tools["file_read"] = {
        "name": "file_read",
        "description": "Read the content of a file from the VM.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"}
            },
            "required": ["path"],
            "additionalProperties": False
        },
        "execute": file_read
    }
    
    async def file_write(path: str, content: str) -> Dict:
        """Write content to file"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "file_write", {"filepath": path, "content": content})
    
    tools["file_write"] = {
        "name": "file_write",
        "description": "Create or overwrite a file with content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "content": {"type": "string", "description": "Content to write"}
            },
            "required": ["path", "content"],
            "additionalProperties": False
        },
        "execute": file_write
    }
    
    async def directory_list(path: str = "/home/controluser") -> Dict:
        """List directory contents"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "directory_list", {"path": path})
    
    tools["directory_list"] = {
        "name": "directory_list",
        "description": "List files and directories in a folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list"}
            },
            "required": ["path"],
            "additionalProperties": False
        },
        "execute": directory_list
    }
    
    async def file_zip(path: str) -> Dict:
        """Download folder as ZIP"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "file_zip", {"path": path})
    
    tools["file_zip"] = {
        "name": "file_zip",
        "description": "Compress a folder as ZIP and return base64-encoded data for download.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path to compress"}
            },
            "required": ["path"],
            "additionalProperties": False
        },
        "execute": file_zip
    }
    
    async def list_windows() -> Dict:
        """List all open windows"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "list_windows", {})
    
    tools["list_windows"] = {
        "name": "list_windows",
        "description": "List all currently open windows on the desktop.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "execute": list_windows
    }
    
    async def close_window(window: Optional[str] = None) -> Dict:
        """Close a window"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        params: Dict[str, Any] = {}
        if window:
            params["window_title"] = window
        return await vm_control_service.execute_command(machine_id, "close_window", params)
    
    tools["close_window"] = {
        "name": "close_window",
        "description": "Close the active window or a window by title.",
        "parameters": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "Window title to close"}
            },
            "required": [],
            "additionalProperties": False
        },
        "execute": close_window
    }
    
    async def switch_window(window: str) -> Dict:
        """Switch to a window"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "switch_to_window", {"window": window})
    
    tools["switch_window"] = {
        "name": "switch_window",
        "description": "Switch focus to a window by title.",
        "parameters": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "Window title or ID"}
            },
            "required": ["window"],
            "additionalProperties": False
        },
        "execute": switch_window
    }
    
    async def open_terminal() -> Dict:
        """Open terminal application"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "open_terminal", {})
    
    tools["open_terminal"] = {
        "name": "open_terminal",
        "description": "Open the terminal application.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "execute": open_terminal
    }
    
    async def open_code_editor() -> Dict:
        """Open code editor"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "open_code_editor", {})
    
    tools["open_code_editor"] = {
        "name": "open_code_editor",
        "description": "Open the code editor (micro) for editing files.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "execute": open_code_editor
    }
    
    async def list_apps() -> Dict:
        """List available applications"""
        if not await ensure_connection():
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        return await vm_control_service.execute_command(machine_id, "list_apps", {})
    
    tools["list_apps"] = {
        "name": "list_apps",
        "description": "List all installed applications on the system.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "execute": list_apps
    }
    
    return tools


def format_tool_result(result: Dict, max_length: int = 5000) -> str:
    """Format a tool result for the AI model, truncating if necessary"""
    if not isinstance(result, dict):
        return str(result)[:max_length]
    
    filtered = {k: v for k, v in result.items() if k != "frontendScreenshot"}
    
    text = json.dumps(filtered, indent=2)
    if len(text) > max_length:
        text = text[:max_length] + "\n... [truncated]"
    
    return text


import json
