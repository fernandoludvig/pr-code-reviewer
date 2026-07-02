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
from .dedup_cache import TTLCache

logger = logging.getLogger("pr_code_reviewer.webhook")

router = APIRouter()

# Ações de pull_request que nos interessam nesta fase.
RELEVANT_ACTIONS = {"opened", "synchronize", "reopened"}

# Cache em memória de commits já revisados (head.sha), para não reprocessar o
# mesmo evento em pushes rápidos e gastar chamadas ao LLM à toa. Reseta ao
# reiniciar o servidor (aceitável para portfólio; ver README > Limitações).
_processed_commits = TTLCache(ttl_seconds=3600)

# Emoji por severidade, usado no corpo de cada comentário postado no PR.
SEVERIDADE_EMOJI = {"alta": "🔴", "media": "🟡", "baixa": "🟢"}

# Ordem de severidade para comparação (baixa < media < alta).
SEVERIDADE_ORDEM = {"baixa": 0, "media": 1, "alta": 2}


def _filter_by_severity(comentarios: list[dict], min_severity: str) -> list[dict]:
    """Mantém apenas comentários com severidade >= min_severity.

    Severidade desconhecida é tratada como a mais baixa (0). Um min_severity
    inválido cai para "media".
    """
    min_rank = SEVERIDADE_ORDEM.get(
        (min_severity or "").lower(), SEVERIDADE_ORDEM["media"]
    )
    return [
        c
        for c in comentarios
        if SEVERIDADE_ORDEM.get(c.get("severidade"), 0) >= min_rank
    ]


def _dedupe_comments(comentarios: list[dict]) -> list[dict]:
    """Remove comentários duplicados na mesma (arquivo, linha aproximada).

    O LLM às vezes gera dois comentários para o mesmo ponto com textos um pouco
    diferentes. Heurística simples: se caírem na mesma (arquivo, linha), mantém
    apenas o de MAIOR severidade e descarta o outro. Preserva a ordem original.
    """
    melhor: dict[tuple, dict] = {}
    for c in comentarios:
        chave = (c.get("arquivo"), c.get("linha_aproximada"))
        atual = melhor.get(chave)
        if atual is None or SEVERIDADE_ORDEM.get(
            c.get("severidade"), 0
        ) > SEVERIDADE_ORDEM.get(atual.get("severidade"), 0):
            melhor[chave] = c
    return list(melhor.values())


def _split_comments(
    comentarios: list[dict], valid_lines: dict[str, set[int]]
) -> tuple[list[dict], list[str]]:
    """Separa os comentários do LLM em ancoráveis × overflow.

    Retorna (ancorados, overflow):
    - ancorados: comentários no formato da API do GitHub cujo (arquivo, linha)
      existe no diff (`side="RIGHT"`) — postados na linha.
    - overflow: linhas markdown pré-formatadas dos comentários que NÃO podem ser
      ancorados (sem arquivo/linha, ou linha fora do diff) — vão para o corpo,
      em vez de serem descartados.
    """
    ancorados: list[dict] = []
    overflow: list[str] = []
    for c in comentarios:
        severidade = c.get("severidade", "baixa")
        emoji = SEVERIDADE_EMOJI.get(severidade, "⚪")
        texto = c.get("comentario", "")
        path = c.get("arquivo")
        linha = c.get("linha_aproximada")

        line: int | None = None
        if path and linha is not None:
            try:
                line = int(linha)
            except (TypeError, ValueError):
                line = None

        if line is not None and line in valid_lines.get(path, set()):
            ancorados.append(
                {
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": f"{emoji} **[{severidade}]** {texto}",
                }
            )
        else:
            overflow.append(f"- {emoji} **[{severidade}]** {path}:{linha} — {texto}")
    return ancorados, overflow


async def process_pull_request(
    owner: str, repo: str, pull_number: int, pr_title: str, head_sha: str | None = None
) -> None:
    """Busca o diff, roda a revisão via LLM e posta a review (em background).

    Roda DEPOIS da resposta 200 já ter sido enviada ao GitHub, então pode
    demorar o quanto for sem estourar o timeout do webhook. Nunca levanta
    exceção para fora: qualquer erro é logado.
    """
    # Deduplicação de evento: se este commit (head.sha) já foi revisado
    # recentemente, pula — evita gasto duplicado de tokens em pushes rápidos.
    if head_sha:
        chave = f"{owner}/{repo}#{pull_number}@{head_sha}"
        if _processed_commits.seen(chave):
            logger.info(
                "PR #%s: commit %s já revisado, ignorando.",
                pull_number,
                head_sha[:8],
            )
            return

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

    # Filtro por severidade mínima (config MIN_SEVERITY).
    total_bruto = len(comentarios)
    comentarios = _filter_by_severity(comentarios, settings.MIN_SEVERITY)
    if total_bruto != len(comentarios):
        logger.info(
            "PR #%s: filtro de severidade (>= %s) manteve %d de %d comentário(s).",
            pull_number,
            settings.MIN_SEVERITY,
            len(comentarios),
            total_bruto,
        )

    # Deduplicação por (arquivo, linha), mantendo a maior severidade.
    antes_dedupe = len(comentarios)
    comentarios = _dedupe_comments(comentarios)
    if antes_dedupe != len(comentarios):
        logger.info(
            "PR #%s: deduplicação removeu %d comentário(s) na mesma linha.",
            pull_number,
            antes_dedupe - len(comentarios),
        )

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

    # Caso 2: há comentários → loga cada um e posta a review.
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

    # Separa, com base no diff, quem pode ser ancorado na linha (ancorados) e
    # quem precisa ir para o corpo (overflow). Os que a API aceitaria como
    # comentário de linha ficam ancorados; só os inválidos viram texto no corpo.
    valid_lines = github_client.valid_diff_lines(diff)
    ancorados, overflow = _split_comments(comentarios, valid_lines)
    if overflow:
        logger.info(
            "PR #%s: %d comentário(s) ancorado(s) na linha, %d movido(s) para o "
            "corpo (linha fora do diff).",
            pull_number,
            len(ancorados),
            len(overflow),
        )

    resultado = await github_client.submit_pr_review(
        owner,
        repo,
        pull_number,
        comments=ancorados,
        commit_id=commit_id,
        overflow=overflow,
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
