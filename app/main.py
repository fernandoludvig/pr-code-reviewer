"""Aplicação FastAPI do PR Code Reviewer.

Ponto de entrada do servidor. Configura o logging e registra as rotas.
Rodar em desenvolvimento com:

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
    description="Bot que revisa Pull Requests do GitHub automaticamente.",
    version="0.1.0",
)

app.include_router(webhook_router)


@app.get("/")
async def root():
    """Rota raiz — útil para checar rapidamente se o serviço está no ar."""
    return {"service": "pr-code-reviewer", "status": "ok", "phase": 1}


@app.get("/health")
async def health():
    """Health check simples para monitoramento."""
    return {"status": "healthy"}
