import os
from dotenv import load_dotenv

load_dotenv()

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", os.getenv("NEXT_PUBLIC_SUPABASE_URL", ""))
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""))
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE", "")

# AI
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Docker VM
VM_IMAGE_NAME = os.getenv("VM_IMAGE_NAME", "control-vm")
VM_BASE_VNC_PORT = int(os.getenv("VM_BASE_VNC_PORT", "5900"))
VM_BASE_NOVNC_PORT = int(os.getenv("VM_BASE_NOVNC_PORT", "6080"))

# Plan limits
PLAN_LIMITS = {
    "free": {"max_vms": 1, "max_sessions": 5},
    "pro": {"max_vms": 3, "max_sessions": 50},
    "enterprise": {"max_vms": 10, "max_sessions": 500},
}

# Server
HOST = os.getenv("BACKEND_HOST", "0.0.0.0")
PORT = int(os.getenv("BACKEND_PORT", "8000"))
PUBLIC_IP = os.getenv("PUBLIC_IP", "localhost")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
