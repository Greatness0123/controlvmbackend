"""
VM Control Service - Enhanced WebSocket communication with VM agents.
Based on open-computer-use's VMControlService architecture.

Maintains persistent connections with heartbeat, auto-reconnect, and
command serialization to prevent concurrent WebSocket operations.

All phases implemented:
- Phase 1: Per-machine locks, cancellation events, health tracking, per-command timeouts
- Phase 2: Tool system integration support (ensure_connection, tool schemas)
- Phase 3: Screenshot optimization (compression, filtering, size limits)
- Phase 4: Connection diagnostics (state tracking, detailed logging, health metrics)
"""

import json
import logging
import asyncio
import time
import os
import base64
from typing import Dict, Any, Optional, Tuple
from io import BytesIO
from PIL import Image

logger = logging.getLogger(__name__)

DOCKER_HOST_IP = os.getenv("DOCKER_HOST_IP", "host.docker.internal")


class CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, success_threshold: int = 3, timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout = timeout
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_state_change = time.time()
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0

    def record_success(self):
        self.total_requests += 1
        self.successful_requests += 1
        self.consecutive_successes += 1
        self.consecutive_failures = 0
        if self.state == CircuitState.HALF_OPEN:
            if self.consecutive_successes >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.consecutive_failures = 0
                self.consecutive_successes = 0
                logger.info("Circuit breaker CLOSED after recovery")

    def record_failure(self):
        self.total_requests += 1
        self.failed_requests += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        if self.state == CircuitState.CLOSED:
            if self.consecutive_failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.last_state_change = time.time()
                logger.warning(f"Circuit breaker OPEN after {self.consecutive_failures} failures")
        elif self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.last_state_change = time.time()
            logger.warning("Circuit breaker OPEN again in half-open state")

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        elif self.state == CircuitState.OPEN:
            if time.time() - self.last_state_change >= self.timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker entering HALF_OPEN state")
                return True
            return False
        elif self.state == CircuitState.HALF_OPEN:
            return True
        return False


