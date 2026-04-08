"""
Remote Desktop WebSocket Relay

Replaces Supabase Realtime for desktop streaming.
Two connection types:
  - Desktop app connects as 'producer' (sends frames)
  - Web viewer connects as 'consumer' (receives frames)

This eliminates the Supabase relay hop and removes the ~256KB message limit,
giving near-Chrome-Remote-Desktop latency.
"""

import asyncio
import json
import logging
import os
import time
from typing import Dict, Set, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/remote", tags=["Remote Desktop Relay"])


class DesktopRelayRoom:
    """A relay room for one paired device. One producer, many consumers."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.producer: Optional[WebSocket] = None
        self.consumers: Set[WebSocket] = set()
        self.last_frame: Optional[bytes] = None  # cache for late joiners
        self.last_frame_time: float = 0
        self.frame_count: int = 0

    async def set_producer(self, ws: WebSocket):
        """Register the desktop app as the frame producer."""
        if self.producer:
            try:
                await self.producer.close(code=4002, reason="Replaced by new producer")
            except Exception:
                pass
        self.producer = ws
        logger.info(f"[Relay:{self.device_id}] Desktop producer connected")

    async def add_consumer(self, ws: WebSocket):
        """Register a web viewer as a frame consumer."""
        self.consumers.add(ws)
        logger.info(f"[Relay:{self.device_id}] Consumer added ({len(self.consumers)} total)")

        # Send cached last frame immediately so viewer isn't blank
        if self.last_frame:
            try:
                await ws.send_bytes(self.last_frame)
            except Exception:
                pass

    def remove_consumer(self, ws: WebSocket):
        self.consumers.discard(ws)
        logger.info(f"[Relay:{self.device_id}] Consumer removed ({len(self.consumers)} total)")

    async def broadcast_frame(self, data: bytes):
        """Forward a frame from producer to all consumers."""
        self.last_frame = data
        self.last_frame_time = time.time()
        self.frame_count += 1

        dead = []
        for consumer in self.consumers:
            try:
                await consumer.send_bytes(data)
            except Exception:
                dead.append(consumer)

        for ws in dead:
            self.consumers.discard(ws)

    async def relay_action(self, action_json: str):
        """Forward an action from a consumer to the producer."""
        if self.producer:
            try:
                await self.producer.send_text(action_json)
            except Exception:
                logger.warning(f"[Relay:{self.device_id}] Failed to send action to producer")

    def is_empty(self) -> bool:
        return self.producer is None and len(self.consumers) == 0


class DesktopRelayManager:
    """Manages all relay rooms."""

    def __init__(self):
        self.rooms: Dict[str, DesktopRelayRoom] = {}

    def get_or_create(self, device_id: str) -> DesktopRelayRoom:
        if device_id not in self.rooms:
            self.rooms[device_id] = DesktopRelayRoom(device_id)
        return self.rooms[device_id]

    def cleanup(self, device_id: str):
        room = self.rooms.get(device_id)
        if room and room.is_empty():
            del self.rooms[device_id]
            logger.info(f"[RelayManager] Cleaned up room {device_id}")


relay_manager = DesktopRelayManager()


# ─── Agent Helpers (for agent_executor.py) ──────────────────────────

async def send_device_action(device_id: str, action_type: str, params: Optional[dict] = None) -> bool:
    """Send an action to a device via the relay (for AI agent)."""
    room = relay_manager.rooms.get(device_id)
    if not room or not room.producer:
        return False
        
    payload = {"type": action_type}
    if params:
        payload.update(params)
        
    try:
        # Re-verify producer in the same tick
        if room.producer:
            await room.relay_action(json.dumps(payload))
            return True
        return False
    except Exception as e:
        logger.error(f"[Relay:Agent] Failed to send action: {e}")
        return False


def get_device_screenshot(device_id: str) -> Optional[bytes]:
    """Get the latest cached frame from a device (for AI agent)."""
    room = relay_manager.rooms.get(device_id)
    if not room:
        return None
    return room.last_frame


# ─── Auth helper (same as VNC proxy) ────────────────────────────────

def _auth_from_token(token: str):
    """Authenticate via Supabase JWT and return user_id."""
    from app.auth import get_supabase_client, get_service_client
    client = get_supabase_client()
    user_response = client.auth.get_user(token)
    if not user_response or not user_response.user:
        return None
    auth_id = str(user_response.user.id)
    svc = get_service_client()
    user_result = svc.table("users").select("id").eq("auth_id", auth_id).execute()
    if not user_result.data:
        return None
    return user_result.data[0]["id"]


def _verify_device_ownership(user_id: str, device_id: str) -> bool:
    """Check that the device belongs to this user."""
    from app.auth import get_service_client
    svc = get_service_client()
    result = svc.table("paired_devices").select("id").eq("id", device_id).eq("user_id", user_id).execute()
    return bool(result.data)


# ─── WebSocket Endpoints ────────────────────────────────────────────

@router.websocket("/{device_id}/producer")
async def desktop_producer(websocket: WebSocket, device_id: str, token: str = Query(default="")):
    """
    Desktop app connects here to SEND screen frames.
    
    Protocol:
    - Binary messages = frame data (JPEG bytes)
    - Text messages = JSON control messages from the web viewer (actions)
    """
    if not token:
        await websocket.close(code=4001, reason="Missing auth token")
        return

    try:
        user_id = _auth_from_token(token)
        if not user_id:
            await websocket.close(code=4001, reason="Invalid token")
            return
        if not _verify_device_ownership(user_id, device_id):
            await websocket.close(code=4003, reason="Device not found")
            return
    except Exception as e:
        logger.error(f"[Relay] Producer auth failed: {e}")
        await websocket.close(code=4001, reason="Auth failed")
        return

    await websocket.accept()
    room = relay_manager.get_or_create(device_id)
    await room.set_producer(websocket)

    try:
        while True:
            # Producer sends binary frames
            message = await websocket.receive()

            if "bytes" in message and message["bytes"]:
                # Binary = screen frame → broadcast to consumers
                await room.broadcast_frame(message["bytes"])
            elif "text" in message and message["text"]:
                # Text = control/status message from desktop
                data = json.loads(message["text"])
                msg_type = data.get("type")

                if msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                elif msg_type == "status":
                    # Forward status updates to consumers
                    for consumer in room.consumers:
                        try:
                            await consumer.send_text(message["text"])
                        except Exception:
                            pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[Relay] Producer error: {e}")
    finally:
        room.producer = None
        relay_manager.cleanup(device_id)
        logger.info(f"[Relay:{device_id}] Desktop producer disconnected")


@router.websocket("/{device_id}/viewer")
async def desktop_viewer(websocket: WebSocket, device_id: str, token: str = Query(default="")):
    """
    Web viewer connects here to RECEIVE screen frames and SEND actions.
    
    Protocol:
    - Receives binary messages = frame data (JPEG bytes)  
    - Sends text messages = JSON actions (click, type, key, etc.)
    """
    if not token:
        await websocket.close(code=4001, reason="Missing auth token")
        return

    try:
        user_id = _auth_from_token(token)
        if not user_id:
            await websocket.close(code=4001, reason="Invalid token")
            return
        if not _verify_device_ownership(user_id, device_id):
            await websocket.close(code=4003, reason="Device not found")
            return
    except Exception as e:
        logger.error(f"[Relay] Viewer auth failed: {e}")
        await websocket.close(code=4001, reason="Auth failed")
        return

    await websocket.accept()
    room = relay_manager.get_or_create(device_id)
    await room.add_consumer(websocket)

    # Notify producer that a viewer joined
    if room.producer:
        try:
            await room.producer.send_text(json.dumps({
                "type": "viewer_joined",
                "count": len(room.consumers)
            }))
        except Exception:
            pass

    try:
        while True:
            # Consumer sends text actions
            text = await websocket.receive_text()
            # Forward action to the desktop producer
            await room.relay_action(text)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[Relay] Viewer error: {e}")
    finally:
        room.remove_consumer(websocket)
        relay_manager.cleanup(device_id)


# ─── REST endpoint for relay status ─────────────────────────────────

@router.get("/{device_id}/status")
async def relay_status(device_id: str):
    """Check if a device has an active relay room."""
    room = relay_manager.rooms.get(device_id)
    if not room:
        return {"active": False, "producer": False, "consumers": 0}
    return {
        "active": True,
        "producer": room.producer is not None,
        "consumers": len(room.consumers),
        "frames": room.frame_count,
        "last_frame_age": time.time() - room.last_frame_time if room.last_frame_time else None,
    }
