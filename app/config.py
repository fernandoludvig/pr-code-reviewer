"""Environment variable loading.

Reads the `.env` file (via python-dotenv) and exposes the project configuration
through a single `settings` object imported by the other modules.
"""

import os

from dotenv import load_dotenv

# Load the project's .env into os.environ (if it exists).
load_dotenv()


class Settings:
    """Application settings read from the environment."""

    # Shared secret configured in the GitHub Webhook. Used to validate the HMAC
    # of every request and ensure the payload really came from GitHub.
    GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")

    # Personal Access Token (classic or fine-grained) used to call the GitHub
    # API — to fetch the PR diff and to post the review back.
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # GitHub API base URL. Configurable to support GitHub Enterprise.
    GITHUB_API_URL: str = os.getenv("GITHUB_API_URL", "https://api.github.com")

    # OpenAI API key, used for the LLM code review.
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # Model used for the review. gpt-4o-mini for its good cost/quality ratio.
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Minimum severity for a comment to be posted on the PR.
    # Values: "low" < "medium" < "high". Default: "medium".
    MIN_SEVERITY: str = os.getenv("MIN_SEVERITY", "medium")


settings = Settings()
