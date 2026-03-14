import asyncio
import base64
import json
import logging
import os
import time
from typing import Dict, Any
import pyautogui
from websockets.server import serve
from PIL import Image
import io

# Disable failsafe for container environment
pyautogui.FAILSAFE = False
os.environ['DISPLAY'] = ':1'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VM-Agent")

class VMAgent:
    async def handle_client(self, websocket):
        logger.info("New connection to VM Agent")
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    msg_type = data.get('type')
                    
                    if msg_type == 'command':
                        cmd_data = data.get('data', {})
                        command = cmd_data.get('command')
                        params = cmd_data.get('parameters', {})
                        
                        logger.info(f"Executing: {command}")
                        result = await self.execute(command, params)
                        
                        await websocket.send(json.dumps({
                            "type": "result",
                            "data": result
                        }))
                    elif msg_type == 'ping':
                        await websocket.send(json.dumps({"type": "pong"}))
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    await websocket.send(json.dumps({
                        "type": "error",
                        "data": {"error": str(e)}
                    }))
        except Exception as e:
            logger.info(f"Connection closed: {e}")

    async def execute(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if command == "screenshot":
                shot = pyautogui.screenshot()
                img_byte_arr = io.BytesIO()
                shot.save(img_byte_arr, format='PNG')
                encoded = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
                return {"success": True, "screenshot": encoded}

            elif command == "click":
                x, y = params.get('x'), params.get('y')
                pyautogui.click(x, y)
                return {"success": True}

            elif command == "type":
                text = params.get('text', '')
                pyautogui.write(text)
                return {"success": True}

            elif command == "key":
                key = params.get('key', '')
                pyautogui.press(key)
                return {"success": True}

            elif command == "scroll":
                direction = params.get('direction', 'down')
                amount = 10 if direction == 'down' else -10
                pyautogui.scroll(amount)
                return {"success": True}

            return {"success": False, "error": f"Unknown command: {command}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

async def main():
    agent = VMAgent()
    logger.info("Starting VM Agent on port 8080...")
    async with serve(agent.handle_client, "0.0.0.0", 8080):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
