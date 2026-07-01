"""Rota que recebe e valida os eventos de webhook do GitHub.

Fase 1: valida a assinatura HMAC, filtra eventos `pull_request` relevantes
(opened / synchronize / reopened) e apenas loga os dados no console.
Ainda NÃO busca o diff nem chama LLM.
"""

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from .config import settings

logger = logging.getLogger("pr_code_reviewer.webhook")

router = APIRouter()

# Ações de pull_request que nos interessam nesta fase.
RELEVANT_ACTIONS = {"opened", "synchronize", "reopened"}


def verify_signature(payload_body: bytes, signature_header: str | None) -> None:
    """Valida a assinatura HMAC-SHA256 enviada pelo GitHub.

    O GitHub assina o corpo bruto da requisição com o `GITHUB_WEBHOOK_SECRET`
    e envia o resultado no header `X-Hub-Signature-256` no formato
    `sha256=<hexdigest>`. Recalculamos e comparamos de forma segura contra
    ataques de timing.

    Levanta HTTPException se a configuração estiver ausente ou a assinatura
    for inválida.
    """
    if not settings.GITHUB_WEBHOOK_SECRET:
        # Sem segredo configurado não há como validar — falha fechada.
        logger.error("GITHUB_WEBHOOK_SECRET não configurado; recusando webhook.")
        raise HTTPException(status_code=500, detail="Webhook secret não configurado")

    if not signature_header:
        raise HTTPException(
            status_code=401, detail="Header X-Hub-Signature-256 ausente"
        )

    expected = "sha256=" + hmac.new(
        key=settings.GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Assinatura inválida")


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Recebe eventos do GitHub Webhook.

    Retorna 200 rapidamente (o GitHub espera resposta em poucos segundos).
    Todo processamento pesado (diff, LLM, comentários) virá nas próximas fases.
    """
    # Importante: assinar o corpo BRUTO, antes de qualquer parse de JSON.
    body = await request.body()
    verify_signature(body, x_hub_signature_256)

    payload = await request.json()

    # Evento de teste disparado ao criar o webhook no GitHub.
    if x_github_event == "ping":
        logger.info("Ping recebido do GitHub webhook.")
        return {"msg": "pong"}

    if x_github_event != "pull_request":
        logger.info("Evento ignorado: %s", x_github_event)
        return {"msg": f"evento '{x_github_event}' ignorado"}

    action = payload.get("action")
    if action not in RELEVANT_ACTIONS:
        logger.info("Ação de pull_request ignorada: %s", action)
        return {"msg": f"ação '{action}' ignorada"}

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    logger.info(
        "PR #%s | ação=%s | repo=%s | autor=%s | título=%s",
        pr.get("number"),
        action,
        repo.get("full_name"),
        pr.get("user", {}).get("login"),
        pr.get("title"),
    )

    # Próximas fases: buscar o diff (github_client.get_pull_request_diff)
    # e enviar ao LLM para gerar comentários de revisão.
    return {"msg": "ok", "pr": pr.get("number"), "action": action}
