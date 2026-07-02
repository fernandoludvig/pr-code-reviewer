"""Route that receives and validates GitHub webhook events.

- Validates the HMAC signature and filters relevant `pull_request` events
  (opened / synchronize / reopened).
- For those events, fetches the real PR diff and runs the LLM reviewer.
- Maps the generated comments to the GitHub API format and posts a review
  (event=COMMENT) back to the PR. All in the background.
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

# pull_request actions we care about.
RELEVANT_ACTIONS = {"opened", "synchronize", "reopened"}

# In-memory cache of already-reviewed commits (head.sha), so we don't reprocess
# the same event on rapid pushes and waste LLM calls. Reset on server restart
# (acceptable for a portfolio; see README > Known limitations).
_processed_commits = TTLCache(ttl_seconds=3600)

# Emoji per severity, used in the body of each comment posted on the PR.
SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Severity ordering for comparison (low < medium < high).
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _filter_by_severity(comments: list[dict], min_severity: str) -> list[dict]:
    """Keep only comments with severity >= min_severity.

    An unknown severity is treated as the lowest (0). An invalid min_severity
    falls back to "medium".
    """
    min_rank = SEVERITY_ORDER.get(
        (min_severity or "").lower(), SEVERITY_ORDER["medium"]
    )
    return [
        c
        for c in comments
        if SEVERITY_ORDER.get(c.get("severity"), 0) >= min_rank
    ]


def _dedupe_comments(comments: list[dict]) -> list[dict]:
    """Remove duplicate comments on the same (file, approximate line).

    The LLM sometimes generates two comments for the same spot with slightly
    different text. Simple heuristic: if they fall on the same (file, line),
    keep only the one with the HIGHEST severity and drop the other. Preserves
    the original order.
    """
    best: dict[tuple, dict] = {}
    for c in comments:
        key = (c.get("file"), c.get("line"))
        current = best.get(key)
        if current is None or SEVERITY_ORDER.get(
            c.get("severity"), 0
        ) > SEVERITY_ORDER.get(current.get("severity"), 0):
            best[key] = c
    return list(best.values())


def _split_comments(
    comments: list[dict], valid_lines: dict[str, set[int]]
) -> tuple[list[dict], list[str]]:
    """Split the LLM comments into anchorable vs. overflow.

    Returns (anchored, overflow):
    - anchored: comments in the GitHub API format whose (file, line) exists in
      the diff (`side="RIGHT"`) — posted on the line.
    - overflow: pre-formatted markdown lines for comments that CANNOT be
      anchored (missing file/line, or a line outside the diff) — they go to the
      body instead of being discarded.
    """
    anchored: list[dict] = []
    overflow: list[str] = []
    for c in comments:
        severity = c.get("severity", "low")
        emoji = SEVERITY_EMOJI.get(severity, "⚪")
        text = c.get("comment", "")
        path = c.get("file")
        raw_line = c.get("line")

        line: int | None = None
        if path and raw_line is not None:
            try:
                line = int(raw_line)
            except (TypeError, ValueError):
                line = None

        if line is not None and line in valid_lines.get(path, set()):
            anchored.append(
                {
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": f"{emoji} **[{severity}]** {text}",
                }
            )
        else:
            overflow.append(f"- {emoji} **[{severity}]** {path}:{raw_line} — {text}")
    return anchored, overflow


async def process_pull_request(
    owner: str, repo: str, pull_number: int, pr_title: str, head_sha: str | None = None
) -> None:
    """Fetch the diff, run the LLM review and post the review (in background).

    Runs AFTER the 200 response has already been sent to GitHub, so it can take
    as long as needed without hitting the webhook timeout. Never raises to the
    caller: any error is logged.
    """
    # Event dedup: if this commit (head.sha) was reviewed recently, skip it —
    # avoids duplicate token spend on rapid pushes.
    if head_sha:
        key = f"{owner}/{repo}#{pull_number}@{head_sha}"
        if _processed_commits.seen(key):
            logger.info(
                "PR #%s: commit %s already reviewed, skipping.",
                pull_number,
                head_sha[:8],
            )
            return

    try:
        diff = await github_client.get_pull_request_diff(owner, repo, pull_number)
    except Exception:
        logger.exception(
            "Failed to fetch the diff of PR #%s (%s/%s).", pull_number, owner, repo
        )
        return

    logger.info(
        "Diff of PR #%s fetched (%d chars). Starting LLM review...",
        pull_number,
        len(diff),
    )

    # review_diff is synchronous (blocking OpenAI client); run it in a thread so
    # we don't block the FastAPI event loop.
    comments = await asyncio.to_thread(llm_reviewer.review_diff, diff, pr_title)

    # Minimum-severity filter (MIN_SEVERITY config).
    raw_total = len(comments)
    comments = _filter_by_severity(comments, settings.MIN_SEVERITY)
    if raw_total != len(comments):
        logger.info(
            "PR #%s: severity filter (>= %s) kept %d of %d comment(s).",
            pull_number,
            settings.MIN_SEVERITY,
            len(comments),
            raw_total,
        )

    # Deduplication by (file, line), keeping the highest severity.
    before_dedupe = len(comments)
    comments = _dedupe_comments(comments)
    if before_dedupe != len(comments):
        logger.info(
            "PR #%s: deduplication removed %d comment(s) on the same line.",
            pull_number,
            before_dedupe - len(comments),
        )

    # commit_id is needed to anchor the line comments. Prefer the SHA that came
    # in the webhook; if missing, fetch it via the API.
    commit_id = head_sha or await github_client.get_pull_request_head_sha(
        owner, repo, pull_number
    )

    # Case 1: no problems found → simple positive review, no line comments.
    if not comments:
        logger.info("Review of PR #%s: no comments generated.", pull_number)
        result = await github_client.submit_pr_review(
            owner,
            repo,
            pull_number,
            comments=[],
            commit_id=commit_id,
            body="✅ Automated review: no critical issues found.",
        )
        logger.info("Review posting (PR #%s): %s", pull_number, result)
        return

    # Case 2: there are comments → log each one and post the review.
    logger.info(
        "Review of PR #%s: %d comment(s) generated:", pull_number, len(comments)
    )
    for i, c in enumerate(comments, start=1):
        logger.info(
            "  [%d] %s:%s | severity=%s | %s",
            i,
            c.get("file"),
            c.get("line"),
            c.get("severity"),
            c.get("comment"),
        )

    # Based on the diff, split which comments can be anchored to a line
    # (anchored) and which must go to the body (overflow). The ones the API
    # would accept as line comments stay anchored; only the invalid ones become
    # body text.
    valid_lines = github_client.valid_diff_lines(diff)
    anchored, overflow = _split_comments(comments, valid_lines)
    if overflow:
        logger.info(
            "PR #%s: %d comment(s) anchored to a line, %d moved to the body "
            "(line outside the diff).",
            pull_number,
            len(anchored),
            len(overflow),
        )

    result = await github_client.submit_pr_review(
        owner,
        repo,
        pull_number,
        comments=anchored,
        commit_id=commit_id,
        overflow=overflow,
    )
    logger.info("Review posting (PR #%s): %s", pull_number, result)


def verify_signature(payload_body: bytes, signature_header: str | None) -> None:
    """Validate the HMAC-SHA256 signature sent by GitHub.

    GitHub signs the raw request body with `GITHUB_WEBHOOK_SECRET` and sends the
    result in the `X-Hub-Signature-256` header as `sha256=<hexdigest>`. We
    recompute it and compare in a timing-safe way.

    Raises HTTPException if the config is missing or the signature is invalid.
    """
    if not settings.GITHUB_WEBHOOK_SECRET:
        # Without a configured secret we cannot validate — fail closed.
        logger.error("GITHUB_WEBHOOK_SECRET not configured; refusing webhook.")
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    if not signature_header:
        raise HTTPException(
            status_code=401, detail="Missing X-Hub-Signature-256 header"
        )

    expected = "sha256=" + hmac.new(
        key=settings.GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid signature")


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    """Receive GitHub Webhook events.

    Returns 200 quickly (GitHub expects a response within a few seconds). The
    heavy work (fetching the diff + LLM review) is scheduled in the background so
    it does not block the response to GitHub.
    """
    # Important: sign the RAW body, before any JSON parsing.
    body = await request.body()
    verify_signature(body, x_hub_signature_256)

    payload = await request.json()

    # Test event fired when the webhook is created on GitHub.
    if x_github_event == "ping":
        logger.info("Ping received from the GitHub webhook.")
        return {"msg": "pong"}

    if x_github_event != "pull_request":
        logger.info("Event ignored: %s", x_github_event)
        return {"msg": f"event '{x_github_event}' ignored"}

    action = payload.get("action")
    if action not in RELEVANT_ACTIONS:
        logger.info("pull_request action ignored: %s", action)
        return {"msg": f"action '{action}' ignored"}

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    number = pr.get("number")
    title = pr.get("title")
    owner = repo.get("owner", {}).get("login")
    repo_name = repo.get("name")
    head_sha = pr.get("head", {}).get("sha")

    logger.info(
        "PR #%s | action=%s | repo=%s | author=%s | title=%s",
        number,
        action,
        repo.get("full_name"),
        pr.get("user", {}).get("login"),
        title,
    )

    # Schedule the diff fetch + LLM review + posting for after the 200 response.
    if owner and repo_name and number is not None:
        background_tasks.add_task(
            process_pull_request, owner, repo_name, number, title or "", head_sha
        )
    else:
        logger.warning(
            "Payload without enough owner/repo/number; review not scheduled."
        )

    return {"msg": "ok", "pr": number, "action": action}
