"""Rota que recebe e valida os eventos de webhook do GitHub.

Fase 1: valida a assinatura HMAC e filtra eventos `pull_request` relevantes
(opened / synchronize / reopened).
Fase 2: para esses eventos, busca o diff real do PR e chama o revisor via LLM.
Fase 3: mapeia os comentários gerados para o formato da API do GitHub e posta
uma review (event=COMMENT) de volta no PR. Tudo em background.
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

# Emoji por severidade, usado no corpo de cada comentário postado no PR.
SEVERIDADE_EMOJI = {"alta": "🔴", "media": "🟡", "baixa": "🟢"}


def _to_github_comments(comentarios: list[dict]) -> list[dict]:
    """Converte os comentários do LLM para o formato da API de reviews do GitHub.

    Formata o corpo com o emoji de severidade + o texto. Descarta itens sem
    arquivo/linha válidos (não dá para ancorar em linha), que serão apenas
    logados.
    """
    gh_comments: list[dict] = []
    for c in comentarios:
        path = c.get("arquivo")
        linha = c.get("linha_aproximada")
        if not path or linha is None:
            continue
        try:
            line = int(linha)
        except (TypeError, ValueError):
            continue
        emoji = SEVERIDADE_EMOJI.get(c.get("severidade"), "⚪")
        corpo = f"{emoji} **[{c.get('severidade', 'baixa')}]** {c.get('comentario', '')}"
        gh_comments.append({"path": path, "line": line, "side": "RIGHT", "body": corpo})
    return gh_comments


async def process_pull_request(
    owner: str, repo: str, pull_number: int, pr_title: str, head_sha: str | None = None
) -> None:
    """Busca o diff, roda a revisão via LLM e posta a review (em background).

    Roda DEPOIS da resposta 200 já ter sido enviada ao GitHub, então pode
    demorar o quanto for sem estourar o timeout do webhook. Nunca levanta
    exceção para fora: qualquer erro é logado.
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

    # commit_id necessário para ancorar os comentários de linha. Prefere o SHA
    # que já veio no webhook; se faltar, busca via API.
    commit_id = head_sha or await github_client.get_pull_request_head_sha(
        owner, repo, pull_number
    )

    # Caso 1: nenhum problema encontrado → review positiva simples, sem linhas.
    if not comentarios:
        logger.info("Revisão do PR #%s: nenhum comentário gerado.", pull_number)
        resultado = await github_client.submit_pr_review(
            owner,
            repo,
            pull_number,
            comments=[],
            commit_id=commit_id,
            body="✅ Revisão automática: nenhum problema crítico encontrado.",
        )
        logger.info("Postagem da review (PR #%s): %s", pull_number, resultado)
        return

    # Caso 2: há comentários → loga cada um e posta como review por linha.
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

    gh_comments = _to_github_comments(comentarios)

    # Se NENHUM comentário pôde ser ancorado em linha, não postamos uma review
    # vazia (que descartaria os achados): mandamos tudo no corpo da review.
    if not gh_comments:
        logger.warning(
            "PR #%s: nenhum dos %d comentário(s) tinha arquivo/linha válidos; "
            "postando como review geral no corpo.",
            pull_number,
            len(comentarios),
        )
        linhas = ["🤖 Revisão automática de código", ""]
        for c in comentarios:
            emoji = SEVERIDADE_EMOJI.get(c.get("severidade"), "⚪")
            linhas.append(
                f"- {emoji} **[{c.get('severidade', 'baixa')}]** "
                f"{c.get('arquivo')}:{c.get('linha_aproximada')} — "
                f"{c.get('comentario', '')}"
            )
        resultado = await github_client.submit_pr_review(
            owner,
            repo,
            pull_number,
            comments=[],
            commit_id=commit_id,
            body="\n".join(linhas),
        )
        logger.info("Postagem da review (PR #%s): %s", pull_number, resultado)
        return

    if len(gh_comments) < len(comentarios):
        logger.warning(
            "PR #%s: %d comentário(s) sem arquivo/linha válidos foram descartados "
            "da postagem por linha (os demais foram postados).",
            pull_number,
            len(comentarios) - len(gh_comments),
        )

    resultado = await github_client.submit_pr_review(
        owner, repo, pull_number, comments=gh_comments, commit_id=commit_id
    )
    logger.info("Postagem da review (PR #%s): %s", pull_number, resultado)


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
    head_sha = pr.get("head", {}).get("sha")

    logger.info(
        "PR #%s | ação=%s | repo=%s | autor=%s | título=%s",
        number,
        action,
        repo.get("full_name"),
        pr.get("user", {}).get("login"),
        title,
    )

    # Agenda a busca do diff + revisão via LLM + postagem para depois do 200.
    if owner and repo_name and number is not None:
        background_tasks.add_task(
            process_pull_request, owner, repo_name, number, title or "", head_sha
        )
    else:
        logger.warning(
            "Payload sem owner/repo/number suficientes; revisão não agendada."
        )

    return {"msg": "ok", "pr": number, "action": action}
