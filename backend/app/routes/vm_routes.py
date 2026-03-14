"""VM management API routes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import create_client
from app.auth import get_current_user, get_service_client
from app.services.vm_service import vm_service

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
