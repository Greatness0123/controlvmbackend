"""Supabase JWT auth middleware for FastAPI."""
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client
from app.config import SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY

security = HTTPBearer()

# Singleton client to avoid repeated initialization bugs
_client = None
def get_supabase_client():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _client

# Service-role client for admin operations (falls back to anon client if key not set)
_service_client = None
def get_service_client():
    global _service_client
    if _service_client is None:
        key = SUPABASE_SERVICE_KEY if SUPABASE_SERVICE_KEY else SUPABASE_ANON_KEY
        _service_client = create_client(SUPABASE_URL, key)
    return _service_client

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Validate Supabase JWT and return user info."""
    token = credentials.credentials
    try:
        client = get_supabase_client()
        # Verify with Supabase Auth
        user_response = client.auth.get_user(token)
        if not user_response or not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        auth_id = str(user_response.user.id)
        
        # Get our internal user record using service client for full access
        svc = get_service_client()
        result = svc.table("users").select("*").eq("auth_id", auth_id).execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="User not found in database")
        
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger("app.auth").error(f"Auth failed: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Auth failed: {str(e)}")
