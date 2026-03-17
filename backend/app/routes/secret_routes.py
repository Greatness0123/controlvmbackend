from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from pydantic import BaseModel
from app.auth import get_current_user, get_service_client
from supabase import Client
import uuid

router = APIRouter(prefix="/api/secrets", tags=["Secrets"])

class SecretBase(BaseModel):
    name: str
    service: str
    username: Optional[str] = None
    password: str
    notes: Optional[str] = None

class SecretCreate(SecretBase):
    pass

class SecretResponse(SecretBase):
    id: str
    created_at: str

@router.get("/list", response_model=List[SecretResponse])
async def list_secrets(user: dict = Depends(get_current_user)):
    svc = get_service_client()
    result = svc.table("secrets").select("*").eq("user_id", user["id"]).execute()
    return result.data

@router.post("/", response_model=SecretResponse)
async def create_secret(req: SecretCreate, user: dict = Depends(get_current_user)):
    svc = get_service_client()
    secret_data = req.dict()
    secret_data["user_id"] = user["id"]
    result = svc.table("secrets").insert(secret_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create secret")
    return result.data[0]

@router.patch("/{secret_id}", response_model=SecretResponse)
async def update_secret(secret_id: str, req: SecretCreate, user: dict = Depends(get_current_user)):
    svc = get_service_client()
    # Check ownership
    existing = svc.table("secrets").select("id").eq("id", secret_id).eq("user_id", user["id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Secret not found")
        
    result = svc.table("secrets").update(req.dict()).eq("id", secret_id).execute()
    return result.data[0]

@router.delete("/{secret_id}")
async def delete_secret(secret_id: str, user: dict = Depends(get_current_user)):
    svc = get_service_client()
    # Check ownership
    existing = svc.table("secrets").select("id").eq("id", secret_id).eq("user_id", user["id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Secret not found")
        
    svc.table("secrets").delete().eq("id", secret_id).execute()
    return {"success": True}
