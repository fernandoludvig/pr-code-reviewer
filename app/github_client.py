"""Funções para chamar a API do GitHub.

Fase 1: apenas define as funções de leitura de diff/arquivos de um PR.
Elas ainda NÃO são chamadas pelo webhook — serão usadas nas próximas fases,
quando o diff for enviado ao LLM.
"""

import httpx

from .config import settings


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
