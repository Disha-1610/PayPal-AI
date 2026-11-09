from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import get_logger, setup_logging

setup_logging(settings.log_level)
logger = get_logger(__name__)

app = FastAPI(title=settings.app_name, debug=settings.debug)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}
