"""LLM code review (OpenAI).

Takes the raw diff of a PR and asks an OpenAI model to act as a senior code
reviewer, returning a structured list of comments. Posting them back to GitHub
is handled by `webhook.py` / `github_client.py`.
"""

import json
import logging

from openai import OpenAI

from .config import settings

logger = logging.getLogger("pr_code_reviewer.llm_reviewer")

# --- Diff size limits -----------------------------------------------------
# Very large diffs are truncated so we don't blow past the model context nor
# waste tokens. We use a rough heuristic of ~4 chars per token.
MAX_DIFF_TOKENS = 6000
CHARS_PER_TOKEN = 4
MAX_DIFF_CHARS = MAX_DIFF_TOKENS * CHARS_PER_TOKEN

# Accepted severities; anything else is normalized to "low".
VALID_SEVERITIES = {"high", "medium", "low"}

SYSTEM_PROMPT = (
    "You are a senior code reviewer: experienced, objective and rigorous. "
    "Your task is to analyze the diff of a Pull Request and point out only real, "
    "actionable problems in the following categories: bugs and logic errors; "
    "security risks (e.g. SQL/command injection, XSS, exposure of credentials or "
    "secrets, unsafe use of user input); bad practices; duplicated code; and "
    "performance issues. "
    "Do not invent problems or make trivial style nitpicks: if the code is "
    "correct and safe, do not force findings. Only analyze the added/changed "
    "lines in the diff. Respond in English."
)


def _truncate_diff(diff_text: str) -> str:
    """Truncate the diff if it exceeds the size limit, warning in the log."""
    if len(diff_text) <= MAX_DIFF_CHARS:
        return diff_text
    logger.warning(
        "Large diff (%d chars ~ %d tokens); truncating to ~%d tokens.",
        len(diff_text),
        len(diff_text) // CHARS_PER_TOKEN,
        MAX_DIFF_TOKENS,
    )
    return diff_text[:MAX_DIFF_CHARS] + "\n\n[... diff truncated by size ...]"


def _build_user_prompt(diff_text: str, pr_title: str) -> str:
    """Build the user instruction asking for a strictly-JSON response."""
    return (
        f"PR title: {pr_title}\n\n"
        "Analyze the diff below (unified diff format) and respond ONLY with a "
        "JSON object, with no text outside the JSON, in this shape:\n"
        '{"comments": [\n'
        '  {"file": "path/to/file.py", "line": 42, '
        '"severity": "high|medium|low", '
        '"comment": "objective description of the issue and a fix suggestion"}\n'
        "]}\n"
        'If there is no relevant problem, return {"comments": []}.\n\n'
        "Diff:\n"
        "```diff\n"
        f"{diff_text}\n"
        "```"
    )


def _parse_response(content: str) -> list[dict]:
    """Robustly parse the JSON returned by the model.

    Accepts either a bare array or an object {"comments": [...]}. On any format
    error, it logs and returns [] instead of raising.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.error("Model response is not valid JSON: %s", content[:500])
        return []

    # The model may return the array inside a "comments" key.
    if isinstance(data, dict):
        data = data.get("comments", [])

    if not isinstance(data, list):
        logger.error("Unexpected shape in the model response: %r", data)
        return []

    comments: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "low")).lower()
        if severity not in VALID_SEVERITIES:
            severity = "low"
        comments.append(
            {
                "file": item.get("file", "?"),
                "line": item.get("line"),
                "severity": severity,
                "comment": item.get("comment", ""),
            }
        )
    return comments


def review_diff(diff_text: str, pr_title: str) -> list[dict]:
    """Review a PR diff using an LLM and return a list of comments.

    Returns a list of dicts in the shape:
        {"file", "line", "severity", "comment"}

    Never raises on model/parsing failure: on any error it logs and returns []
    so the application does not crash.
    """
    if not settings.OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not configured; skipping LLM review.")
        return []

    if not diff_text or not diff_text.strip():
        logger.info("Empty diff; nothing to review.")
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
        # Network, auth, rate-limit, etc. errors must not crash the app.
        logger.exception("Error calling the OpenAI API.")
        return []

    return _parse_response(content)
