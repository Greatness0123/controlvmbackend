"""Chat session API routes with SSE streaming and file upload support."""
import json
import os
import uuid
import base64
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from app.auth import get_current_user, get_service_client
from app.services.agent_executor import agent_executor
from app.services.vm_service import vm_service

router = APIRouter(prefix="/api/chat", tags=["Chat"])

# Supported file types
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
ALLOWED_TEXT_TYPES = {
    "text/plain", "text/markdown", "text/csv",
    "application/json", "application/pdf",
    "text/javascript", "text/typescript", "text/html", "text/css",
    "application/x-python", "text/x-python",
}
ALLOWED_TYPES = ALLOWED_IMAGE_TYPES | ALLOWED_TEXT_TYPES
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


class CreateSessionRequest(BaseModel):
    vm_id: Optional[str] = None
    device_id: Optional[str] = None


class UpdateSessionRequest(BaseModel):
    vm_id: Optional[str] = None
    device_id: Optional[str] = None
    title: Optional[str] = None
    ai_status: Optional[str] = None


class SendMessageRequest(BaseModel):
    message: str
    model: Optional[str] = "gemini-2.5-flash"
    file_url: Optional[str] = None
    file_type: Optional[str] = None


class SaveProviderConfigRequest(BaseModel):
    provider: str
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = "gemini-2.5-flash"
    openai_api_key: Optional[str] = None
    openai_model: Optional[str] = "gpt-4o"
    anthropic_api_key: Optional[str] = None
    anthropic_model: Optional[str] = "claude-3-5-sonnet-20241022"
    openrouter_api_key: Optional[str] = None
    openrouter_model: Optional[str] = "anthropic/claude-3.5-sonnet"
    xai_api_key: Optional[str] = None
    xai_model: Optional[str] = "grok-2-vision-1212"
    ollama_model: Optional[str] = "llava"


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

    session = db.table("chat_sessions").select("user_id").eq("id", session_id).execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    result = db.table("chat_messages").select("*")\
        .eq("session_id", session_id)\
        .order("created_at")\
        .execute()
    return {"messages": result.data}


@router.post("/{session_id}/send")
async def send_message(
    session_id: str, req: SendMessageRequest, user: dict = Depends(get_current_user)
):
    db = get_service_client()

    session = db.table("chat_sessions").select("*, virtual_machines(*)")\
        .eq("id", session_id)\
        .execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    session_data = session.data[0]
    # Inject user_id so agent can look up provider config
    session_data["user_id"] = user["id"]

    # Update VM activity heartbeat if applicable
    if session_data.get("vm_id"):
        await vm_service.update_activity(db, session_data["vm_id"])

    # Optionally attach file info to message for context
    message = req.message
    if req.file_url:
        message += f"\n\n[Attached file: {req.file_url}]"

    # Save user message to database
    db.table("chat_messages").insert({
        "session_id": session_id,
        "role": "user",
        "content": message
    }).execute()

    async def event_stream():
        async for event in agent_executor.execute_task(db, session_id, message, session_data):
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


