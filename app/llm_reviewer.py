"""Revisão de código via LLM (OpenAI).

Fase 2: recebe o diff bruto de um PR e pede a um modelo da OpenAI para agir como
revisor de código sênior, retornando uma lista estruturada de comentários.
Ainda NÃO posta nada de volta no GitHub — isso fica para a Fase 3.
"""

import json
import logging

from openai import OpenAI

from .config import settings

logger = logging.getLogger("pr_code_reviewer.llm_reviewer")

# --- Limites de tamanho do diff -------------------------------------------
# Truncamos diffs muito grandes para não estourar o contexto do modelo nem
# gastar tokens à toa. Usamos uma heurística grosseira de ~4 chars por token.
MAX_DIFF_TOKENS = 6000
CHARS_PER_TOKEN = 4
MAX_DIFF_CHARS = MAX_DIFF_TOKENS * CHARS_PER_TOKEN

# Severidades aceitas; qualquer valor fora disso é normalizado para "baixa".
SEVERIDADES_VALIDAS = {"alta", "media", "baixa"}

SYSTEM_PROMPT = (
    "Você é um revisor de código sênior, experiente, objetivo e rigoroso. "
    "Sua tarefa é analisar o diff de um Pull Request e apontar apenas problemas "
    "reais e acionáveis, nas seguintes categorias: bugs e erros de lógica; "
    "riscos de segurança (ex.: SQL/command injection, XSS, exposição de "
    "credenciais ou segredos, uso inseguro de entrada do usuário); más práticas "
    "de código; código duplicado; e problemas de performance. "
    "Não invente problemas nem faça comentários de estilo triviais: se o código "
    "estiver correto e seguro, não force apontamentos. Analise apenas as linhas "
    "adicionadas/alteradas no diff. Responda em português."
)


def _truncate_diff(diff_text: str) -> str:
    """Trunca o diff se ele exceder o limite de tamanho, avisando no log."""
    if len(diff_text) <= MAX_DIFF_CHARS:
        return diff_text
    logger.warning(
        "Diff grande (%d caracteres ~ %d tokens); truncando para ~%d tokens.",
        len(diff_text),
        len(diff_text) // CHARS_PER_TOKEN,
        MAX_DIFF_TOKENS,
    )
    return diff_text[:MAX_DIFF_CHARS] + "\n\n[... diff truncado por tamanho ...]"


def _build_user_prompt(diff_text: str, pr_title: str) -> str:
    """Monta a instrução do usuário pedindo resposta estritamente em JSON."""
    return (
        f"Título do PR: {pr_title}\n\n"
        "Analise o diff abaixo (formato unified diff) e responda SOMENTE com um "
        "objeto JSON, sem texto fora do JSON, no formato:\n"
        '{"comentarios": [\n'
        '  {"arquivo": "caminho/do/arquivo.py", "linha_aproximada": 42, '
        '"severidade": "alta|media|baixa", '
        '"comentario": "descrição objetiva do problema e sugestão de correção"}\n'
        "]}\n"
        'Se não houver nenhum problema relevante, retorne {"comentarios": []}.\n\n'
        "Diff:\n"
        "```diff\n"
        f"{diff_text}\n"
        "```"
    )


def _parse_response(content: str) -> list[dict]:
    """Faz o parsing robusto do JSON retornado pelo modelo.

    Aceita tanto um array direto quanto um objeto {"comentarios": [...]}.
    Em qualquer erro de formato, loga e retorna [] em vez de quebrar.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.error("Resposta do modelo não é JSON válido: %s", content[:500])
        return []

    # O modelo pode devolver o array dentro de uma chave "comentarios".
    if isinstance(data, dict):
        data = data.get("comentarios", [])

    if not isinstance(data, list):
        logger.error("Formato inesperado na resposta do modelo: %r", data)
        return []

    comentarios: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        severidade = str(item.get("severidade", "baixa")).lower()
        if severidade not in SEVERIDADES_VALIDAS:
            severidade = "baixa"
        comentarios.append(
            {
                "arquivo": item.get("arquivo", "?"),
                "linha_aproximada": item.get("linha_aproximada"),
                "severidade": severidade,
                "comentario": item.get("comentario", ""),
            }
        )
    return comentarios


def review_diff(diff_text: str, pr_title: str) -> list[dict]:
    """Revisa o diff de um PR usando um LLM e retorna uma lista de comentários.

    Retorna uma lista de dicts no formato:
        {"arquivo", "linha_aproximada", "severidade", "comentario"}

    Nunca levanta exceção por falha do modelo/parsing: em qualquer erro, loga e
    retorna [] para não derrubar a aplicação.
    """
    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY não configurada; pulando revisão via LLM.")
        return []

    if not diff_text or not diff_text.strip():
        logger.info("Diff vazio; nada para revisar.")
        return []

    diff_text = _truncate_diff(diff_text)
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(diff_text, pr_title)},
            ],
        )
        content = response.choices[0].message.content or ""
    except Exception:
        # Erros de rede, autenticação, rate limit, etc. — não devem quebrar o app.
        logger.exception("Erro ao chamar a API da OpenAI.")
        return []

    return _parse_response(content)