class VMControlService:
    """Enhanced service for controlling virtual machines via WebSocket"""
    
    def __init__(self):
        self.connections: Dict[str, Any] = {}
        self.session_data: Dict[str, Dict] = {}
        self.connection_locks: Dict[str, asyncio.Lock] = {}
        self.command_locks: Dict[str, asyncio.Lock] = {}
        self.execution_locks: Dict[str, asyncio.Lock] = {}
        self.execution_owners: Dict[str, str] = {}
        self.cancellation_events: Dict[str, asyncio.Event] = {}
        self.heartbeat_tasks: Dict[str, asyncio.Task] = {}
        self.reconnect_attempts: Dict[str, int] = {}
        self.max_reconnect_attempts = 7
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 15
        self.heartbeat_interval = 30
        self.connection_health: Dict[str, Dict] = {}
        self.last_successful_command: Dict[str, float] = {}
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        
        self.command_timeout = {
            "browser_get_dom": 60.0,
            "browser_get_context": 45.0,
            "browser_get_clickables": 45.0,
            "detect_elements": 60.0,
            "ocr": 45.0,
            "browser_navigate": 45.0,
            "screenshot": 30.0,
            "browser_connect": 30.0,
            "browser_open": 30.0,
            "terminal": 60.0,
            "terminal_execute": 60.0,
            "list_apps": 15.0,
            "default": 30.0
        }
    
    async def connect(
        self,
        agent_port: int,
        machine_id: Optional[str] = None,
        host: Optional[str] = None,
        public_ip: Optional[str] = None,
        vnc_password: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """Establish WebSocket connection to VM agent with auto-reconnect and heartbeat.
        
        Args:
            agent_port: The port the VM agent is listening on
            machine_id: Unique identifier for the machine/VM
            host: Override host (if not provided, uses DOCKER_HOST_IP)
            public_ip: Public IP of the VM (used for dynamic port resolution)
            vnc_password: VNC password for authentication
            session_id: Session ID for auth
            user_id: User ID for auth
        """
        import websockets
        from websockets.protocol import State as WSState

        if not machine_id:
            machine_id = f"vm_{agent_port}"

        target_host = host or DOCKER_HOST_IP

        # Dynamic port resolution: if localhost and default port 8080, use 8081
        actual_port = agent_port
        is_localhost = public_ip in ("localhost", "127.0.0.1", None) or (host and host in ("localhost", "127.0.0.1", "host.docker.internal"))
        
        if is_localhost and agent_port == 8080:
            actual_port = 8081
            logger.info(f"Using port 8081 for localhost connection instead of 8080")

        logger.info(f"🔌 VM CONNECT: machine_id={machine_id}, target_host={target_host}, port={actual_port}")
        logger.info(f"   original_port={agent_port}, public_ip={public_ip}, is_localhost={is_localhost}")
        logger.info(f"   auth: session_id={session_id or 'auto'}, user_id={user_id or 'default'}, has_password={bool(vnc_password)}")
        
        if machine_id not in self.connection_locks:
            self.connection_locks[machine_id] = asyncio.Lock()
        
        async with self.connection_locks[machine_id]:
            if machine_id in self.connections:
                ws = self.connections[machine_id]
                try:
                    if ws.state == WSState.OPEN:
                        logger.info(f"Reusing existing persistent connection for machine {machine_id}")
                        self.last_successful_command[machine_id] = time.time()
                        return True
                except Exception as e:
                    logger.debug(f"Connection check error: {e}")
                logger.warning(f"Connection for {machine_id} is closed, cleaning up")
                await self._cleanup_connection(machine_id)
            
            self.reconnect_attempts[machine_id] = 0
            
            while self.reconnect_attempts[machine_id] < self.max_reconnect_attempts:
                try:
                    agent_url = f"ws://{target_host}:{actual_port}"
                    logger.info(
                        f"🔌 attempt {self.reconnect_attempts[machine_id] + 1}/{self.max_reconnect_attempts}: connecting to {agent_url}"
                    )

                    # Test connectivity first with a quick socket test
                    try:
                        import socket
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(2)
                        result = sock.connect_ex((target_host, actual_port))
                        sock.close()
                        if result == 0:
                            logger.info(f"✅ TCP port {actual_port} is reachable on {target_host}")
                        else:
                            logger.warning(f"❌ TCP port {actual_port} NOT reachable on {target_host} (connect_ex code: {result})")
                            # Code 111 = ECONNREFUSED, 113 = EHOSTUNREACH, 101 = ENETUNREACH
                            if result == 111:
                                logger.warning(f"   💡 Connection refused - VM agent may not be running on port {actual_port}")
                            elif result == 113:
                                logger.warning(f"   💡 No route to host - check firewall/network settings")
                    except Exception as e:
                        logger.debug(f"Port check failed (non-critical): {e}")

                    websocket = await asyncio.wait_for(
                        websockets.connect(
                            agent_url,
                            ping_interval=20,
                            ping_timeout=10,
                            close_timeout=10,
                            max_size=50 * 1024 * 1024,
                            compression=None,
                        ),
                        timeout=15,
                    )

                    self.connections[machine_id] = websocket
                    logger.info(f"✅ WebSocket CONNECTED to {agent_url}")

                    # Enhanced auth message with all credentials
                    auth_msg = {
                        "type": "auth",
                        "sessionId": session_id or f"backend_{int(time.time())}",
                        "userId": user_id or "backend_agent",
                        "password": vnc_password or "",
                    }
                    logger.info(f"📤 Sending AUTH: {json.dumps(auth_msg)}")
                    await websocket.send(json.dumps(auth_msg))

                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                        auth_result = json.loads(response)
                        if auth_result.get("type") == "auth_success":
                            logger.info(f"✓ Authenticated with VM agent {machine_id}: {auth_result}")
                        else:
                            logger.error(f"❌ Auth rejected by VM agent {machine_id}: {auth_result}")
                            await websocket.close()
                            del self.connections[machine_id]
                            self.reconnect_attempts[machine_id] += 1
                            continue
                    except asyncio.TimeoutError:
                        logger.error(f"⏱️ Auth timeout for {machine_id} - closing connection")
                        await websocket.close()
                        del self.connections[machine_id]
                        self.reconnect_attempts[machine_id] += 1
                        continue
                    except Exception as e:
                        logger.error(f"❌ Auth handshake error for {machine_id}: {e}")
                        await websocket.close()
                        del self.connections[machine_id]
                        self.reconnect_attempts[machine_id] += 1
                        continue

                    if machine_id not in self.execution_locks:
                        self.execution_locks[machine_id] = asyncio.Lock()
                    if machine_id not in self.cancellation_events:
                        self.cancellation_events[machine_id] = asyncio.Event()
                        self.cancellation_events[machine_id].set()
                    
                    self.connection_health[machine_id] = {
                        "connected_at": time.time(),
                        "last_heartbeat": time.time(),
                        "commands_executed": 0,
                        "commands_failed": 0,
                        "status": "healthy",
                        "consecutive_heartbeat_failures": 0,
                    }

                    self.session_data[machine_id] = {
                        "host": target_host,
                        "agent_port": agent_port,
                        "public_ip": public_ip,
                        "vnc_password": vnc_password,
                        "session_id": session_id,
                        "user_id": user_id,
                        "connected_at": time.time(),
                    }

                    if machine_id not in self.circuit_breakers:
                        self.circuit_breakers[machine_id] = CircuitBreaker()

                    await self._start_heartbeat(machine_id)

                    self.reconnect_attempts[machine_id] = 0
                    self.last_successful_command[machine_id] = time.time()
                    logger.info(f"Persistent connection established for {machine_id}")
                    return True

                except asyncio.TimeoutError:
                    logger.error(f"Timeout connecting to {target_host}:{agent_port}")
                except Exception as e:
                    logger.error(f"Connection failed: {e}")

                self.reconnect_attempts[machine_id] += 1
                if self.reconnect_attempts[machine_id] < self.max_reconnect_attempts:
                    delay = min(
                        self.reconnect_delay * (2 ** (self.reconnect_attempts[machine_id] - 1)),
                        self.max_reconnect_delay,
                    )
                    logger.info(f"Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)

            logger.error(f"Max reconnect attempts reached for {machine_id}")
            return False

    async def _cleanup_connection(self, machine_id: str):
        """Clean up a specific machine's connection."""
        if machine_id in self.heartbeat_tasks:
            task = self.heartbeat_tasks[machine_id]
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            del self.heartbeat_tasks[machine_id]
        
        if machine_id in self.connections:
            try:
                ws = self.connections[machine_id]
                from websockets.protocol import State as WSState
                if ws.state == WSState.OPEN:
                    await ws.close(code=1000, reason="Session ended")
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.debug(f"Error closing WebSocket for {machine_id}: {e}")
            finally:
                del self.connections[machine_id]
        
        if machine_id in self.connection_health:
            del self.connection_health[machine_id]

    async def _start_heartbeat(self, machine_id: str):
        """Start heartbeat task for a machine using native WebSocket ping frames."""
        if machine_id in self.heartbeat_tasks:
            self.heartbeat_tasks[machine_id].cancel()
        
        self.heartbeat_tasks[machine_id] = asyncio.create_task(
            self._heartbeat_loop(machine_id)
        )

    async def _heartbeat_loop(self, machine_id: str):
        """Heartbeat using native WebSocket ping frames with failure tracking."""
        from websockets.protocol import State as WSState
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        while machine_id in self.connections:
            try:
                ws = self.connections.get(machine_id)
                if not ws or ws.state != WSState.OPEN:
                    logger.warning(f"Connection lost for machine {machine_id}")
                    break
                
                try:
                    pong_waiter = await ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=5.0)
                    
                    consecutive_failures = 0
                    if machine_id in self.connection_health:
                        self.connection_health[machine_id]["last_heartbeat"] = time.time()
                        self.connection_health[machine_id]["status"] = "healthy"
                        self.connection_health[machine_id]["consecutive_heartbeat_failures"] = 0
                    logger.debug(f"Heartbeat successful for {machine_id}")
                    
                except asyncio.TimeoutError:
                    consecutive_failures += 1
                    logger.warning(f"Heartbeat timeout for {machine_id}, failures: {consecutive_failures}/{max_consecutive_failures}")
                    if machine_id in self.connection_health:
                        self.connection_health[machine_id]["status"] = "unhealthy"
                        self.connection_health[machine_id]["consecutive_heartbeat_failures"] = consecutive_failures
                except Exception as e:
                    consecutive_failures += 1
                    logger.warning(f"Heartbeat error for {machine_id}: {e}")
                
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(f"Too many heartbeat failures for {machine_id}, triggering reconnection")
                    await self._cleanup_connection(machine_id)
                    asyncio.create_task(self.ensure_connection(machine_id))
                    break
                
                await asyncio.sleep(self.heartbeat_interval)
                
            except asyncio.CancelledError:
                logger.debug(f"Heartbeat task cancelled for {machine_id}")
                raise
            except Exception as e:
                logger.error(f"Unexpected heartbeat error for machine {machine_id}: {e}")
                await asyncio.sleep(self.heartbeat_interval)
        
        await self._cleanup_connection(machine_id)

    async def ensure_connection(self, machine_id: str) -> bool:
        """Ensure we have an active connection, reconnecting if needed."""
        from websockets.protocol import State as WSState
        
        if machine_id in self.connections:
            ws = self.connections[machine_id]
            try:
                if ws.state == WSState.OPEN:
                    logger.debug(f"Connection for {machine_id} is OPEN, checking last used...")
                    last = self.last_successful_command.get(machine_id, 0)
                    if time.time() - last < 60:
                        logger.debug(f"Reusing active connection for {machine_id} (last used {time.time() - last:.1f}s ago)")
                        return True
                    # Do a quick ping test for idle connections
                    await asyncio.wait_for(ws.ping(), timeout=3.0)
                    logger.debug(f"Connection for {machine_id} verified and reused")
                    return True
            except Exception as e:
                logger.warning(f"Connection check failed for {machine_id}: {e}")

        # Try to reconnect using stored session data
        if machine_id in self.session_data:
            session = self.session_data[machine_id]
            logger.info(f"Attempting to reconnect to {machine_id} with stored session data")
            return await self.connect(
                session.get("agent_port", 8080),
                machine_id,
                session.get("host"),
                session.get("public_ip"),
                session.get("vnc_password"),
                session.get("session_id"),
                session.get("user_id"),
            )
        logger.warning(f"No session data found for machine {machine_id}")
        return False

    def get_command_lock(self, machine_id: str) -> asyncio.Lock:
        """Get or create a per-machine command lock."""
        if machine_id not in self.command_locks:
            self.command_locks[machine_id] = asyncio.Lock()
        return self.command_locks[machine_id]

    def get_execution_lock(self, machine_id: str) -> asyncio.Lock:
        """Get or create a per-machine execution lock for session-level mutual exclusion."""
        if machine_id not in self.execution_locks:
            self.execution_locks[machine_id] = asyncio.Lock()
        return self.execution_locks[machine_id]

    def is_machine_busy(self, machine_id: str) -> Tuple[bool, Optional[str]]:
        """Check if a machine is currently executing a task. Returns (busy, owner_chat_id)."""
        lock = self.execution_locks.get(machine_id)
        if lock and lock.locked():
            return True, self.execution_owners.get(machine_id)
        return False, None

    def get_cancellation_event(self, machine_id: str) -> asyncio.Event:
        """Get or create a per-machine cancellation event."""
        if machine_id not in self.cancellation_events:
            self.cancellation_events[machine_id] = asyncio.Event()
        return self.cancellation_events[machine_id]

    def request_cancellation(self, machine_id: str) -> Tuple[bool, Optional[str]]:
        """Signal running execution to stop. Returns (was_busy, owner_chat_id)."""
        busy, owner = self.is_machine_busy(machine_id)
        if busy:
            self.get_cancellation_event(machine_id).set()
        return busy, owner

    def reset_cancellation(self, machine_id: str):
        """Clear stale cancellation signal (called when new execution starts)."""
        if machine_id in self.cancellation_events:
            self.cancellation_events[machine_id].clear()

    async def cancel_execution(self, machine_id: str) -> bool:
        """Cancel any ongoing command execution for a machine."""
        if machine_id in self.cancellation_events:
            self.cancellation_events[machine_id].clear()
            logger.info(f"Cancelled execution for {machine_id}")
            return True
        return False

    async def resume_execution(self, machine_id: str) -> bool:
        """Resume command execution after cancellation."""
        if machine_id in self.cancellation_events:
            self.cancellation_events[machine_id].set()
            logger.info(f"Resumed execution for {machine_id}")
            return True
        return False

    async def execute_command(
        self,
        machine_id: str,
        command: str,
        parameters: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute a command on the VM using the persistent connection."""
        async with self.get_command_lock(machine_id):
            return await self._execute_inner(machine_id, command, parameters, timeout)

    async def _execute_inner(
        self,
        machine_id: str,
        command: str,
        parameters: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Inner command execution (called under lock)."""
        
        logger.info(f"📝 Executing command '{command}' on machine {machine_id}")
        logger.debug(f"   Parameters: {parameters}")
        
        if machine_id in self.cancellation_events:
            if not self.cancellation_events[machine_id].is_set():
                logger.warning(f"Execution cancelled for {machine_id} before command")
                return {"success": False, "error": "Execution cancelled"}
        
        if machine_id in self.circuit_breakers:
            breaker = self.circuit_breakers[machine_id]
            if not breaker.can_execute():
                logger.error(f"Circuit breaker open for {machine_id}, rejecting command")
                return {"success": False, "error": "Circuit breaker open - service unavailable"}
        
        logger.debug(f"Ensuring connection for {machine_id} before executing command")
        if not await self.ensure_connection(machine_id):
            logger.error(f"❌ Failed to ensure connection for {machine_id}")
            if machine_id in self.circuit_breakers:
                self.circuit_breakers[machine_id].record_failure()
            return {"success": False, "error": "Cannot connect to VM agent"}
        
        logger.debug(f"Connection ready for {machine_id}, sending command")
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

        max_retries = 3
        for attempt in range(max_retries):
            try:
                if machine_id in self.cancellation_events:
                    if not self.cancellation_events[machine_id].is_set():
                        return {"success": False, "error": "Execution cancelled"}

                await ws.send(json.dumps(command_msg))

                start_time = time.time()
                while time.time() - start_time < timeout:
                    if machine_id in self.cancellation_events:
                        if not self.cancellation_events[machine_id].is_set():
                            return {"success": False, "error": "Execution cancelled"}

                    try:
                        remaining = timeout - (time.time() - start_time)
                        response = await asyncio.wait_for(
                            ws.recv(),
                            timeout=min(remaining, 5.0),
                        )
                        result = json.loads(response)

                        if result.get("type") == "result":
                            self.last_successful_command[machine_id] = time.time()
                            
                            if machine_id in self.connection_health:
                                self.connection_health[machine_id]["commands_executed"] += 1
                            
                            if machine_id in self.circuit_breakers:
                                self.circuit_breakers[machine_id].record_success()
                            
                            data = result.get("data", {})
                            if data.get("screenshot"):
                                data = self._compress_screenshot(data)
                            
                            return data
                        elif result.get("type") == "error":
                            if machine_id in self.connection_health:
                                self.connection_health[machine_id]["commands_failed"] += 1
                            if machine_id in self.circuit_breakers:
                                self.circuit_breakers[machine_id].record_failure()
                            return result.get("data", {})
                        elif result.get("type") == "pong":
                            continue
                        elif result.get("type") == "auth_success":
                            continue
                    except asyncio.TimeoutError:
                        continue

                logger.warning(f"Command {command} timed out for {machine_id}")
                if machine_id in self.circuit_breakers:
                    self.circuit_breakers[machine_id].record_failure()
                return {"success": False, "error": f"Command timed out after {timeout}s"}

            except Exception as e:
                logger.error(f"Command execution error: {e}")
                if machine_id in self.circuit_breakers:
                    self.circuit_breakers[machine_id].record_failure()
                if attempt < max_retries - 1:
                    await self._cleanup_connection(machine_id)
                    await asyncio.sleep(2)
                    if await self.ensure_connection(machine_id):
                        ws = self.connections[machine_id]
                        await asyncio.sleep(1)
                        continue
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "Max retries exceeded"}

    async def take_screenshot(self, machine_id: str) -> Optional[str]:
        """Take a screenshot and return compressed base64 with data URI prefix."""
        try:
            result = await self.execute_command(machine_id, "screenshot", {})
            if result.get("success") and result.get("screenshot"):
                screenshot_data = result["screenshot"]
                if not screenshot_data.startswith("data:image"):
                    screenshot_data = f"data:image/jpeg;base64,{screenshot_data}"
                return screenshot_data
            return None
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return None

    def _compress_screenshot(self, data: Dict) -> Dict:
        """Compress screenshot using enhanced ImageCompressor."""
        try:
            if not data.get("screenshot"):
                return data
            
            from app.utils.image_compression import ImageCompressor
            
            screenshot_data = data["screenshot"]
            if isinstance(screenshot_data, str):
                if not screenshot_data.startswith("data:image"):
                    screenshot_data = f"data:image/png;base64,{screenshot_data}"
                
                compressed, orig_size, new_size = ImageCompressor.compress_screenshot(
                    screenshot_data,
                    max_width=1280,
                    max_height=720,
                    quality=65,
                    format="JPEG"
                )
                
                data["screenshot"] = compressed
                data["screenshot_compressed"] = True
                data["original_size"] = orig_size
                data["compressed_size"] = new_size
                
                logger.debug(f"Screenshot compressed: {orig_size:,} -> {new_size:,} bytes")
            
        except Exception as e:
            logger.warning(f"Screenshot compression failed: {e}")
        
        return data

    def get_connection_health(self, machine_id: str) -> Optional[Dict]:
        """Get connection health metrics for a machine."""
        health = self.connection_health.get(machine_id)
        if not health:
            return None
        
        ws = self.connections.get(machine_id)
        if ws:
            try:
                from websockets.protocol import State as WSState
                health["ws_state"] = ws.state.name if hasattr(ws.state, 'name') else str(ws.state)
                health["is_open"] = ws.state == WSState.OPEN
            except Exception:
                health["ws_state"] = "unknown"
                health["is_open"] = False
        
        if machine_id in self.circuit_breakers:
            breaker = self.circuit_breakers[machine_id]
            health["circuit_breaker_state"] = breaker.state
            health["circuit_breaker_failures"] = breaker.consecutive_failures
            health["circuit_breaker_successes"] = breaker.consecutive_successes
        
        health["reconnect_attempts"] = self.reconnect_attempts.get(machine_id, 0)
        health["last_command_success"] = self.last_successful_command.get(machine_id, 0)
        
        return health

    def get_all_connections(self) -> Dict[str, Dict]:
        """Get all connection health data."""
        result = {}
        for machine_id in list(self.connection_health.keys()):
            health = self.get_connection_health(machine_id)
            if health:
                result[machine_id] = health
        return result

    def get_connection_status(self, machine_id: str) -> Dict[str, Any]:
        """Get detailed connection status for a machine."""
        from websockets.protocol import State as WSState
        
        if machine_id not in self.connections:
            return {"connected": False, "error": "No connection exists"}
        
        ws = self.connections[machine_id]
        session = self.session_data.get(machine_id, {})
        
        return {
            "connected": ws.state == WSState.OPEN,
            "ws_state": ws.state.name if hasattr(ws.state, 'name') else str(ws.state),
            "host": session.get("host"),
            "agent_port": session.get("agent_port"),
            "connected_at": session.get("connected_at"),
            "reconnect_attempts": self.reconnect_attempts.get(machine_id, 0),
        }

    async def disconnect(self, machine_id: str) -> bool:
        """Disconnect and clean up a machine's connection."""
        await self._cleanup_connection(machine_id)
        
        if machine_id in self.connection_locks:
            del self.connection_locks[machine_id]
        if machine_id in self.command_locks:
            del self.command_locks[machine_id]
        if machine_id in self.execution_locks:
            del self.execution_locks[machine_id]
        if machine_id in self.execution_owners:
            del self.execution_owners[machine_id]
        if machine_id in self.cancellation_events:
            del self.cancellation_events[machine_id]
        if machine_id in self.session_data:
            del self.session_data[machine_id]
        if machine_id in self.reconnect_attempts:
            del self.reconnect_attempts[machine_id]
        if machine_id in self.last_successful_command:
            del self.last_successful_command[machine_id]
        if machine_id in self.circuit_breakers:
            del self.circuit_breakers[machine_id]
        
        logger.info(f"Disconnected and cleaned up {machine_id}")
        return True

    async def disconnect_all(self):
        """Disconnect from all VM agents."""
        logger.info("Disconnecting from all machines")
        machine_ids = list(self.connections.keys())
        for machine_id in machine_ids:
            await self.disconnect(machine_id)
        logger.info("Disconnected from all machines")


vm_control_service = VMControlService()
