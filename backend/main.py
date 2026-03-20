"""Control Web Backend — FastAPI entry point."""
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import FRONTEND_URL, HOST, PORT
from app.routes import vm_routes, chat_routes, pair_routes, secret_routes, payment_routes
from app.services.vm_service import vm_service
from app.auth import get_service_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Control Web Backend starting...")
    
    # Background task for auto-shutdown
    async def cleanup_loop():
        db = get_service_client()
        while True:
            try:
                await vm_service.cleanup_inactive_vms(db)
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")
            await asyncio.sleep(600)  # Check every 10 minutes

    task = asyncio.create_task(cleanup_loop())
    
    yield
    
    task.cancel()
    logger.info("Control Web Backend shutting down...")


app = FastAPI(
    title="Control Web API",
    description="Backend for Control Web — VM management, AI agents, and desktop pairing",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS - Allow explicit origins and regex for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://20.164.16.171:3000"
    ],
    allow_origin_regex="https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(vm_routes.router)
app.include_router(chat_routes.router)
app.include_router(pair_routes.router)
app.include_router(secret_routes.router)
app.include_router(payment_routes.router)


@app.get("/")
def root():
    return {
        "name": "Control Web API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
