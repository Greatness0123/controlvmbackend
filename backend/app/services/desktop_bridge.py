"""Desktop pairing bridge — manages secure connections to Control Desktop app."""
import secrets
import logging
from datetime import datetime, timezone
from supabase import Client

logger = logging.getLogger(__name__)


class DesktopBridge:
    def generate_pairing_code(self, db: Client, user_id: str, device_name: str) -> dict:
        """Generate a permanent pairing code locked to a device."""
        code = secrets.token_hex(4).upper()  # 8 chars

        result = db.table("paired_devices").insert({
            "user_id": user_id,
            "name": device_name,
            "pairing_code": code,
            "status": "pending",
        }).execute()

        # Also update user record for desktop app to check
        db.table("users").update({
            "remote_pairing_code": code,
        }).eq("id", user_id).execute()

        return {
            "device_id": result.data[0]["id"],
            "code": code,
        }

    def validate_pairing(self, db: Client, user_internal_id: str, code: str) -> dict:
        """Validate a pairing code entered on the web."""
        code = code.strip().upper()
        logger.info(f"[DesktopBridge] Validating code '{code}' for internal user '{user_internal_id}'")
        
        # 1. Try matching by internal user ID
        result = db.table("paired_devices").select("*")\
            .eq("user_id", user_internal_id)\
            .eq("pairing_code", code)\
            .eq("status", "pending")\
            .execute()
        
        # 2. If not found, try matching by auth_id (the UUID)
        if not result.data:
            user_data = db.table("users").select("auth_id").eq("id", user_internal_id).single().execute()
            if user_data.data:
                auth_id = user_data.data.get("auth_id")
                logger.info(f"[DesktopBridge] Internal ID fetch failed. Trying auth_id {auth_id}")
                result = db.table("paired_devices").select("*")\
                    .eq("user_id", auth_id)\
                    .eq("pairing_code", code)\
                    .eq("status", "pending")\
                    .execute()

        logger.info(f"[DesktopBridge] Found {len(result.data)} pending devices for this code")

        if not result.data:
            # Fallback: check if the code exists on the user record directly
            user_record = db.table("users").select("remote_pairing_code").eq("id", user_internal_id).execute()
            if user_record.data and user_record.data[0].get("remote_pairing_code") == code:
                logger.info("[DesktopBridge] Found code match on user record (fallback). Creating device entry.")
                # Create a device entry if it was missing
                result = db.table("paired_devices").insert({
                    "user_id": user_internal_id,
                    "name": "Control Desktop (Recovered)",
                    "pairing_code": code,
                    "status": "pending",
                }).execute()
            else:
                # Check if it exists for this user at ALL (debugging)
                all_devices = db.table("paired_devices").select("user_id,pairing_code,status").eq("pairing_code", code).execute()
                logger.info(f"[DesktopBridge] Global code check for {code}: {all_devices.data}")
                raise ValueError("Invalid pairing code or device already paired")

        device = result.data[0]

        # Mark as paired
        db.table("paired_devices").update({
            "status": "paired",
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }).eq("id", device["id"]).execute()

        db.table("users").update({
            "remote_access_enabled": True,
        }).eq("id", user_internal_id).execute()

        return {
            "device_id": device["id"],
            "name": device.get("name", "Control Desktop"),
            "status": "paired",
        }

    def list_devices(self, db: Client, user_id: str, include_revoked: bool = True) -> list:
        """List all paired devices for a user."""
        query = db.table("paired_devices").select("*").eq("user_id", user_id)
        if not include_revoked:
            query = query.neq("status", "revoked")
        
        result = query.order("created_at", desc=True).execute()
        return result.data

    def update_device_status(self, db: Client, device_id: str, user_id: str, status: str) -> dict:
        """Update the status of a device (e.g., reactivate from revoked)."""
        result = db.table("paired_devices").update({"status": status})\
            .eq("id", device_id)\
            .eq("user_id", user_id)\
            .execute()
        
        if not result.data:
            raise ValueError("Device not found")
        
        # If reactivating, ensure user remote_access_enabled is True
        if status == "paired":
            db.table("users").update({"remote_access_enabled": True}).eq("id", user_id).execute()
        
        # If revoking, check if we should disable user remote_access_enabled
        if status == "revoked":
            remaining = db.table("paired_devices").select("id")\
                .eq("user_id", user_id)\
                .eq("status", "paired")\
                .execute()
            if not remaining.data:
                db.table("users").update({"remote_access_enabled": False}).eq("id", user_id).execute()

        return result.data[0]

    def revoke_device(self, db: Client, device_id: str, user_id: str) -> bool:
        """Revoke access for a paired device."""
        return bool(self.update_device_status(db, device_id, user_id, "revoked"))


# Singleton
desktop_bridge = DesktopBridge()
