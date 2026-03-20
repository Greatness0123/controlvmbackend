"""Desktop pairing API routes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.auth import get_current_user, get_service_client
from app.services.desktop_bridge import desktop_bridge

router = APIRouter(prefix="/api/pair", tags=["Desktop Pairing"])


class GenerateCodeRequest(BaseModel):
    device_name: str = "My Desktop"


class ValidateCodeRequest(BaseModel):
    code: str


class UpdateStatusRequest(BaseModel):
    status: str


@router.post("/generate")
async def generate_code(req: GenerateCodeRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    result = desktop_bridge.generate_pairing_code(db, user["id"], req.device_name)
    return result


@router.post("/validate")
async def validate_code(req: ValidateCodeRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    try:
        result = desktop_bridge.validate_pairing(db, user["id"], req.code)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/devices")
async def list_devices(user: dict = Depends(get_current_user)):
    db = get_service_client()
    devices = desktop_bridge.list_devices(db, user["id"])
    return {"devices": devices}


@router.patch("/{device_id}")
async def update_device_status(device_id: str, req: UpdateStatusRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    try:
        result = desktop_bridge.update_device_status(db, device_id, user["id"], req.status)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{device_id}")
async def revoke_device(device_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    success = desktop_bridge.revoke_device(db, device_id, user["id"])
    if not success:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"success": True}