@router.post("/{session_id}/upload")
async def upload_file(
    session_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload a file and attach it to a chat session."""
    db = get_service_client()

    # Verify session ownership
    session = db.table("chat_sessions").select("user_id").eq("id", session_id).execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    # Validate file type
    content_type = file.content_type or "application/octet-stream"
    ext = os.path.splitext(file.filename or "")[1].lower()
    
    if content_type not in ALLOWED_TYPES and ext not in {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".txt", ".md",
        ".csv", ".json", ".html", ".css", ".pdf"
    }:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{content_type}' not supported. Allowed: images, text files, PDFs, code files."
        )

    # Read and size-check
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 20MB.")

    # For images, return as base64 data URL for AI consumption
    if content_type in ALLOWED_IMAGE_TYPES:
        b64 = base64.b64encode(contents).decode()
        data_url = f"data:{content_type};base64,{b64}"
        return {
            "success": True,
            "file_url": data_url,
            "file_type": "image",
            "filename": file.filename,
            "size": len(contents),
        }

    # For text/code files, return decoded content
    try:
        text_content = contents.decode("utf-8")
    except UnicodeDecodeError:
        text_content = contents.decode("latin-1")

    # Truncate very large files
    if len(text_content) > 50000:
        text_content = text_content[:50000] + "\n...[truncated]"

    return {
        "success": True,
        "file_url": f"[File: {file.filename}]\n\n```\n{text_content}\n```",
        "file_type": "text",
        "filename": file.filename,
        "size": len(contents),
    }


@router.patch("/{session_id}")
async def update_session(
    session_id: str, req: UpdateSessionRequest, user: dict = Depends(get_current_user)
):
    db = get_service_client()
    session = db.table("chat_sessions").select("user_id").eq("id", session_id).execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    update_data = {}
    if req.vm_id is not None:
        update_data["vm_id"] = req.vm_id or None
    if req.device_id is not None:
        update_data["device_id"] = req.device_id or None
    if req.title is not None:
        update_data["title"] = req.title
    if req.ai_status is not None:
        update_data["ai_status"] = req.ai_status

    if update_data:
        result = db.table("chat_sessions").update(update_data).eq("id", session_id).execute()
        return {"session": result.data[0] if result.data else {}}
    return {"session": {}}


@router.delete("/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    session = db.table("chat_sessions").select("user_id").eq("id", session_id).execute()
    if not session.data or session.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")

    db.table("chat_messages").delete().eq("session_id", session_id).execute()
    db.table("chat_sessions").delete().eq("id", session_id).execute()
    return {"success": True}


@router.post("/provider-config")
async def save_provider_config(
    req: SaveProviderConfigRequest, user: dict = Depends(get_current_user)
):
    """Save AI provider configuration for this user."""
    db = get_service_client()
    
    config_data = req.dict(exclude_none=True)
    
    # Upsert user-specific config
    existing = db.table("app_config").select("id")\
        .eq("key", f"api_keys_{user['id']}").execute()
    
    if existing.data:
        db.table("app_config").update({"value": config_data})\
            .eq("key", f"api_keys_{user['id']}").execute()
    else:
        db.table("app_config").insert({
            "key": f"api_keys_{user['id']}",
            "value": config_data,
            "user_id": user["id"],
        }).execute()
    
    return {"success": True}


@router.get("/provider-config")
async def get_provider_config(user: dict = Depends(get_current_user)):
    """Get AI provider configuration for this user."""
    db = get_service_client()
    
    res = db.table("app_config").select("value")\
        .eq("key", f"api_keys_{user['id']}").execute()
    
    if res.data:
        config = res.data[0].get("value", {})
        # Mask API keys in response (show only first 8 chars)
        masked = {}
        for k, v in config.items():
            if "api_key" in k and v:
                masked[k] = v[:8] + "••••••••" if len(v) > 8 else "••••••••"
            else:
                masked[k] = v
        return {"config": masked}
    
    return {"config": {"provider": "gemini"}}


@router.post("/terminal-permission")
async def set_terminal_permission(
    permission: str, user: dict = Depends(get_current_user)
):
    """Set terminal execution permission (always/ask/never)."""
    if permission not in ("always", "ask", "never"):
        raise HTTPException(status_code=400, detail="Invalid permission value")

    db = get_service_client()
    key = f"terminal_permission_{user['id']}"
    
    existing = db.table("app_config").select("id").eq("key", key).execute()
    if existing.data:
        db.table("app_config").update({"value": {"permission": permission}}).eq("key", key).execute()
    else:
        db.table("app_config").insert({"key": key, "value": {"permission": permission}, "user_id": user["id"]}).execute()
    
    return {"success": True, "permission": permission}


@router.get("/terminal-permission")
async def get_terminal_permission(user: dict = Depends(get_current_user)):
    """Get terminal execution permission setting."""
    db = get_service_client()
    key = f"terminal_permission_{user['id']}"
    
    res = db.table("app_config").select("value").eq("key", key).execute()
    if res.data:
        return {"permission": res.data[0].get("value", {}).get("permission", "ask")}
    return {"permission": "ask"}
