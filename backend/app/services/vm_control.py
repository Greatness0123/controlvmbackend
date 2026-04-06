"""
VM Control Service - Persistent WebSocket connection manager for VM agents.
Inspired by open-computer-use's VMControlService architecture.

Maintains persistent connections with heartbeat, auto-reconnect, and
command serialization to prevent concurrent WebSocket operations.
"""

import json
import logging
import asyncio
import time
import os
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# When running inside Docker, localhost is the container's own loopback.
# VM container ports are mapped to the HOST machine.
# Use localhost for local dev, 172.17.0.1 for containerized backend, or the configured DOCKER_HOST_IP
DOCKER_HOST_IP = os.getenv("DOCKER_HOST_IP", os.getenv("DOCKER_HOST_IP", "") or "172.17.0.1")


class VMControlService:
    """Persistent WebSocket connection manager for VM agents."""

    def __init__(self):
        self.connections: Dict[str, Any] = {}           # machine_id -> websocket
        self.session_data: Dict[str, Dict] = {}         # machine_id -> connection info
        self.connection_locks: Dict[str, asyncio.Lock] = {}
        self.command_locks: Dict[str, asyncio.Lock] = {}
        self.heartbeat_tasks: Dict[str, asyncio.Task] = {}
        self.reconnect_attempts: Dict[str, int] = {}
        self.last_successful_command: Dict[str, float] = {}
        self.max_reconnect_attempts = 5
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 15
        self.heartbeat_interval = 30
        self.command_timeout = {
            "screenshot": 15.0,
            "terminal": 60.0,
            "terminal_execute": 60.0,
            "browser_navigate": 30.0,
            "browser_get_content": 30.0,
            "list_apps": 15.0,
            "default": 20.0,
        }

    async def connect(
        self,
        agent_port: int,
        machine_id: Optional[str] = None,
        host: Optional[str] = None,
    ) -> bool:
        """Establish persistent WebSocket connection to a VM agent."""
        import websockets

        if not machine_id:
            machine_id = f"vm_{agent_port}"

        target_host = host or DOCKER_HOST_IP

        if machine_id not in self.connection_locks:
            self.connection_locks[machine_id] = asyncio.Lock()

        async with self.connection_locks[machine_id]:
            # Reuse existing open connection
            if machine_id in self.connections:
                ws = self.connections[machine_id]
                try:
                    if not ws.closed:
                        logger.debug(f"Reusing persistent connection for {machine_id}")
                        return True
                except Exception:
                    pass
                # Connection dead, clean up
                await self._cleanup(machine_id)

            self.reconnect_attempts[machine_id] = 0

            while self.reconnect_attempts[machine_id] < self.max_reconnect_attempts:
                try:
                    agent_url = f"ws://{target_host}:{agent_port}"
                    logger.info(
                        f"🔌 Connecting to {agent_url} "
                        f"(attempt {self.reconnect_attempts[machine_id] + 1})"
                    )

                    websocket = await asyncio.wait_for(
                        websockets.connect(
                            agent_url,
                            ping_interval=20,
                            ping_timeout=10,
                            close_timeout=10,
                            max_size=50 * 1024 * 1024,
                            compression=None,  # Lower latency
                        ),
                        timeout=15,
                    )

                    # Store connection
                    self.connections[machine_id] = websocket

                    # Send auth message
                    auth_msg = {
                        "type": "auth",
                        "sessionId": f"backend_{int(time.time())}",
                        "userId": "backend_agent",
                        "password": "",
                    }
                    await websocket.send(json.dumps(auth_msg))

                    # Wait for auth response
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                        auth_result = json.loads(response)
                        if auth_result.get("type") == "auth_success":
                            logger.info(f"✓ Authenticated with VM agent {machine_id}")
                        else:
                            logger.warning(f"Auth response: {auth_result}")
                            # Continue anyway - old agents don't auth
                    except asyncio.TimeoutError:
                        logger.warning("Auth timeout - agent may not require auth")
                    except Exception as e:
                        logger.warning(f"Auth handshake issue: {e}")

                    # Store session data
                    self.session_data[machine_id] = {
                        "host": target_host,
                        "agent_port": agent_port,
                        "connected_at": time.time(),
                    }

                    # Start heartbeat
                    await self._start_heartbeat(machine_id)

                    self.reconnect_attempts[machine_id] = 0
                    self.last_successful_command[machine_id] = time.time()
                    logger.info(f"✅ Persistent connection established for {machine_id}")
                    return True

                except asyncio.TimeoutError:
                    logger.error(f"⏱️ Timeout connecting to {target_host}:{agent_port}")
                except Exception as e:
                    logger.error(f"❌ Connection failed: {e}")

                self.reconnect_attempts[machine_id] += 1
                if self.reconnect_attempts[machine_id] < self.max_reconnect_attempts:
                    delay = min(
                        self.reconnect_delay * (2 ** (self.reconnect_attempts[machine_id] - 1)),
                        self.max_reconnect_delay,
                    )
                    logger.info(f"⏳ Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)

            logger.error(f"❌ Max reconnect attempts reached for {machine_id}")
            return False

    async def ensure_connection(self, machine_id: str) -> bool:
        """Ensure we have an active connection, reconnecting if needed."""
        if machine_id in self.connections:
            ws = self.connections[machine_id]
            try:
                if not ws.closed:
                    # Recently used? Trust it.
                    last = self.last_successful_command.get(machine_id, 0)
                    if time.time() - last < 60:
                        return True
                    # Idle - quick ping
                    await asyncio.wait_for(ws.ping(), timeout=3.0)
                    return True
            except Exception:
                logger.warning(f"Connection check failed for {machine_id}")

        # Reconnect using stored session data
        if machine_id in self.session_data:
            session = self.session_data[machine_id]
            return await self.connect(
                session["agent_port"],
                machine_id,
                session.get("host"),
            )
        return False

    async def execute_command(
        self,
        machine_id: str,
        command: str,
        parameters: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute a command on the VM using the persistent connection."""
        # Get per-machine command lock
        if machine_id not in self.command_locks:
            self.command_locks[machine_id] = asyncio.Lock()

        async with self.command_locks[machine_id]:
            return await self._execute_inner(machine_id, command, parameters, timeout)

    async def _execute_inner(
        self,
        machine_id: str,
        command: str,
        parameters: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Inner command execution (called under lock)."""
        if not await self.ensure_connection(machine_id):
            return {"success": False, "error": "Cannot connect to VM agent"}

        ws = self.connections[machine_id]
        if timeout is None:
            timeout = self.command_timeout.get(command, self.command_timeout["default"])

        command_msg = {
            "type": "command",
            "data": {
                "command": command,
                "parameters": parameters,
            },
            "timestamp": time.time(),
        }

        max_retries = 2
        for attempt in range(max_retries):
            try:
                await ws.send(json.dumps(command_msg))

                start_time = time.time()
                while time.time() - start_time < timeout:
                    try:
                        remaining = timeout - (time.time() - start_time)
                        response = await asyncio.wait_for(
                            ws.recv(),
                            timeout=min(remaining, 5.0),
                        )
                        result = json.loads(response)

                        if result.get("type") == "result":
                            self.last_successful_command[machine_id] = time.time()
                            return result.get("data", {})
                        elif result.get("type") == "error":
                            return {
                                "success": False,
                                "error": result.get("data", {}).get("error", "Unknown error"),
                            }
                        # else: unexpected type, keep waiting

                    except asyncio.TimeoutError:
                        if time.time() - start_time >= timeout:
                            raise
                        continue

                raise asyncio.TimeoutError(f"Command timeout after {timeout}s")

            except asyncio.TimeoutError:
                logger.error(f"⏱️ Timeout for {command} on {machine_id} (attempt {attempt + 1})")
                if attempt < max_retries - 1:
                    await self._cleanup(machine_id)
                    await asyncio.sleep(1)
                    if not await self.ensure_connection(machine_id):
                        return {"success": False, "error": "Reconnection failed"}
                    ws = self.connections[machine_id]
                else:
                    return {"success": False, "error": f"Command timeout after {max_retries} attempts"}

            except Exception as e:
                logger.error(f"❌ Command error: {e}")
                if attempt < max_retries - 1:
                    await self._cleanup(machine_id)
                    await asyncio.sleep(1)
                    if await self.ensure_connection(machine_id):
                        ws = self.connections[machine_id]
                        continue
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "Command failed after all retries"}

    async def take_screenshot(self, machine_id: str) -> Optional[str]:
        """Convenience method to capture a screenshot."""
        result = await self.execute_command(machine_id, "screenshot", {})
        if result.get("success") and result.get("screenshot"):
            b64 = result["screenshot"]
            if not b64.startswith("data:image"):
                b64 = f"data:image/jpeg;base64,{b64}"
            return b64
        return None

    # ── Heartbeat ───────────────────────────────────────────────────────

    async def _start_heartbeat(self, machine_id: str):
        if machine_id in self.heartbeat_tasks:
            self.heartbeat_tasks[machine_id].cancel()
        self.heartbeat_tasks[machine_id] = asyncio.create_task(
            self._heartbeat_loop(machine_id)
        )

    async def _heartbeat_loop(self, machine_id: str):
        consecutive_failures = 0
        while machine_id in self.connections:
            try:
                ws = self.connections.get(machine_id)
                if not ws or ws.closed:
                    break

                try:
                    pong = await ws.ping()
                    await asyncio.wait_for(pong, timeout=5.0)
                    consecutive_failures = 0
                except asyncio.TimeoutError:
                    consecutive_failures += 1
                    logger.warning(f"Heartbeat timeout ({consecutive_failures}/3)")
                except Exception:
                    consecutive_failures += 1

                if consecutive_failures >= 3:
                    logger.error(f"Too many heartbeat failures for {machine_id}")
                    await self._cleanup(machine_id)
                    asyncio.create_task(self.ensure_connection(machine_id))
                    break

                await asyncio.sleep(self.heartbeat_interval)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(self.heartbeat_interval)

    # ── Cleanup ─────────────────────────────────────────────────────────

    async def _cleanup(self, machine_id: str):
        if machine_id in self.heartbeat_tasks:
            self.heartbeat_tasks[machine_id].cancel()
            try:
                await asyncio.wait_for(self.heartbeat_tasks[machine_id], timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            del self.heartbeat_tasks[machine_id]

        if machine_id in self.connections:
            try:
                ws = self.connections[machine_id]
                if not ws.closed:
                    await ws.close(code=1000, reason="Cleanup")
            except Exception:
                pass
            del self.connections[machine_id]

    async def disconnect(self, machine_id: str):
        await self._cleanup(machine_id)
        self.session_data.pop(machine_id, None)
        self.connection_locks.pop(machine_id, None)
        self.command_locks.pop(machine_id, None)
        self.reconnect_attempts.pop(machine_id, None)

    def get_status(self, machine_id: str) -> Dict[str, Any]:
        if machine_id not in self.connections:
            return {"connected": False}

        ws = self.connections[machine_id]
        return {
            "connected": not ws.closed,
            **self.session_data.get(machine_id, {}),
        }


# Singleton instance
vm_control_service = VMControlService()
