import docker
import random
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional
from supabase import Client
from app.config import VM_IMAGE_NAME, VM_BASE_NOVNC_PORT, PLAN_LIMITS, PUBLIC_IP

logger = logging.getLogger(__name__)

class VMService:
    def __init__(self):
        try:
            self.docker_client = docker.from_env()
            logger.info("Docker client connected")
        except Exception as e:
            logger.warning(f"Docker not available: {e}")
            self.docker_client = None

    def _get_free_port(self, base: int = 6080) -> int:
        """Find an available port starting from base."""
        used_ports = set()
        if self.docker_client:
            for container in self.docker_client.containers.list():
                for port_info in container.ports.values():
                    if port_info:
                        for p in port_info:
                            used_ports.add(int(p["HostPort"]))
        port = base
        while port in used_ports:
            port += 1
        return port

    async def create_vm(self, db: Client, user_id: str, name: str, user_plan: str = "free") -> dict:
        """Create a new VM container for a user."""
        if not self.docker_client:
            raise RuntimeError("Docker is not available on this system")

        # Check plan limits
        limits = PLAN_LIMITS.get(user_plan, PLAN_LIMITS["free"])
        existing = db.table("virtual_machines").select("id").eq("user_id", user_id).execute()
        if len(existing.data) >= limits["max_vms"]:
            raise ValueError(f"VM limit reached ({limits['max_vms']} for {user_plan} plan)")

        novnc_port = self._get_free_port(VM_BASE_NOVNC_PORT)
        vnc_port = self._get_free_port(5900)
        agent_port = self._get_free_port(8080)

        try:
            container = self.docker_client.containers.run(
                VM_IMAGE_NAME,
                detach=True,
                name=f"control-vm-{user_id}-{random.randint(1000,9999)}",
                ports={
                    "6080/tcp": novnc_port,
                    "5900/tcp": vnc_port,
                    "8080/tcp": agent_port,
                },
                environment={
                    "RESOLUTION": "1920x1080x24",
                    "VNC_PASSWORD": "",
                    "AUTOLOGIN": "yes",
                    "USER": "controluser",
                },
                mem_limit="2g",
                cpu_period=100000,
                cpu_quota=200000,  # 2 CPUs
                restart_policy={"Name": "unless-stopped"},
                labels={
                    "control.user_id": user_id,
                    "control.type": "vm",
                },
            )

            # Save to database
            vm_data = {
                "user_id": user_id,
                "name": name,
                "status": "running",
                "container_id": container.id,
                "vnc_port": vnc_port,
                "novnc_port": novnc_port,
                "agent_port": agent_port,
                "instance_url": f"http://{PUBLIC_IP}:{novnc_port}",
                "last_active_at": datetime.now(timezone.utc).isoformat(),
            }
            result = db.table("virtual_machines").insert(vm_data).execute()
            
            # Disable screensaver and power management in background
            async def _disable_power_mgmt(cont):
                await asyncio.sleep(10) # wait for X server
                try:
                    # DISPLAY :1 is standard for this image
                    cont.exec_run("xset s off", user="controluser", environment={"DISPLAY": ":1"})
                    cont.exec_run("xset -dpms", user="controluser", environment={"DISPLAY": ":1"})
                    cont.exec_run("pkill xscreensaver", user="controluser")
                except Exception as e:
                    logger.warning(f"Failed to disable power mgmt on VM: {e}")
            
            asyncio.create_task(_disable_power_mgmt(container))

            return result.data[0]

        except docker.errors.ImageNotFound:
            raise RuntimeError(
                f"VM image '{VM_IMAGE_NAME}' not found. "
                f"Build it with: docker build -t {VM_IMAGE_NAME} ./vm"
            )
        except Exception as e:
            logger.error(f"Failed to create VM: {e}")
            raise

    async def start_vm(self, db: Client, vm_id: str, user_id: str) -> dict:
        """Start a stopped VM."""
        vm = db.table("virtual_machines").select("*").eq("id", vm_id).eq("user_id", user_id).execute()
        if not vm.data:
            raise ValueError("VM not found")
        
        vm_data = vm.data[0]
        if not self.docker_client:
            raise RuntimeError("Docker not available")

        try:
            container = self.docker_client.containers.get(vm_data["container_id"])
            container.start()
            
            # Wait briefly and verify container is actually running
            import time
            for _ in range(5):
                time.sleep(0.5)
                container.reload()
                if container.status == "running":
                    break
            
            actual_status = "running" if container.status == "running" else "starting"
            db.table("virtual_machines").update({"status": actual_status}).eq("id", vm_id).execute()
            return {**vm_data, "status": actual_status}
        except docker.errors.NotFound:
            db.table("virtual_machines").update({"status": "stopped"}).eq("id", vm_id).execute()
            raise ValueError("Container not found — it may have been removed")

    async def stop_vm(self, db: Client, vm_id: str, user_id: str) -> dict:
        """Stop a running VM."""
        logger.info(f"Stopping VM {vm_id} requested by user {user_id}")
        vm = db.table("virtual_machines").select("*").eq("id", vm_id).eq("user_id", user_id).execute()
        if not vm.data:
            raise ValueError("VM not found")

        vm_data = vm.data[0]
        if not self.docker_client:
            raise RuntimeError("Docker not available")

        try:
            container = self.docker_client.containers.get(vm_data["container_id"])
            container.stop(timeout=10)
            db.table("virtual_machines").update({"status": "stopped"}).eq("id", vm_id).execute()
            return {**vm_data, "status": "stopped"}
        except docker.errors.NotFound:
            db.table("virtual_machines").update({"status": "stopped"}).eq("id", vm_id).execute()
            return {**vm_data, "status": "stopped"}

    async def destroy_vm(self, db: Client, vm_id: str, user_id: str) -> bool:
        """Destroy a VM and its container."""
        vm = db.table("virtual_machines").select("*").eq("id", vm_id).eq("user_id", user_id).execute()
        if not vm.data:
            raise ValueError("VM not found")

        vm_data = vm.data[0]
        if self.docker_client and vm_data.get("container_id"):
            try:
                container = self.docker_client.containers.get(vm_data["container_id"])
                container.remove(force=True)
            except docker.errors.NotFound:
                pass

        db.table("virtual_machines").delete().eq("id", vm_id).execute()
        return True

    async def list_vms(self, db: Client, user_id: str) -> list:
        """List all VMs for a user, refreshing status from Docker."""
        result = db.table("virtual_machines").select("*").eq("user_id", user_id).order("created_at").execute()
        vms = result.data

        if self.docker_client:
            for vm in vms:
                if vm.get("container_id"):
                    try:
                        container = self.docker_client.containers.get(vm["container_id"])
                        # Handle various Docker states
                        if container.status == "running":
                            actual_status = "running"
                        elif container.status in ["restarting", "created", "starting"]:
                            actual_status = "starting"
                        else:
                            actual_status = "stopped"

                        if actual_status != vm.get("status"):
                            db.table("virtual_machines").update({"status": actual_status}).eq("id", vm["id"]).execute()
                            vm["status"] = actual_status
                    except docker.errors.NotFound:
                        if vm.get("status") != "stopped":
                            db.table("virtual_machines").update({"status": "stopped"}).eq("id", vm["id"]).execute()
                            vm["status"] = "stopped"

        return vms
    
    async def get_vm_stats(self, vm_id: str, container_id: str) -> dict:
        """Get live resource stats from a running container."""
        if not self.docker_client:
            return {"cpu": 0, "memory": 0, "memory_limit": 0}
        try:
            container = self.docker_client.containers.get(container_id)
            stats = container.stats(stream=False)
            
            # CPU calculation
            cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
            system_delta = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
            num_cpus = stats["cpu_stats"].get("online_cpus", 1)
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100 if system_delta > 0 else 0

            # Memory
            mem_usage = stats["memory_stats"].get("usage", 0)
            mem_limit = stats["memory_stats"].get("limit", 0)

            # Storage (Approximate via container filesystem)
            storage_used = 0
            storage_limit = 20 * 1024 # default 20GB in MB
            try:
                # Run df -m / inside container
                df_res = container.exec_run("df -m /")
                if df_res.exit_code == 0:
                    lines = df_res.output.decode().split('\n')
                    if len(lines) > 1:
                        parts = lines[1].split()
                        if len(parts) >= 3:
                            storage_limit = float(parts[1])
                            storage_used = float(parts[2])
            except:
                pass

            return {
                "cpu": round(cpu_percent, 1),
                "memory": round(mem_usage / (1024 * 1024), 1),  # MB
                "memory_limit": round(mem_limit / (1024 * 1024), 1),
                "storage_used": round(float(storage_used or 0) / 1024, 1),
                "storage_limit": round(float(storage_limit or 0) / 1024, 1),
            }
        except Exception:
            return {"cpu": 0, "memory": 0, "memory_limit": 0}

    async def update_activity(self, db: Client, vm_id: str):
        """Update last active timestamp for a VM."""
        try:
            db.table("virtual_machines").update({
                "last_active_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", vm_id).execute()
        except Exception as e:
            logger.error(f"Failed to update VM activity: {e}")

    async def cleanup_inactive_vms(self, db: Client):
        """Shutdown VMs that have been inactive for more than 30 minutes."""
        try:
            # Get all running VMs
            result = db.table("virtual_machines").select("*").eq("status", "running").execute()
            vms = result.data
            
            now = datetime.now(timezone.utc)
            for vm in vms:
                last_active_str = vm.get("last_active_at")
                if not last_active_str:
                    continue
                
                last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
                inactive_seconds = (now - last_active).total_seconds()
                
                if inactive_seconds > 1800: # 30 minutes
                    logger.info(f"Auto-shutting down inactive VM {vm['id']} (Inactive for {inactive_seconds}s)")
                    try:
                        await self.stop_vm(db, vm["id"], vm["user_id"])
                    except Exception as e:
                        logger.error(f"Failed to auto-stop VM {vm['id']}: {e}")
        except Exception as e:
            logger.error(f"Error during VM cleanup: {e}")

    def get_vm_password_info(self):
        """Research/utility: how to check password from console."""
        return {
            "method": "Check environment variables OR VNC config files",
            "console_command": "env | grep VNC_PASSWORD",
            "config_file": "/home/ubuntu/.vnc/config",
            "note": "Control VMs are now configured with VNC_PASSWORD='' for passwordless access."
        }


# Singleton
vm_service = VMService()
