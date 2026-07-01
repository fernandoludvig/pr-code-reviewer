"""Carregamento de variáveis de ambiente.

Lê o arquivo `.env` (via python-dotenv) e expõe as configurações do projeto
através de um objeto `settings` único, importado pelos demais módulos.
"""

import os

from dotenv import load_dotenv

# Carrega o .env da raiz do projeto para dentro de os.environ (se existir).
load_dotenv()


class Settings:
    """Configurações da aplicação lidas do ambiente."""

    # Segredo compartilhado com o GitHub Webhook. Usado para validar o HMAC
    # de cada requisição e garantir que o payload realmente veio do GitHub.
    GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")

    # Personal Access Token (classic ou fine-grained) usado para chamar a
    # API do GitHub — por enquanto só para buscar o diff dos PRs.
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # Base da API do GitHub. Configurável para permitir GitHub Enterprise.
    GITHUB_API_URL: str = os.getenv("GITHUB_API_URL", "https://api.github.com")

    # Chave da API da OpenAI, usada para a revisão de código via LLM (Fase 2).
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # Modelo usado na revisão. gpt-4o-mini pelo bom custo-benefício.
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


settings = Settings()
