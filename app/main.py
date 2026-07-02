"""PR Code Reviewer FastAPI application.

Server entry point. Configures logging and registers the routes.
Run in development with:

    uvicorn app.main:app --reload --port 8000
"""

import logging

from fastapi import FastAPI

from .webhook import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(
    title="PR Code Reviewer",
    description="Bot that automatically reviews GitHub Pull Requests.",
    version="0.1.0",
)

app.include_router(webhook_router)


@app.get("/")
async def root():
    """Root route — handy to quickly check that the service is up."""
    return {"service": "pr-code-reviewer", "status": "ok", "phase": 1}


@app.get("/health")
async def health():
    """Simple health check for monitoring."""
    return {"status": "healthy"}
