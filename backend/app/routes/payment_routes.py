import hmac
import hashlib
import logging
from fastapi import APIRouter, Request, Header, HTTPException
from app.auth import get_service_client
from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY, FLUTTERWAVE_SECRET_HASH

router = APIRouter(prefix="/api/webhooks", tags=["Payments"])
logger = logging.getLogger(__name__)

@router.post("/flutterwave")
async def flutterwave_webhook(request: Request, verif_hash: str = Header(None, alias="verif-hash")):

    if not verif_hash or verif_hash != FLUTTERWAVE_SECRET_HASH:
        logger.warning(f"Unauthorized webhook attempt with hash: {verif_hash}")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        logger.error("Failed to parse webhook JSON payload")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"Received Flutterwave webhook: {payload.get('event') or payload.get('status')}")

    if payload.get("status") == "successful" or payload.get("event") == "charge.completed":
        data = payload.get("data", payload) # Event based vs direct status
        
        transaction_id = data.get("id")
        tx_ref = data.get("tx_ref", "")
        amount = data.get("amount")
        user_email = data.get("customer", {}).get("email")

        meta = data.get("meta", {})
        user_id = meta.get("userId")
        plan = meta.get("plan")

        if not plan:
            if amount == 49: plan = "pro"
            elif amount == 199: plan = "master"
            else: plan = "free"
            
        logger.info(f"Processing successful payment for {user_email}, plan: {plan}, ref: {tx_ref}")

        db = get_service_client()

        try:
            db.table("users").update({"plan": plan}).eq("email", user_email).execute()
        except Exception as e:
            logger.error(f"Error updating public.users: {e}")

        if user_id:
            try:

                db.auth.admin.update_user_by_id(
                    user_id,
                    attributes={"user_metadata": {"plan": plan}}
                )
                logger.info(f"Updated auth metadata for user {user_id}")
            except Exception as e:
                logger.error(f"Error updating auth metadata: {e}")
        
        return {"status": "success", "message": f"Plan updated to {plan}"}
    
    logger.info(f"Webhook ignored: {payload.get('status') or payload.get('event')}")
    return {"status": "ignored"}
