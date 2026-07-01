"""Rota que recebe e valida os eventos de webhook do GitHub.

Fase 1: valida a assinatura HMAC e filtra eventos `pull_request` relevantes
(opened / synchronize / reopened).
Fase 2: para esses eventos, busca o diff real do PR e chama o revisor via LLM
em background, logando os comentários gerados. Ainda NÃO posta nada de volta
no GitHub — isso fica para a Fase 3.
"""

import asyncio
import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from . import github_client, llm_reviewer
from .config import settings

logger = logging.getLogger("pr_code_reviewer.webhook")

router = APIRouter()

# Ações de pull_request que nos interessam nesta fase.
RELEVANT_ACTIONS = {"opened", "synchronize", "reopened"}


async def process_pull_request(
    owner: str, repo: str, pull_number: int, pr_title: str
) -> None:
    """Busca o diff do PR e roda a revisão via LLM (executado em background).

    Roda DEPOIS da resposta 200 já ter sido enviada ao GitHub, então pode
    demorar o quanto for (chamada à API do GitHub + LLM) sem estourar o timeout
    do webhook. Nunca levanta exceção para fora: qualquer erro é logado.
    """
    try:
        diff = await github_client.get_pull_request_diff(owner, repo, pull_number)
    except Exception:
        logger.exception(
            "Falha ao buscar o diff do PR #%s (%s/%s).", pull_number, owner, repo
        )
        return

    logger.info(
        "Diff do PR #%s obtido (%d caracteres). Iniciando revisão via LLM...",
        pull_number,
        len(diff),
    )

    # review_diff é síncrono (cliente da OpenAI bloqueante); rodamos numa thread
    # para não bloquear o event loop do FastAPI.
    comentarios = await asyncio.to_thread(llm_reviewer.review_diff, diff, pr_title)

    if not comentarios:
        logger.info("Revisão do PR #%s: nenhum comentário gerado.", pull_number)
        return

    logger.info(
        "Revisão do PR #%s: %d comentário(s) gerado(s):", pull_number, len(comentarios)
    )
    for i, c in enumerate(comentarios, start=1):
        logger.info(
            "  [%d] %s:%s | severidade=%s | %s",
            i,
            c.get("arquivo"),
            c.get("linha_aproximada"),
            c.get("severidade"),
            c.get("comentario"),
        )


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
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Recebe eventos do GitHub Webhook.

    Retorna 200 rapidamente (o GitHub espera resposta em poucos segundos). O
    trabalho pesado (buscar o diff + revisão via LLM) é agendado em background
    para não bloquear a resposta ao GitHub.
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

    number = pr.get("number")
    title = pr.get("title")
    owner = repo.get("owner", {}).get("login")
    repo_name = repo.get("name")

    logger.info(
        "PR #%s | ação=%s | repo=%s | autor=%s | título=%s",
        number,
        action,
        repo.get("full_name"),
        pr.get("user", {}).get("login"),
        title,
    )

    # Agenda a busca do diff + revisão via LLM para depois da resposta 200.
    if owner and repo_name and number is not None:
        background_tasks.add_task(
            process_pull_request, owner, repo_name, number, title or ""
        )
    else:
        logger.warning(
            "Payload sem owner/repo/number suficientes; revisão não agendada."
        )

    return {"msg": "ok", "pr": number, "action": action}
