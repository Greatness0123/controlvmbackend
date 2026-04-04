import asyncio
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from supabase import create_client
from app.auth import get_current_user, get_service_client, get_supabase_client
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)

# When running inside Docker, 'localhost' refers to the container itself.
# VM container ports are mapped to the HOST machine's network interface.
# Use DOCKER_HOST_IP env var (set in docker-compose) to reach host ports,
# falling back to the standard Linux Docker bridge gateway address.
DOCKER_HOST_IP = os.getenv("DOCKER_HOST_IP", "172.17.0.1")

router = APIRouter(prefix="/api/vm", tags=["Virtual Machines"])

class CreateVMRequest(BaseModel):
    name: str = "My Computer"

@router.get("/list")
async def list_vms(user: dict = Depends(get_current_user)):
    db = get_service_client()
    vms = await vm_service.list_vms(db, user["id"])
    return {"vms": vms}

@router.post("/create")
async def create_vm(req: CreateVMRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    try:
        vm = await vm_service.create_vm(db, user["id"], req.name, user.get("plan", "free"))
        return {"vm": vm}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

@router.post("/{vm_id}/start")
async def start_vm(vm_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    try:
        vm = await vm_service.start_vm(db, vm_id, user["id"])
        return {"vm": vm}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post("/{vm_id}/stop")
async def stop_vm(vm_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    try:
        vm = await vm_service.stop_vm(db, vm_id, user["id"])
        return {"vm": vm}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.delete("/{vm_id}")
async def destroy_vm(vm_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    try:
        await vm_service.destroy_vm(db, vm_id, user["id"])
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/{vm_id}/stats")
async def get_vm_stats(vm_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    vm = db.table("virtual_machines").select("*").eq("id", vm_id).eq("user_id", user["id"]).execute()
    if not vm.data:
        raise HTTPException(status_code=404, detail="VM not found")
    
    stats = await vm_service.get_vm_stats(vm_id, vm.data[0].get("container_id", ""))
    return {"stats": stats}

@router.get("/{vm_id}/apps")
async def get_vm_apps(vm_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    try:
        apps = await vm_service.get_vm_apps(db, vm_id, user["id"])
        return {"apps": apps}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── WebSocket Proxy for VNC ─────────────────────────────────────────────────
# This endpoint proxies WebSocket traffic from the browser to the VM container's
# websockify (noVNC) port. This solves the HTTPS/WSS mismatch: the browser
# connects via WSS to this proxy (behind the same TLS termination as the rest of
# the app), and this proxy connects via plain WS to the VM container internally.
@router.websocket("/{vm_id}/ws")
async def vnc_ws_proxy(websocket: WebSocket, vm_id: str, token: str = Query(default="")):
    """
    WebSocket proxy: Browser -(wss)-> Backend -(ws)-> VM container websockify.
    Auth is done via ?token= query param since WebSocket doesn't carry HTTP headers easily.
    """
    # --- Authenticate via token query param ---
    if not token:
        await websocket.close(code=4001, reason="Missing auth token")
        return

    try:
        client = get_supabase_client()
        user_response = client.auth.get_user(token)
        if not user_response or not user_response.user:
            await websocket.close(code=4001, reason="Invalid token")
            return
        auth_id = str(user_response.user.id)
        svc = get_service_client()
        user_result = svc.table("users").select("id").eq("auth_id", auth_id).execute()
        if not user_result.data:
            await websocket.close(code=4001, reason="User not found")
            return
        user_id = user_result.data[0]["id"]
    except Exception as e:
        logger.error(f"[VNC-WS] Auth failed: {e}")
        await websocket.close(code=4001, reason="Auth failed")
        return

    # --- Look up the VM and its noVNC port ---
    db = get_service_client()
    vm_result = db.table("virtual_machines").select("*").eq("id", vm_id).eq("user_id", user_id).execute()
    if not vm_result.data:
        await websocket.close(code=4004, reason="VM not found")
        return

    vm_data = vm_result.data[0]
    novnc_port = vm_data.get("novnc_port")
    if not novnc_port:
        await websocket.close(code=4004, reason="VM has no noVNC port")
        return

    if vm_data.get("status") != "running":
        await websocket.close(code=4004, reason="VM is not running")
        return

    # --- Accept the client WebSocket ---
    await websocket.accept(subprotocol="binary")

    # --- Connect to the VM's websockify ---
    # IMPORTANT: must use the Docker host IP, not localhost.
    # The VM containers' ports are mapped to the HOST, not to this container.
    import websockets
    target_url = f"ws://{DOCKER_HOST_IP}:{novnc_port}/websockify"
    logger.info(f"[VNC-WS] Proxying {vm_id} to {target_url}")

    try:
        async with websockets.connect(
            target_url,
            subprotocols=["binary"],
            max_size=None,
            ping_interval=20,
            ping_timeout=60,
        ) as vm_ws:

            async def client_to_vm():
                """Forward messages from browser → VM."""
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await vm_ws.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def vm_to_client():
                """Forward messages from VM → browser."""
                try:
                    async for message in vm_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass

            # Run both directions concurrently; when either ends, cancel the other
            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_vm()), asyncio.create_task(vm_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        logger.error(f"[VNC-WS] Proxy error for VM {vm_id}: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"[VNC-WS] Connection closed for VM {vm_id}")
