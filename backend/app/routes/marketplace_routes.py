import json
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Any
from app.auth import get_current_user, get_service_client

router = APIRouter(prefix="/api/marketplace", tags=["Marketplace"])

class MarketplacePublishRequest(BaseModel):
    workflow_id: str
    price: float = 0
    description: str = ""
    category: str = "productivity"

class CommentRequest(BaseModel):
    content: str

@router.get("/list")
async def list_marketplace(category: Optional[str] = None):
    db = get_service_client()
    query = db.table("marketplace_listings").select(
        "*, users!marketplace_listings_author_id_fkey(first_name, last_name)"
    )
    
    if category and category != 'all':
        query = query.eq("category", category)
    
    result = query.order("stars", desc=True).execute()
    return {"workflows": result.data}

@router.get("/{listing_id}")
async def get_listing(listing_id: str):
    db = get_service_client()
    
    listing = db.table("marketplace_listings").select(
        "*, users!marketplace_listings_author_id_fkey(first_name, last_name)"
    ).eq("id", listing_id).execute()
    
    if not listing.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    comments = db.table("marketplace_comments").select("*")\
        .eq("listing_id", listing_id)\
        .order("created_at", desc=True)\
        .execute()
    
    return {"workflow": listing.data[0], "comments": comments.data}

@router.post("/publish")
async def publish_workflow(req: MarketplacePublishRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    
    workflow = db.table("workflows").select("*")\
        .eq("id", req.workflow_id)\
        .eq("user_id", user["id"])\
        .execute()
    
    if not workflow.data:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    listing_data = {
        "id": str(uuid.uuid4()),
        "author_id": user["id"],
        "workflow_id": req.workflow_id,
        "workflow_name": workflow.data[0]["name"],
        "workflow_data": workflow.data[0],
        "price": req.price,
        "description": req.description,
        "category": req.category,
        "stars": 0,
        "downloads": 0,
        "status": "active"
    }
    
    result = db.table("marketplace_listings").insert(listing_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to publish workflow")
    
    return {"listing": result.data[0]}

@router.post("/{listing_id}/purchase")
async def purchase_workflow(listing_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    
    listing = db.table("marketplace_listings").select("*")\
        .eq("id", listing_id)\
        .eq("status", "active")\
        .execute()
    
    if not listing.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    listing_item = listing.data[0]
    
    if listing_item["price"] > 0:
        purchases = db.table("marketplace_purchases").select("id")\
            .eq("listing_id", listing_id)\
            .eq("buyer_id", user["id"])\
            .execute()
        
        if purchases.data:
            raise HTTPException(status_code=400, detail="Already purchased")
    
    purchase_data = {
        "id": str(uuid.uuid4()),
        "listing_id": listing_id,
        "buyer_id": user["id"],
        "price": listing_item["price"]
    }
    
    db.table("marketplace_purchases").insert(purchase_data).execute()
    
    db.table("marketplace_listings").update({
        "downloads": listing_item["downloads"] + 1
    }).eq("id", listing_id).execute()
    
    if listing_item["price"] == 0:
        workflow_data = listing_item["workflow_data"].copy() if listing_item["workflow_data"] else {}
        workflow_data["id"] = str(uuid.uuid4())
        workflow_data["user_id"] = user["id"]
        workflow_data.pop("id", None)
        
        db.table("workflows").insert(workflow_data).execute()
    
    return {"success": True, "message": "Workflow purchased"}

@router.post("/{listing_id}/star")
async def star_listing(listing_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    
    listing = db.table("marketplace_listings").select("stars").eq("id", listing_id).execute()
    if not listing.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    existing = db.table("marketplace_stars").select("id")\
        .eq("listing_id", listing_id)\
        .eq("user_id", user["id"])\
        .execute()
    
    if existing.data:
        db.table("marketplace_stars").delete().eq("id", existing.data[0]["id"]).execute()
        new_stars = max(0, listing.data[0]["stars"] - 1)
    else:
        db.table("marketplace_stars").insert({
            "id": str(uuid.uuid4()),
            "listing_id": listing_id,
            "user_id": user["id"]
        }).execute()
        new_stars = listing.data[0]["stars"] + 1
    
    db.table("marketplace_listings").update({"stars": new_stars}).eq("id", listing_id).execute()
    
    return {"stars": new_stars}

@router.delete("/{listing_id}/star")
async def unstar_listing(listing_id: str, user: dict = Depends(get_current_user)):
    db = get_service_client()
    
    listing = db.table("marketplace_listings").select("stars").eq("id", listing_id).execute()
    if not listing.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    existing = db.table("marketplace_stars").select("id")\
        .eq("listing_id", listing_id)\
        .eq("user_id", user["id"])\
        .execute()
    
    new_stars = listing.data[0]["stars"]
    if existing.data:
        db.table("marketplace_stars").delete().eq("id", existing.data[0]["id"]).execute()
        new_stars = max(0, new_stars - 1)
        db.table("marketplace_listings").update({"stars": new_stars}).eq("id", listing_id).execute()
    
    return {"stars": new_stars}

@router.get("/{listing_id}/comments")
async def get_comments(listing_id: str):
    db = get_service_client()
    result = db.table("marketplace_comments").select(
        "*, users!marketplace_comments_author_id_fkey(first_name, last_name)"
    )\
        .eq("listing_id", listing_id)\
        .order("created_at", desc=True)\
        .execute()
    
    return {"comments": result.data}

@router.post("/{listing_id}/comments")
async def add_comment(listing_id: str, req: CommentRequest, user: dict = Depends(get_current_user)):
    db = get_service_client()
    
    listing = db.table("marketplace_listings").select("id").eq("id", listing_id).execute()
    if not listing.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    
    comment_data = {
        "id": str(uuid.uuid4()),
        "listing_id": listing_id,
        "author_id": user["id"],
        "content": req.content
    }
    
    result = db.table("marketplace_comments").insert(comment_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to add comment")
    
    return {"comment": result.data[0]}

@router.get("/my-listings")
async def my_listings(user: dict = Depends(get_current_user)):
    db = get_service_client()
    result = db.table("marketplace_listings").select("*")\
        .eq("author_id", user["id"])\
        .order("created_at", desc=True)\
        .execute()
    
    return {"listings": result.data}

@router.get("/my-purchases")
async def my_purchases(user: dict = Depends(get_current_user)):
    db = get_service_client()
    result = db.table("marketplace_purchases").select(
        "*, marketplace_listings!inner(workflow_name, workflow_data)"
    )\
        .eq("buyer_id", user["id"])\
        .order("created_at", desc=True)\
        .execute()
    
    return {"purchases": result.data}