import asyncio
import base64
import json
import logging
import os
import subprocess
import time
from typing import Dict, Any, Optional
import pyautogui
from websockets.server import serve
from PIL import Image
import io

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05  # Reduce default pause for faster actions
os.environ['DISPLAY'] = ':1'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VM-Agent")

# Screen resolution (will be detected dynamically)
SCREEN_WIDTH, SCREEN_HEIGHT = 1920, 1080


class VMAgent:
    def __init__(self):
        self.terminal_process: Optional[subprocess.Popen] = None
        self.terminal_history: list = []
        self._update_screen_size()

    def _update_screen_size(self):
        global SCREEN_WIDTH, SCREEN_HEIGHT
        try:
            w, h = pyautogui.size()
            SCREEN_WIDTH, SCREEN_HEIGHT = w, h
            logger.info(f"Screen size: {SCREEN_WIDTH}x{SCREEN_HEIGHT}")
        except Exception:
            pass

    def _normalize_coords(self, x, y):
        """Convert 0-1000 normalized coords to actual screen pixels."""
        actual_x = int((x / 1000.0) * SCREEN_WIDTH)
        actual_y = int((y / 1000.0) * SCREEN_HEIGHT)
        return actual_x, actual_y

    async def handle_client(self, websocket):
        logger.info("New connection to VM Agent")
        try:
            # Wait for first message - check if it's an auth message
            first_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            data = json.loads(first_msg)

            if data.get('type') == 'auth':
                # Send auth success response
                await websocket.send(json.dumps({
                    "type": "auth_success",
                    "message": "Authenticated successfully"
                }))
                logger.info(f"Client authenticated: {data.get('userId', 'unknown')}")
            elif data.get('type') == 'command':
                # No auth, process as command directly
                result = await self._process_command(data)
                await websocket.send(json.dumps(result))
            elif data.get('type') == 'ping':
                await websocket.send(json.dumps({"type": "pong"}))

            # Main message loop
            async for message in websocket:
                try:
                    data = json.loads(message)
                    result = await self._process_command(data)
                    if result:
                        await websocket.send(json.dumps(result))
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "data": {"error": "Invalid JSON"}
                    }))
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    await websocket.send(json.dumps({
                        "type": "error",
                        "data": {"error": str(e)}
                    }))
        except asyncio.TimeoutError:
            logger.warning("Client connection timeout during handshake")
        except Exception as e:
            logger.info(f"Connection closed: {e}")

    async def _process_command(self, data: dict) -> Optional[dict]:
        msg_type = data.get('type')

        if msg_type == 'command':
            cmd_data = data.get('data', {})
            command = cmd_data.get('command')
            params = cmd_data.get('parameters', {})

            logger.info(f"Executing: {command}")
            result = await self.execute(command, params)

            return {
                "type": "result",
                "data": result
            }
        elif msg_type == 'ping':
            return {"type": "pong"}

        return None

    async def execute(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            # ════════════════════════════════════════════
            # SCREENSHOT
            # ════════════════════════════════════════════
            if command == "screenshot":
                return await self._screenshot(params)

            # ════════════════════════════════════════════
            # MOUSE ACTIONS
            # ════════════════════════════════════════════
            elif command == "click":
                return await self._click(params)
            elif command == "double_click":
                return await self._double_click(params)
            elif command == "right_click":
                return await self._right_click(params)
            elif command in ("move", "mouse_move"):
                return await self._mouse_move(params)
            elif command == "drag":
                return await self._drag(params)
            elif command == "scroll":
                return await self._scroll(params)

            # ════════════════════════════════════════════
            # KEYBOARD ACTIONS
            # ════════════════════════════════════════════
            elif command == "type":
                return await self._type_text(params)
            elif command in ("key", "key_press"):
                return await self._key_press(params)
            elif command == "key_combo":
                return await self._key_combo(params)

            # ════════════════════════════════════════════
            # BROWSER ACTIONS
            # ════════════════════════════════════════════
            elif command == "browser_navigate":
                return await self._browser_navigate(params)
            elif command == "browser_get_content":
                return await self._browser_get_content(params)
            elif command == "browser_find":
                return await self._browser_find(params)

            # ════════════════════════════════════════════
            # TERMINAL
            # ════════════════════════════════════════════
            elif command == "terminal":
                return await self._terminal(params)
            elif command == "terminal_execute":
                return await self._terminal(params)

            # ════════════════════════════════════════════
            # FILE OPERATIONS
            # ════════════════════════════════════════════
            elif command == "file_read":
                return await self._file_read(params)
            elif command == "file_write":
                return await self._file_write(params)
            elif command == "file_exists":
                return await self._file_exists(params)
            elif command == "directory_list":
                return await self._directory_list(params)

            # ════════════════════════════════════════════
            # LIST APPS
            # ════════════════════════════════════════════
            elif command == "list_apps":
                return await self._list_apps()

            else:
                return {"success": False, "error": f"Unknown command: {command}"}

        except Exception as e:
            logger.error(f"Command '{command}' failed: {e}")
            return {"success": False, "error": str(e)}

    # ── Screenshot ──────────────────────────────────────────────────────

    async def _screenshot(self, params: dict) -> dict:
        """Take a screenshot, compress to JPEG for speed."""
        try:
            shot = pyautogui.screenshot()

            # Compress to JPEG for much smaller payload
            quality = params.get("quality", 70)
            max_width = params.get("max_width", 1280)

            # Resize if needed
            if shot.width > max_width:
                ratio = max_width / shot.width
                new_h = int(shot.height * ratio)
                shot = shot.resize((max_width, new_h), Image.LANCZOS)

            img_bytes = io.BytesIO()
            shot.save(img_bytes, format='JPEG', quality=quality, optimize=True)
            encoded = base64.b64encode(img_bytes.getvalue()).decode('utf-8')

            return {
                "success": True,
                "screenshot": encoded,
                "format": "jpeg",
                "resolution": f"{shot.width}x{shot.height}"
            }
        except Exception as e:
            return {"success": False, "error": f"Screenshot failed: {e}"}

    # ── Mouse Actions ───────────────────────────────────────────────────

    async def _click(self, params: dict) -> dict:
        x_raw, y_raw = params.get('x'), params.get('y')
        if x_raw is None or y_raw is None:
            return {"success": False, "error": "Missing x,y coordinates"}

        x, y = self._normalize_coords(x_raw, y_raw)
        button = params.get('button', 'left')
        pyautogui.click(x, y, button=button)
        return {"success": True, "clicked": {"x": x, "y": y}}

    async def _double_click(self, params: dict) -> dict:
        x_raw, y_raw = params.get('x'), params.get('y')
        if x_raw is None or y_raw is None:
            return {"success": False, "error": "Missing x,y coordinates"}

        x, y = self._normalize_coords(x_raw, y_raw)
        pyautogui.doubleClick(x, y)
        return {"success": True, "double_clicked": {"x": x, "y": y}}

    async def _right_click(self, params: dict) -> dict:
        x_raw, y_raw = params.get('x'), params.get('y')
        if x_raw is None or y_raw is None:
            return {"success": False, "error": "Missing x,y coordinates"}

        x, y = self._normalize_coords(x_raw, y_raw)
        pyautogui.rightClick(x, y)
        return {"success": True, "right_clicked": {"x": x, "y": y}}

    async def _mouse_move(self, params: dict) -> dict:
        x_raw, y_raw = params.get('x'), params.get('y')
        if x_raw is None or y_raw is None:
            return {"success": False, "error": "Missing x,y coordinates"}

        x, y = self._normalize_coords(x_raw, y_raw)
        pyautogui.moveTo(x, y)
        return {"success": True, "moved_to": {"x": x, "y": y}}

    async def _drag(self, params: dict) -> dict:
        from_x_raw = params.get('from_x', params.get('x'))
        from_y_raw = params.get('from_y', params.get('y'))
        to_x_raw = params.get('to_x')
        to_y_raw = params.get('to_y')

        if None in (from_x_raw, from_y_raw, to_x_raw, to_y_raw):
            return {"success": False, "error": "Missing drag coordinates (from_x, from_y, to_x, to_y)"}

        fx, fy = self._normalize_coords(from_x_raw, from_y_raw)
        tx, ty = self._normalize_coords(to_x_raw, to_y_raw)

        pyautogui.moveTo(fx, fy)
        pyautogui.mouseDown()
        pyautogui.moveTo(tx, ty, duration=0.3)
        pyautogui.mouseUp()
        return {"success": True, "dragged": {"from": [fx, fy], "to": [tx, ty]}}

    async def _scroll(self, params: dict) -> dict:
        direction = params.get('direction', 'down')
        amount = params.get('amount', 5)

        clicks = -abs(amount) if direction == 'down' else abs(amount)
        pyautogui.scroll(clicks)
        return {"success": True, "scrolled": {"direction": direction, "amount": amount}}

    # ── Keyboard Actions ────────────────────────────────────────────────

    async def _type_text(self, params: dict) -> dict:
        text = params.get('text', '')
        if not text:
            return {"success": False, "error": "No text provided"}

        # Use xdotool for more reliable typing (handles special chars)
        try:
            subprocess.run(
                ['xdotool', 'type', '--clearmodifiers', '--', text],
                timeout=10, check=False,
                env={**os.environ, 'DISPLAY': ':1'}
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Fallback to pyautogui
            pyautogui.write(text, interval=0.02)

        return {"success": True, "typed": len(text)}

    async def _key_press(self, params: dict) -> dict:
        """Press one or more keys sequentially."""
        keys = params.get('keys', [])
        key = params.get('key', '')

        # Support both single key and key list
        if key and not keys:
            # Handle combo keys like "ctrl+c"
            if '+' in key:
                parts = [k.strip().lower() for k in key.split('+')]
                return await self._key_combo({"keys": parts})
            keys = [key]

        KEY_MAP = {
            'enter': 'Return', 'return': 'Return',
            'tab': 'Tab', 'escape': 'Escape', 'esc': 'Escape',
            'backspace': 'BackSpace', 'delete': 'Delete',
            'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
            'home': 'Home', 'end': 'End',
            'pageup': 'Page_Up', 'pagedown': 'Page_Down',
            'space': 'space',
            'f1': 'F1', 'f2': 'F2', 'f3': 'F3', 'f4': 'F4',
            'f5': 'F5', 'f6': 'F6', 'f7': 'F7', 'f8': 'F8',
            'f9': 'F9', 'f10': 'F10', 'f11': 'F11', 'f12': 'F12',
        }

        for k in keys:
            mapped = KEY_MAP.get(k.lower(), k)
            try:
                pyautogui.press(mapped)
            except Exception:
                # Fallback with xdotool
                subprocess.run(
                    ['xdotool', 'key', mapped],
                    timeout=5, check=False,
                    env={**os.environ, 'DISPLAY': ':1'}
                )

        return {"success": True, "pressed": keys}

    async def _key_combo(self, params: dict) -> dict:
        """Press key combination (e.g., ctrl+c, alt+f4)."""
        keys = params.get('keys', [])
        if not keys:
            return {"success": False, "error": "No keys provided"}

        MODIFIER_MAP = {
            'ctrl': 'ctrl', 'control': 'ctrl',
            'alt': 'alt', 'option': 'alt',
            'shift': 'shift',
            'cmd': 'super', 'command': 'super',
            'win': 'super', 'super': 'super', 'meta': 'super',
        }

        KEY_MAP = {
            'enter': 'Return', 'return': 'Return',
            'tab': 'Tab', 'escape': 'Escape', 'esc': 'Escape',
            'backspace': 'BackSpace', 'delete': 'Delete',
            'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
            'space': 'space',
        }

        mapped_keys = []
        for k in keys:
            k_lower = k.lower()
            if k_lower in MODIFIER_MAP:
                mapped_keys.append(MODIFIER_MAP[k_lower])
            elif k_lower in KEY_MAP:
                mapped_keys.append(KEY_MAP[k_lower])
            else:
                mapped_keys.append(k)

        try:
            pyautogui.hotkey(*mapped_keys)
        except Exception:
            # Fallback: use xdotool
            combo = '+'.join(mapped_keys)
            subprocess.run(
                ['xdotool', 'key', combo],
                timeout=5, check=False,
                env={**os.environ, 'DISPLAY': ':1'}
            )

        return {"success": True, "combo": keys}

    # ── Browser Actions ─────────────────────────────────────────────────

    async def _browser_navigate(self, params: dict) -> dict:
        url = params.get('url', '')
        if not url:
            return {"success": False, "error": "No URL provided"}

        try:
            # Try to open in existing Firefox, or start new one
            subprocess.Popen(
                ['firefox', '--new-tab', url],
                env={**os.environ, 'DISPLAY': ':1'},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            await asyncio.sleep(2)  # Wait for browser to load
            return {"success": True, "navigated_to": url}
        except Exception as e:
            return {"success": False, "error": f"Browser navigation failed: {e}"}

    async def _browser_get_content(self, params: dict) -> dict:
        url = params.get('url', '')
        if not url:
            return {"success": False, "error": "No URL provided"}

        try:
            import urllib.request
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode('utf-8', errors='ignore')

            # Simple text extraction
            import re
            # Remove script/style
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
            # Remove tags
            text = re.sub(r'<[^>]+>', ' ', html)
            # Clean whitespace
            text = re.sub(r'\s+', ' ', text).strip()
            return {"success": True, "content": text[:5000], "url": url}
        except Exception as e:
            return {"success": False, "error": f"Content fetch failed: {e}"}

    async def _browser_find(self, params: dict) -> dict:
        query = params.get('query', '')
        if not query:
            return {"success": False, "error": "No query provided"}

        # Use Ctrl+F to find text on page
        pyautogui.hotkey('ctrl', 'f')
        await asyncio.sleep(0.5)
        pyautogui.write(query, interval=0.02)
        await asyncio.sleep(0.3)
        return {"success": True, "searched_for": query}

    # ── Terminal ────────────────────────────────────────────────────────

    async def _terminal(self, params: dict) -> dict:
        command = params.get('command', '')
        if not command:
            return {"success": False, "error": "No command provided"}

        timeout = params.get('timeout', 30)

        try:
            result = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, 'DISPLAY': ':1'},
                    cwd='/home/controluser'
                ),
                timeout=5
            )

            stdout, stderr = await asyncio.wait_for(
                result.communicate(),
                timeout=timeout
            )

            output = stdout.decode('utf-8', errors='replace')
            error_output = stderr.decode('utf-8', errors='replace')

            # Truncate long outputs
            if len(output) > 5000:
                output = output[:5000] + "\n...[truncated]"
            if len(error_output) > 2000:
                error_output = error_output[:2000] + "\n...[truncated]"

            self.terminal_history.append({
                "command": command,
                "output": output,
                "exit_code": result.returncode
            })

            return {
                "success": True,
                "output": output,
                "error": error_output,
                "exit_code": result.returncode
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"success": False, "error": f"Terminal error: {e}"}

    # ── File Operations ─────────────────────────────────────────────────

    async def _file_read(self, params: dict) -> dict:
        filepath = params.get('filepath', params.get('path', ''))
        if not filepath:
            return {"success": False, "error": "No filepath provided"}

        if not os.path.isabs(filepath):
            filepath = os.path.join('/home/controluser/Desktop', filepath)

        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            if len(content) > 10000:
                content = content[:10000] + "\n...[truncated]"
            return {"success": True, "content": content, "filepath": filepath}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _file_write(self, params: dict) -> dict:
        filepath = params.get('filepath', params.get('path', ''))
        content = params.get('content', '')
        if not filepath:
            return {"success": False, "error": "No filepath provided"}

        if not os.path.isabs(filepath):
            filepath = os.path.join('/home/controluser/Desktop', filepath)

        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            return {"success": True, "filepath": filepath, "size": len(content)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _file_exists(self, params: dict) -> dict:
        filepath = params.get('filepath', params.get('path', ''))
        if not os.path.isabs(filepath):
            filepath = os.path.join('/home/controluser/Desktop', filepath)
        return {"success": True, "exists": os.path.exists(filepath), "filepath": filepath}

    async def _directory_list(self, params: dict) -> dict:
        dirpath = params.get('dirpath', params.get('path', '/home/controluser/Desktop'))
        if not os.path.isabs(dirpath):
            dirpath = os.path.join('/home/controluser/Desktop', dirpath)

        try:
            entries = []
            for entry in os.listdir(dirpath):
                full = os.path.join(dirpath, entry)
                entries.append({
                    "name": entry,
                    "is_dir": os.path.isdir(full),
                    "size": os.path.getsize(full) if os.path.isfile(full) else 0
                })
            return {"success": True, "entries": entries, "path": dirpath}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── List Apps ───────────────────────────────────────────────────────

    async def _list_apps(self) -> dict:
        try:
            result = subprocess.run(
                "find /usr/share/applications /home/controluser/.local/share/applications "
                "-name '*.desktop' -exec grep -l 'Exec=' {} + 2>/dev/null | "
                "xargs grep -h '^Name=' 2>/dev/null | cut -d'=' -f2 | sort -u",
                shell=True, capture_output=True, text=True, timeout=10
            )
            apps = [a.strip() for a in result.stdout.split('\n') if a.strip()]
            return {"success": True, "apps": apps}
        except Exception as e:
            return {"success": False, "error": str(e)}


async def main():
    agent = VMAgent()
    logger.info("Starting VM Agent on port 8080...")
    async with serve(
        agent.handle_client,
        "0.0.0.0",
        8080,
        max_size=50 * 1024 * 1024,  # 50MB max message
        ping_interval=20,
        ping_timeout=10,
    ):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
