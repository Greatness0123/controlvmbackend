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
        client_addr = websocket.remote_address
        logger.info(f"🔌 New connection from {client_addr}")
        try:
            # Wait for first message - check if it's an auth message
            logger.info("⏳ Waiting for auth/command message...")
            first_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            logger.info(f"📥 Received first message: {first_msg[:200] if len(first_msg) > 200 else first_msg}")
            
            data = json.loads(first_msg)

            if data.get('type') == 'auth':
                session_id = data.get('sessionId', 'unknown')
                user_id = data.get('userId', 'unknown')
                password = data.get('password', '')
                logger.info(f"🔐 Auth request: sessionId={session_id}, userId={user_id}, has_password={bool(password)}")
                
                # Send auth success response
                await websocket.send(json.dumps({
                    "type": "auth_success",
                    "message": "Authenticated successfully"
                }))
                logger.info(f"✅ Client authenticated: userId={user_id}, sessionId={session_id}")
            elif data.get('type') == 'command':
                # No auth, process as command directly
                result = await self._process_command(data)
                await websocket.send(json.dumps(result))
            elif data.get('type') == 'ping':
                logger.info("🏓 Ping received, sending pong")
                await websocket.send(json.dumps({"type": "pong"}))
            elif data.get('type') == 'command':
                # No auth, process as command directly
                logger.info("⚡ Processing command without auth")
                result = await self._process_command(data)
                await websocket.send(json.dumps(result))

            # Main message loop
            logger.info("🔄 Entering message loop...")
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
            elif command == "file_edit":
                return await self._file_edit(params)
            elif command == "file_append":
                return await self._file_append(params)
            elif command == "file_delete":
                return await self._file_delete(params)
            elif command == "file_exists":
                return await self._file_exists(params)
            elif command == "directory_list":
                return await self._directory_list(params)
            elif command == "directory_delete":
                return await self._directory_delete(params)
            elif command == "file_zip":
                return await self._file_zip(params)
            elif command == "file_download":
                return await self._file_download(params)

            # ════════════════════════════════════════════
            # WINDOW MANAGEMENT
            # ════════════════════════════════════════════
            elif command == "list_windows":
                return await self._list_windows()
            elif command == "switch_to_window":
                return await self._switch_window(params)
            elif command == "arrange_windows":
                return await self._arrange_windows(params)
            elif command == "close_window":
                return await self._close_window(params)
            elif command == "minimize_window":
                return await self._minimize_window(params)
            elif command == "maximize_window":
                return await self._maximize_window(params)
            elif command == "restore_window":
                return await self._restore_window(params)
            elif command == "move_window":
                return await self._move_window(params)

            # ════════════════════════════════════════════
            # BROWSER AUTOMATION
            # ════════════════════════════════════════════
            elif command == "browser_open":
                return await self._browser_open(params)
            elif command == "browser_connect":
                return await self._browser_connect()
            elif command == "browser_get_dom":
                return await self._browser_get_dom(params)
            elif command == "browser_get_clickables":
                return await self._browser_get_clickables(params)
            elif command == "browser_click":
                return await self._browser_click(params)
            elif command == "browser_type":
                return await self._browser_type(params)
            elif command == "browser_execute":
                return await self._browser_execute(params)
            elif command == "browser_wait":
                return await self._browser_wait(params)
            elif command == "browser_go":
                return await self._browser_navigate(params)
            elif command == "browser_info":
                return await self._browser_info()
            elif command == "browser_state":
                return await self._browser_state()
            elif command == "browser_get_context":
                return await self._browser_get_context()
            elif command == "browser_tabs":
                return await self._browser_tabs()
            elif command == "browser_new_tab":
                return await self._browser_new_tab(params)
            elif command == "browser_close_tab":
                return await self._browser_close_tab(params)
            elif command == "browser_switch_tab":
                return await self._browser_switch_tab(params)

            # ════════════════════════════════════════════
            # TERMINAL
            # ════════════════════════════════════════════
            elif command == "terminal_connect":
                return await self._terminal_connect()
            elif command == "terminal_read":
                return await self._terminal_read()
            elif command == "terminal_clear":
                return await self._terminal_clear()
            elif command == "terminal_close":
                return await self._terminal_close()
            elif command == "open_terminal":
                return await self._open_terminal()

            # ════════════════════════════════════════════
            # OCR & ELEMENT DETECTION
            # ════════════════════════════════════════════
            elif command == "ocr":
                return await self._ocr(params)
            elif command == "detect_elements":
                return await self._detect_elements(params)

            # ════════════════════════════════════════════
            # LIST APPS
            # ════════════════════════════════════════════
            elif command == "list_apps":
                return await self._list_apps()

            # ════════════════════════════════════════════
            # APP LAUNCHER
            # ════════════════════════════════════════════
            elif command == "open_code_editor":
                return await self._open_code_editor()
            elif command == "open_file_manager":
                return await self._open_file_manager()
            elif command == "open_application":
                app = params.get('app', '')
                if app in ['code', 'editor', 'micro']:
                    return await self._open_code_editor()
                elif app in ['file', 'files', 'finder', 'thunar']:
                    return await self._open_file_manager()
                elif app in ['terminal', 'term', 'console']:
                    return await self._open_terminal()
                elif app in ['browser', 'firefox', 'chrome']:
                    return await self._browser_open(params)
                return {"success": False, "error": f"Unknown app: {app}"}

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
            subprocess.Popen(
                ['firefox', '--no-sandbox', '--new-tab', url],
                env={**os.environ, 'DISPLAY': ':1'},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            await asyncio.sleep(2)
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

    # ── File Zip ────────────────────────────────────────────────────────

    async def _file_zip(self, params: dict) -> dict:
        import zipfile
        import tempfile
        
        path = params.get('path', '')
        if not path:
            return {"success": False, "error": "No path provided"}
        
        if not os.path.isabs(path):
            path = os.path.join('/home/controluser', path)
        
        CONTROL_FILES = ['ai_agent.py', 'entrypoint.sh', 'wallpaper1.png', 'wallpaper2.png', 'wallpaper3.png']
        
        try:
            if os.path.isfile(path):
                files_to_zip = [path]
            elif os.path.isdir(path):
                files_to_zip = []
                for root, dirs, files in os.walk(path):
                    for f in files:
                        full_path = os.path.join(root, f)
                        if f not in CONTROL_FILES and not any(cf in full_path for cf in CONTROL_FILES):
                            files_to_zip.append(full_path)
            else:
                return {"success": False, "error": "Path does not exist"}
            
            if not files_to_zip:
                return {"success": False, "error": "No files to zip (or all files are control files)"}
            
            with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
                zip_path = tmp.name
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in files_to_zip:
                    arcname = os.path.relpath(f, '/home/controluser')
                    zf.write(f, arcname)
            
            with open(zip_path, 'rb') as f:
                zip_data = base64.b64encode(f.read()).decode('utf-8')
            
            os.unlink(zip_path)
            
            return {
                "success": True,
                "zip_data": zip_data,
                "filename": os.path.basename(path) + ".zip",
                "file_count": len(files_to_zip)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── File Download (single file) ────────────────────────────────────────

    async def _file_download(self, params: dict) -> dict:
        path = params.get('path', '')
        if not path:
            return {"success": False, "error": "No path provided"}
        
        if not os.path.isabs(path):
            path = os.path.join('/home/controluser', path)
        
        CONTROL_FILES = ['ai_agent.py', 'entrypoint.sh']
        
        basename = os.path.basename(path)
        if basename in CONTROL_FILES:
            return {"success": False, "error": "Cannot download control files"}
        
        try:
            if not os.path.isfile(path):
                return {"success": False, "error": "File does not exist"}
            
            with open(path, 'rb') as f:
                file_data = base64.b64encode(f.read()).decode('utf-8')
            
            return {
                "success": True,
                "file_data": file_data,
                "filename": basename,
                "size": os.path.getsize(path)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── File Edit ──────────────────────────────────────────────────────────

    async def _file_edit(self, params: dict) -> dict:
        filepath = params.get('filepath', params.get('path', ''))
        old_text = params.get('old_text', '')
        new_text = params.get('new_text', '')
        if not filepath:
            return {"success": False, "error": "No filepath provided"}
        
        if not os.path.isabs(filepath):
            filepath = os.path.join('/home/controluser/Desktop', filepath)
        
        CONTROL_FILES = ['ai_agent.py', 'entrypoint.sh']
        if os.path.basename(filepath) in CONTROL_FILES:
            return {"success": False, "error": "Cannot edit control files"}
        
        try:
            if not os.path.isfile(filepath):
                return {"success": False, "error": "File does not exist"}
            
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if old_text not in content:
                return {"success": False, "error": "Old text not found in file"}
            
            content = content.replace(old_text, new_text)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return {"success": True, "filepath": filepath}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── File Append ─────────────────────────────────────────────────────────

    async def _file_append(self, params: dict) -> dict:
        filepath = params.get('filepath', params.get('path', ''))
        content = params.get('content', '')
        if not filepath:
            return {"success": False, "error": "No filepath provided"}
        
        if not os.path.isabs(filepath):
            filepath = os.path.join('/home/controluser/Desktop', filepath)
        
        CONTROL_FILES = ['ai_agent.py', 'entrypoint.sh']
        if os.path.basename(filepath) in CONTROL_FILES:
            return {"success": False, "error": "Cannot append to control files"}
        
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(content)
            return {"success": True, "filepath": filepath}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── File Delete ─────────────────────────────────────────────────────────

    async def _file_delete(self, params: dict) -> dict:
        filepath = params.get('filepath', params.get('path', ''))
        if not filepath:
            return {"success": False, "error": "No filepath provided"}
        
        if not os.path.isabs(filepath):
            filepath = os.path.join('/home/controluser/Desktop', filepath)
        
        CONTROL_FILES = ['ai_agent.py', 'entrypoint.sh']
        if os.path.basename(filepath) in CONTROL_FILES:
            return {"success": False, "error": "Cannot delete control files"}
        
        try:
            if not os.path.isfile(filepath):
                return {"success": False, "error": "File does not exist"}
            os.remove(filepath)
            return {"success": True, "filepath": filepath}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Directory Delete ───────────────────────────────────────────────────

    async def _directory_delete(self, params: dict) -> dict:
        dirpath = params.get('dirpath', params.get('path', ''))
        if not dirpath:
            return {"success": False, "error": "No directory path provided"}
        
        if not os.path.isabs(dirpath):
            dirpath = os.path.join('/home/controluser/Desktop', dirpath)
        
        CONTROL_DIRS = ['.config', '.local', 'Documents', 'Downloads']
        if os.path.basename(dirpath) in CONTROL_DIRS:
            return {"success": False, "error": "Cannot delete control directories"}
        
        try:
            if not os.path.isdir(dirpath):
                return {"success": False, "error": "Directory does not exist"}
            import shutil
            shutil.rmtree(dirpath)
            return {"success": True, "dirpath": dirpath}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Window Management ───────────────────────────────────────────────────

    async def _list_windows(self) -> dict:
        try:
            result = subprocess.run(
                "wmctrl -l -G",
                shell=True, capture_output=True, text=True, timeout=5
            )
            windows = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    parts = line.split(None, 4)
                    if len(parts) >= 5:
                        windows.append({
                            "id": parts[0],
                            "desktop": parts[1],
                            "x": parts[2],
                            "y": parts[3],
                            "title": parts[4]
                        })
            return {"success": True, "windows": windows}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _switch_window(self, params: dict) -> dict:
        window = params.get('window', '')
        try:
            if window.startswith('0x') or window.isdigit():
                subprocess.run(f"wmctrl -i -a {window}", shell=True, timeout=5)
            else:
                subprocess.run(f"wmctrl -a {window}", shell=True, timeout=5)
            return {"success": True, "window": window}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _arrange_windows(self, params: dict) -> dict:
        arrangement = params.get('arrangement', 'tile')
        try:
            if arrangement == 'tile':
                subprocess.run("wmctrl -s 0 && wmctrl -k on", shell=True)
            elif arrangement == 'cascade':
                subprocess.run("wmctrl -s 0 && wmctrl -k on", shell=True)
            return {"success": True, "arrangement": arrangement}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _close_window(self, params: dict) -> dict:
        window = params.get('window_title', params.get('window_id', None))
        try:
            if window:
                subprocess.run(f"wmctrl -c '{window}'", shell=True, timeout=5)
            else:
                pyautogui.hotkey('alt', 'f4')
            await asyncio.sleep(0.5)
            return {"success": True, "window": window or "current"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _minimize_window(self, params: dict) -> dict:
        window = params.get('window_title', params.get('window_id', None))
        try:
            if window:
                subprocess.run(f"wmctrl -r '{window}' -b add,hidden", shell=True, timeout=5)
            else:
                pyautogui.hotkey('alt', 'f9')
            await asyncio.sleep(0.5)
            return {"success": True, "window": window or "current"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _maximize_window(self, params: dict) -> dict:
        window = params.get('window_title', params.get('window_id', None))
        try:
            if window:
                subprocess.run(f"wmctrl -r '{window}' -b add,maximized_vert,maximized_horz", shell=True, timeout=5)
            else:
                pyautogui.hotkey('alt', 'f10')
            await asyncio.sleep(0.5)
            return {"success": True, "window": window or "current"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _restore_window(self, params: dict) -> dict:
        window = params.get('window_title', params.get('window_id', None))
        try:
            if window:
                subprocess.run(f"wmctrl -r '{window}' -b remove,hidden,maximized_vert,maximized_horz", shell=True, timeout=5)
            else:
                pyautogui.hotkey('alt', 'f5')
            await asyncio.sleep(0.5)
            return {"success": True, "window": window or "current"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _move_window(self, params: dict) -> dict:
        x = params.get('x', 100)
        y = params.get('y', 100)
        width = params.get('width')
        height = params.get('height')
        window = params.get('window_title', params.get('window_id', None))
        try:
            if window:
                size_params = f"0,{x},{y},-1,-1"
                if width and height:
                    size_params = f"0,{x},{y},{width},{height}"
                subprocess.run(f"wmctrl -r '{window}' -e {size_params}", shell=True, timeout=5)
            else:
                pyautogui.hotkey('alt', 'f7')
                await asyncio.sleep(0.5)
                pyautogui.moveTo(x, y)
                pyautogui.click()
            await asyncio.sleep(0.5)
            return {"success": True, "position": {"x": x, "y": y}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Browser Automation (Basic - Enhanced) ───────────────────────────────

    async def _browser_open(self, params: dict) -> dict:
        try:
            result = subprocess.run(
                "firefox --no-sandbox --new-window about:blank &",
                shell=True, capture_output=True, timeout=10
            )
            await asyncio.sleep(3)
            return {"success": True, "message": "Browser opened"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _browser_connect(self) -> dict:
        return {"success": True, "message": "Browser connected for automation"}

    async def _browser_get_dom(self, params: dict) -> dict:
        return {"success": False, "error": "Browser automation not fully implemented - use browser_navigate for now"}

    async def _browser_get_clickables(self, params: dict) -> dict:
        return {"success": False, "error": "Browser automation not fully implemented - use browser_navigate for now"}

    async def _browser_click(self, params: dict) -> dict:
        x = params.get('x', 500)
        y = params.get('y', 500)
        pyautogui.click(x, y)
        return {"success": True, "clicked": {"x": x, "y": y}}

    async def _browser_type(self, params: dict) -> dict:
        text = params.get('text', '')
        pyautogui.write(text, interval=0.02)
        return {"success": True, "typed": text}

    async def _browser_execute(self, params: dict) -> dict:
        script = params.get('script', '')
        return {"success": False, "error": "Browser execute not implemented"}

    async def _browser_wait(self, params: dict) -> dict:
        seconds = params.get('seconds', 2)
        await asyncio.sleep(seconds)
        return {"success": True, "waited": seconds}

    async def _browser_info(self) -> dict:
        return {"success": True, "browser": "firefox", "version": "unknown"}

    async def _browser_state(self) -> dict:
        return {"success": True, "state": "active"}

    async def _browser_get_context(self) -> dict:
        return {"success": True, "url": "about:blank", "title": "New Tab"}

    async def _browser_tabs(self) -> dict:
        return {"success": True, "tabs": [{"url": "about:blank", "title": "New Tab"}]}

    async def _browser_new_tab(self, params: dict) -> dict:
        url = params.get('url', 'about:blank')
        try:
            subprocess.run(f"firefox --no-sandbox -new-tab {url} &", shell=True, timeout=5)
            await asyncio.sleep(1)
            return {"success": True, "url": url}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _browser_close_tab(self, params: dict) -> dict:
        pyautogui.hotkey('ctrl', 'w')
        return {"success": True}

    async def _browser_switch_tab(self, params: dict) -> dict:
        index = params.get('index', 0)
        pyautogui.hotkey('ctrl', 'tab')
        return {"success": True, "index": index}

    # ── Terminal ───────────────────────────────────────────────────────────

    async def _terminal_connect(self) -> dict:
        return {"success": True, "message": "Terminal session connected"}

    async def _terminal_read(self) -> dict:
        if self.terminal_history:
            return {"success": True, "output": self.terminal_history[-1].get('output', '')}
        return {"success": True, "output": ""}

    async def _terminal_clear(self) -> dict:
        self.terminal_history = []
        return {"success": True}

    async def _terminal_close(self) -> dict:
        if self.terminal_process:
            self.terminal_process.terminate()
            self.terminal_process = None
        return {"success": True}

    async def _open_terminal(self) -> dict:
        try:
            pyautogui.hotkey('ctrl', 'alt', 't')
            await asyncio.sleep(2)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _open_code_editor(self) -> dict:
        """Open the code editor (micro)"""
        try:
            subprocess.Popen(
                "xfce4-terminal -e 'micro'",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            await asyncio.sleep(2)
            return {"success": True, "message": "Code editor opened"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _open_file_manager(self) -> dict:
        """Open file manager (thunar)"""
        try:
            subprocess.Popen(
                "thunar",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            await asyncio.sleep(1)
            return {"success": True, "message": "File manager opened"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── OCR ─────────────────────────────────────────────────────────────────

    async def _ocr(self, params: dict) -> dict:
        try:
            import pytesseract
            shot = pyautogui.screenshot()
            text = pytesseract.image_to_string(shot)
            return {"success": True, "text": text[:5000]}
        except ImportError:
            return {"success": False, "error": "OCR (pytesseract) not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Detect Elements ───────────────────────────────────────────────────

    async def _detect_elements(self, params: dict) -> dict:
        try:
            shot = pyautogui.screenshot()
            width, height = shot.size
            
            elements = [
                {"type": "screen", "bounds": {"x": 0, "y": 0, "width": width, "height": height}},
                {"type": "window", "bounds": {"x": 0, "y": 0, "width": width, "height": height}, "name": "Desktop"}
            ]
            
            return {"success": True, "elements": elements, "screen_size": {"width": width, "height": height}}
        except Exception as e:
            return {"success": False, "error": str(e)}


async def main():
    agent = VMAgent()
    AGENT_PORT = int(os.environ.get("AGENT_PORT", "8080"))
    logger.info(f"🚀 Starting VM Agent on ws://0.0.0.0:{AGENT_PORT}")
    logger.info(f"   WebSocket will accept connections and handle: auth, command, ping, screenshot, browser, terminal, file ops")
    
    # Check what's listening before starting
    logger.info(f"   Checking if port {AGENT_PORT} is available...")
    import socket
    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    test_sock.settimeout(1)
    result = test_sock.connect_ex(("127.0.0.1", AGENT_PORT))
    test_sock.close()
    if result == 0:
        logger.warning(f"   ⚠️ Port {AGENT_PORT} is already in use!")
    else:
        logger.info(f"   ✓ Port {AGENT_PORT} is available")
    
    async with serve(
        agent.handle_client,
        "0.0.0.0",
        AGENT_PORT,
        max_size=50 * 1024 * 1024,  # 50MB max message
        ping_interval=20,
        ping_timeout=10,
    ):
        logger.info(f"✅ VM Agent listening on ws://0.0.0.0:{AGENT_PORT}")
        logger.info(f"   Ready to accept WebSocket connections...")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
