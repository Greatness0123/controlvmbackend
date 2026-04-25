import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List, Any
from app.auth import get_current_user, get_service_client

router = APIRouter(prefix="/api/workflows", tags=["Workflows"])

class WorkflowCreateRequest(BaseModel):
    name: str
    enabled: Optional[bool] = True
    trigger: Optional[dict] = {"type": "none"}
    nodes: Optional[List[Any]] = []
    edges: Optional[List[Any]] = []
    steps: Optional[List[Any]] = []

class WorkflowUpdateRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    trigger: Optional[dict] = None
    nodes: Optional[List[Any]] = None
    edges: Optional[List[Any]] = None
    steps: Optional[List[Any]] = None

@router.get("/list")
async def list_workflows(user: dict = Depends(get_current_user)):
    db = get_service_client()
    result = db.table("workflows").select("*")\
        .eq("user_id", user["id"])\
        .order("updated_at", desc=True)\
        .execute()
    return {"workflows": result.data}

@router.post("/create")
async def create_workflow(req: WorkflowCreateRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    workflow_data = {
        "user_id": user["id"],
        "name": req.name,
        "enabled": req.enabled,
        "trigger": req.trigger,
        "nodes": req.nodes,
        "edges": req.edges,
        "steps": req.steps
    }
    result = db.table("workflows").insert(workflow_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create workflow")
    return {"workflow": result.data[0]}

@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    result = db.table("workflows").select("*")\
        .eq("id", workflow_id)\
        .eq("user_id", user["id"])\
        .execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"workflow": result.data[0]}

@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: str, req: WorkflowUpdateRequest, user: dict = Depends(get_current_user)
):
    db = get_service_client()

    # Check ownership
    existing = db.table("workflows").select("id").eq("id", workflow_id).eq("user_id", user["id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Workflow not found")

    update_data = req.dict(exclude_none=True)
    if update_data:
        result = db.table("workflows").update(update_data).eq("id", workflow_id).execute()
        return {"workflow": result.data[0] if result.data else {}}
    return {"workflow": {}}

@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    # Check ownership
    existing = db.table("workflows").select("id").eq("id", workflow_id).eq("user_id", user["id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Workflow not found")

    db.table("workflows").delete().eq("id", workflow_id).execute()
    return {"success": True}

class WorkflowExecuteRequest(BaseModel):
    target_id: str
    target_type: str # 'vm' or 'device'

@router.post("/{workflow_id}/execute")
async def execute_workflow(
    workflow_id: str,
    req: WorkflowExecuteRequest,
    user: dict = Depends(get_current_user)
):
    db = get_service_client()

    # Check ownership of workflow
    wf_res = db.table("workflows").select("*").eq("id", workflow_id).eq("user_id", user["id"]).execute()
    if not wf_res.data:
        raise HTTPException(status_code=404, detail="Workflow not found")

    workflow = wf_res.data[0]

    # Verify ownership of the target machine
    if req.target_type == "device":
        target_res = db.table("paired_devices").select("id").eq("id", req.target_id).eq("user_id", user["id"]).execute()
        if not target_res.data:
            raise HTTPException(status_code=403, detail="Unauthorized: Target device does not belong to user.")
        channel_name = f"remote_control:{req.target_id}"
    else:
        target_res = db.table("virtual_machines").select("id").eq("id", req.target_id).eq("user_id", user["id"]).execute()
        if not target_res.data:
            raise HTTPException(status_code=403, detail="Unauthorized: Target VM does not belong to user.")
        channel_name = f"vm_control:{req.target_id}"

    # Estimate tokens based on workflow steps complexity
    steps = workflow.get("steps", [])
    estimated_tokens = 500 + (len(steps) * 200)  # Base tokens + per-step estimate

    # Log billing for workflow execution
    try:
        billing_data = {
            "user_id": user["id"],
            "mode": "workflow",
            "tokens": estimated_tokens,
            "created_at": "NOW()",
            "workflow_id": workflow_id,
            "workflow_name": workflow.get("name", "Unknown")
        }
        db.table("billing_metrics").insert(billing_data).execute()

        # Update user usage stats
        user_res = db.table("users").select("daily_token_usage").eq("id", user["id"]).execute()
        if user_res.data:
            from datetime import datetime, timezone
            daily_stats = user_res.data[0].get("daily_token_usage", {})
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if today not in daily_stats:
                daily_stats[today] = {"workflow": 0, "total": 0}

            daily_stats[today]["workflow"] = daily_stats[today].get("workflow", 0) + 1
            daily_stats[today]["total"] = daily_stats[today].get("total", 0) + estimated_tokens

            db.table("users").update({"daily_token_usage": daily_stats}).eq("id", user["id"]).execute()
    except Exception as billing_err:
        print(f"Billing logging error (non-fatal): {billing_err}")

    # Store execution request in the database to trigger Realtime listener on the desktop app
    execution_request = {
        "target_id": req.target_id,
        "target_type": req.target_type,
        "requested_at": "NOW()", # Supabase will handle this or we can use ISO string
        "workflow_data": {
            "id": workflow_id,
            "name": workflow["name"],
            "steps": workflow["steps"],
            "nodes": workflow["nodes"],
            "edges": workflow["edges"]
        }
    }

    try:
        db.table("workflows").update({
            "last_execution_request": execution_request
        }).eq("id", workflow_id).execute()
    except Exception as e:
        print(f"Execution request storage error: {e}")
        raise HTTPException(status_code=500, detail="Failed to initiate workflow execution.")

    return {"success": True, "message": f"Workflow execution requested on {req.target_type} {req.target_id}"}
