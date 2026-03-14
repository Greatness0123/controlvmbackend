"""Desktop pairing bridge — manages secure connections to Control Desktop app."""
import secrets
import logging
from datetime import datetime, timedelta, timezone
from supabase import Client

logger = logging.getLogger(__name__)


class DesktopBridge:
    def generate_pairing_code(self, db: Client, user_id: str, device_name: str) -> dict:
        """Generate a secure 8-char pairing code for a device."""
        code = secrets.token_hex(4).upper()  # 8 chars
        expires = datetime.now(timezone.utc) + timedelta(minutes=10)

        result = db.table("paired_devices").insert({
            "user_id": user_id,
            "name": device_name,
            "pairing_code": code,
            "pairing_expires": expires.isoformat(),
            "status": "pending",
        }).execute()

        # Also update user record for desktop app to check
        db.table("users").update({
            "remote_pairing_code": code,
            "remote_pairing_expires": expires.isoformat(),
        }).eq("id", user_id).execute()

        return {
            "device_id": result.data[0]["id"],
            "code": code,
            "expires_at": expires.isoformat(),
        }

    def validate_pairing(self, db: Client, user_id: str, code: str) -> dict:
        """Validate a pairing code entered on the web."""
        result = db.table("paired_devices").select("*")\
            .eq("user_id", user_id)\
            .eq("pairing_code", code)\
            .eq("status", "pending")\
            .execute()

        if not result.data:
            raise ValueError("Invalid or expired pairing code")

        device = result.data[0]
        expires = datetime.fromisoformat(device["pairing_expires"].replace("Z", "+00:00"))
        
        if datetime.now(timezone.utc) > expires:
            db.table("paired_devices").update({"status": "revoked"}).eq("id", device["id"]).execute()
            raise ValueError("Pairing code has expired")

        # Mark as paired
        db.table("paired_devices").update({
            "status": "paired",
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }).eq("id", device["id"]).execute()

        db.table("users").update({
            "remote_access_enabled": True,
        }).eq("id", user_id).execute()

        return {
            "device_id": device["id"],
            "name": device["name"],
            "status": "paired",
        }

    def list_devices(self, db: Client, user_id: str) -> list:
        """List all paired devices for a user."""
        result = db.table("paired_devices").select("*")\
            .eq("user_id", user_id)\
            .neq("status", "revoked")\
            .order("created_at", desc=True)\
            .execute()
        return result.data

    def revoke_device(self, db: Client, device_id: str, user_id: str) -> bool:
        """Revoke access for a paired device."""
        result = db.table("paired_devices").update({"status": "revoked"})\
            .eq("id", device_id)\
            .eq("user_id", user_id)\
            .execute()

        # Check if user has any remaining paired devices
        remaining = db.table("paired_devices").select("id")\
            .eq("user_id", user_id)\
            .eq("status", "paired")\
            .execute()

        if not remaining.data:
            db.table("users").update({
                "remote_access_enabled": False,
                "remote_pairing_code": None,
            }).eq("id", user_id).execute()

        return bool(result.data)


# Singleton
desktop_bridge = DesktopBridge()
