"""Funções para chamar a API do GitHub.

Fase 1/2: leitura do diff de um PR (enviado ao LLM).
Fase 3: postagem da review de volta no PR (comentários por linha), com fallback
para comentário geral quando a linha não faz parte do diff.
"""

import logging

import httpx

from .config import settings

logger = logging.getLogger("pr_code_reviewer.github_client")


def _headers(diff: bool = False) -> dict[str, str]:
    """Monta os headers padrão para a API do GitHub.

    Se `diff=True`, pede o corpo no media type de diff unificado
    (`application/vnd.github.v3.diff`); caso contrário, JSON padrão.
    """
    accept = (
        "application/vnd.github.v3.diff" if diff else "application/vnd.github+json"
    )
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    return headers


async def get_pull_request_diff(owner: str, repo: str, pull_number: int) -> str:
    """Retorna o diff unificado completo de um PR (texto no formato `.diff`).

    Usa GET /repos/{owner}/{repo}/pulls/{pull_number} com o Accept de diff.
    Ideal para enviar o patch inteiro ao LLM.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=_headers(diff=True))
        resp.raise_for_status()
        return resp.text


async def get_pull_request_files(
    owner: str, repo: str, pull_number: int
) -> list[dict]:
    """Lista os arquivos alterados no PR, com o `patch` de cada arquivo.

    Usa GET /repos/{owner}/{repo}/pulls/{pull_number}/files, seguindo a
    paginação (100 por página). Útil quando quisermos comentar por arquivo/linha
    em vez de enviar o diff inteiro.
    """
    files: list[dict] = []
    url = f"{settings.GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/files"
    per_page = 100
    page = 1

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                url, headers=_headers(), params={"per_page": per_page, "page": page}
            )
            resp.raise_for_status()
            batch = resp.json()
            files.extend(batch)
            if len(batch) < per_page:
                break
            page += 1

    return files


async def get_pull_request_head_sha(
    owner: str, repo: str, pull_number: int
) -> str | None:
    """Retorna o SHA do último commit do PR (campo `head.sha`).

    Necessário como `commit_id` no payload da review. Normalmente o SHA já vem
    no payload do webhook (`pull_request.head.sha`); esta função serve como
    fallback quando ele não está disponível. Retorna None em caso de erro.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            return resp.json().get("head", {}).get("sha")
    except Exception:
        logger.exception(
            "Falha ao buscar o head.sha do PR #%s (%s/%s).", pull_number, owner, repo
        )
        return None


async def _submit_review_fallback(
    client: httpx.AsyncClient,
    url: str,
    pull_number: int,
    comments: list[dict],
    commit_id: str | None,
    header_body: str,
) -> dict:
    """Fallback: quando a review com comentários por linha é rejeitada (ex.: a
    linha não faz parte do diff atual), posta uma review geral única com todos
    os comentários concatenados no corpo, sem âncora de linha.
    """
    linhas = [
        header_body,
        "",
        "> ⚠️ Alguns comentários não puderam ser ancorados em linhas do diff "
        "e seguem abaixo como comentário geral:",
        "",
    ]
    for c in comments:
        linhas.append(
            f"- **{c.get('path')}** (linha {c.get('line')}): {c.get('body')}"
        )

    payload: dict = {"body": "\n".join(linhas), "event": "COMMENT"}
    if commit_id:
        payload["commit_id"] = commit_id

    try:
        resp = await client.post(url, headers=_headers(), json=payload)
    except Exception:
        logger.exception("Falha na requisição de fallback da review (PR #%s).", pull_number)
        return {"ok": False, "fallback": True}

    if resp.status_code in (200, 201):
        logger.info(
            "Fallback: review geral postada no PR #%s com %d comentário(s) no corpo.",
            pull_number,
            len(comments),
        )
        return {"ok": True, "fallback": True, "posted_line_comments": 0, "in_body": len(comments)}

    logger.error(
        "Fallback também falhou no PR #%s (HTTP %s): %s",
        pull_number,
        resp.status_code,
        resp.text[:500],
    )
    return {"ok": False, "fallback": True, "status": resp.status_code}


async def submit_pr_review(
    owner: str,
    repo: str,
    pull_number: int,
    comments: list[dict],
    commit_id: str | None,
    body: str = "🤖 Revisão automática de código",
    event: str = "COMMENT",
) -> dict:
    """Posta uma review no PR via POST /repos/{owner}/{repo}/pulls/{n}/reviews.

    `comments` deve estar no formato da API do GitHub:
        {"path": "arquivo.py", "line": 42, "side": "RIGHT", "body": "..."}

    Usa event="COMMENT" (o bot só comenta; não aprova nem bloqueia). Se a API
    rejeitar os comentários de linha (comum quando a linha não está no diff),
    faz fallback para uma review geral. Nunca levanta exceção: loga e retorna
    um dict com o resultado.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"

    payload: dict = {"body": body, "event": event}
    if commit_id:
        payload["commit_id"] = commit_id
    if comments:
        payload["comments"] = comments

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, headers=_headers(), json=payload)
        except Exception:
            logger.exception("Falha na requisição da review do PR #%s.", pull_number)
            return {"ok": False}

        if resp.status_code in (200, 201):
            logger.info(
                "Review postada no PR #%s (%d comentário(s) de linha, event=%s).",
                pull_number,
                len(comments),
                event,
            )
            return {"ok": True, "fallback": False, "posted_line_comments": len(comments)}

        logger.error(
            "GitHub rejeitou a review do PR #%s (HTTP %s): %s",
            pull_number,
            resp.status_code,
            resp.text[:500],
        )

        # Se havia comentários de linha, tenta o fallback geral.
        if comments:
            return await _submit_review_fallback(
                client, url, pull_number, comments, commit_id, body
            )
        return {"ok": False, "status": resp.status_code}
