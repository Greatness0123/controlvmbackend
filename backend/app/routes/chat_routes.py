"""Chat session API routes with SSE streaming."""
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from app.auth import get_current_user, get_service_client
from app.services.agent_executor import agent_executor

router = APIRouter(prefix="/api/chat", tags=["Chat"])


class CreateSessionRequest(BaseModel):
    vm_id: Optional[str] = None
    device_id: Optional[str] = None


class UpdateSessionRequest(BaseModel):
    vm_id: Optional[str] = None
    device_id: Optional[str] = None
    title: Optional[str] = None


class SendMessageRequest(BaseModel):
    message: str
    model: Optional[str] = "gemini-2.0-flash"


@router.get("/list")
async def list_sessions(user: dict = Depends(get_current_user)):
    db = get_service_client()
    result = db.table("chat_sessions").select("*")\
        .eq("user_id", user["id"])\
        .order("updated_at", desc=True)\
        .execute()
    return {"sessions": result.data}


@router.post("/create")
async def create_session(req: CreateSessionRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    session_data = {
        "user_id": user["id"],
        "title": "New Chat",
    }
    if req.vm_id:
        session_data["vm_id"] = req.vm_id
    if req.device_id:
        session_data["device_id"] = req.device_id

    result = db.table("chat_sessions").insert(session_data).execute()
    return {"session": result.data[0]}


@router.get("/{session_id}/messages")
async def get_messages(session_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    
    # Verify ownership
    session = db.table("chat_sessions").select("user_id").eq("id", session_id).execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    result = db.table("chat_messages").select("*")\
        .eq("session_id", session_id)\
        .order("created_at")\
        .execute()
    return {"messages": result.data}


@router.post("/{session_id}/send")
async def send_message(session_id: str, req: SendMessageRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()

    # Verify ownership
    session = db.table("chat_sessions").select("*, virtual_machines(*)")\
        .eq("id", session_id)\
        .execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    session_data = session.data[0]
    vm_data = session_data.get("virtual_machines") or {}

    async def event_stream():
        async for event in agent_executor.execute_task(db, session_id, req.message, session_data):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.patch("/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    session = db.table("chat_sessions").select("user_id").eq("id", session_id).execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    update_data = {}
    if req.vm_id is not None:
        update_data["vm_id"] = req.vm_id
    if req.device_id is not None:
        update_data["device_id"] = req.device_id
    if req.title is not None:
        update_data["title"] = req.title

    result = db.table("chat_sessions").update(update_data).eq("id", session_id).execute()
    return {"session": result.data[0]}


@router.delete("/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    session = db.table("chat_sessions").select("user_id").eq("id", session_id).execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    db.table("chat_messages").delete().eq("session_id", session_id).execute()
    db.table("chat_sessions").delete().eq("id", session_id).execute()
    return {"success": True}
