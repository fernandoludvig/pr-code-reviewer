"""Functions to call the GitHub API.

Reading: fetch the diff of a PR (sent to the LLM).
Writing: post the review back to the PR (line comments), with a fallback to a
general comment when a line is not part of the diff.
"""

import logging
import re

import httpx

from .config import settings

logger = logging.getLogger("pr_code_reviewer.github_client")

# Captures the start of the RIGHT-side range in a hunk header: "@@ -a,b +c,d @@".
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def valid_diff_lines(diff_text: str) -> dict[str, set[int]]:
    """Map each file -> set of RIGHT-side line numbers present in the diff.

    These are the added (`+`) or context (` `) lines — exactly the ones on which
    the GitHub API accepts review comments with `side="RIGHT"`. Used to decide,
    before posting, which comments can be anchored to a line and which must go to
    the review body.
    """
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    right_line = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            current_file = None
            continue
        if line.startswith("+++ "):
            path = line[4:]
            if path.startswith("b/"):
                path = path[2:]
            current_file = None if path == "/dev/null" else path
            if current_file is not None:
                result.setdefault(current_file, set())
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                right_line = int(m.group(1))
            continue
        if current_file is None:
            continue
        if line.startswith("+"):
            result[current_file].add(right_line)
            right_line += 1
        elif line.startswith("-") or line.startswith("\\"):
            # `-` = LEFT side (does not advance RIGHT); `\` = "No newline at EOF".
            continue
        else:
            # Context line (starts with a space or is empty).
            result[current_file].add(right_line)
            right_line += 1

    return result


def _headers(diff: bool = False) -> dict[str, str]:
    """Build the standard headers for the GitHub API.

    If `diff=True`, request the body in the unified-diff media type
    (`application/vnd.github.v3.diff`); otherwise standard JSON.
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
    """Return the full unified diff of a PR (text in `.diff` format).

    Uses GET /repos/{owner}/{repo}/pulls/{pull_number} with the diff Accept.
    Ideal for sending the whole patch to the LLM.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=_headers(diff=True))
        resp.raise_for_status()
        return resp.text


async def get_pull_request_files(
    owner: str, repo: str, pull_number: int
) -> list[dict]:
    """List the files changed in the PR, with each file's `patch`.

    Uses GET /repos/{owner}/{repo}/pulls/{pull_number}/files, following
    pagination (100 per page). Useful when we want to comment per file/line
    instead of sending the whole diff.
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
    """Return the SHA of the PR's latest commit (the `head.sha` field).

    Needed as `commit_id` in the review payload. Normally the SHA already comes
    in the webhook payload (`pull_request.head.sha`); this function is a fallback
    for when it is not available. Returns None on error.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            return resp.json().get("head", {}).get("sha")
    except Exception:
        logger.exception(
            "Failed to fetch head.sha of PR #%s (%s/%s).", pull_number, owner, repo
        )
        return None


def _build_body(header: str, overflow: list[str] | None) -> str:
    """Build the review body: header + (optional) section for comments that
    could not be anchored to diff lines."""
    if not overflow:
        return header
    lines = [
        header,
        "",
        "> ⚠️ Comments that could not be anchored to diff lines:",
        "",
    ]
    lines.extend(overflow)
    return "\n".join(lines)


async def _submit_review_fallback(
    client: httpx.AsyncClient,
    url: str,
    pull_number: int,
    comments: list[dict],
    commit_id: str | None,
    header: str,
    overflow: list[str] | None,
) -> dict:
    """Safety net: if even the already-validated line comments get rejected,
    post a single general review moving ALL of them to the body (no line
    anchor) — nothing is discarded.
    """
    all_lines = list(overflow or [])
    for c in comments:
        all_lines.append(f"- **{c.get('path')}** (line {c.get('line')}): {c.get('body')}")

    payload: dict = {"body": _build_body(header, all_lines), "event": "COMMENT"}
    if commit_id:
        payload["commit_id"] = commit_id

    try:
        resp = await client.post(url, headers=_headers(), json=payload)
    except Exception:
        logger.exception("Fallback review request failed (PR #%s).", pull_number)
        return {"ok": False, "fallback": True}

    if resp.status_code in (200, 201):
        logger.info(
            "Fallback: general review posted on PR #%s with %d comment(s) in the body.",
            pull_number,
            len(all_lines),
        )
        return {"ok": True, "fallback": True, "posted_line_comments": 0, "in_body": len(all_lines)}

    logger.error(
        "Fallback also failed on PR #%s (HTTP %s): %s",
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
    body: str = "🤖 Automated code review",
    event: str = "COMMENT",
    overflow: list[str] | None = None,
) -> dict:
    """Post a review on the PR via POST /repos/{owner}/{repo}/pulls/{n}/reviews.

    `comments` must be in the GitHub API format, ALREADY validated against the
    diff by the caller (see `valid_diff_lines`):
        {"path": "file.py", "line": 42, "side": "RIGHT", "body": "..."}
    `overflow` is a list of pre-formatted markdown lines for comments that could
    not be anchored (file/line outside the diff) — they go to the body.

    Uses event="COMMENT" (the bot only comments; it neither approves nor blocks).
    Since the line comments are already validated, a 422 is rare; if it still
    happens, the fallback moves everything to the body. Never raises.
    """
    url = f"{settings.GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"

    payload: dict = {"body": _build_body(body, overflow), "event": event}
    if commit_id:
        payload["commit_id"] = commit_id
    if comments:
        payload["comments"] = comments

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, headers=_headers(), json=payload)
        except Exception:
            logger.exception("Review request failed for PR #%s.", pull_number)
            return {"ok": False}

        if resp.status_code in (200, 201):
            logger.info(
                "Review posted on PR #%s (%d line comment(s), %d in the body, "
                "event=%s).",
                pull_number,
                len(comments),
                len(overflow or []),
                event,
            )
            return {
                "ok": True,
                "fallback": False,
                "posted_line_comments": len(comments),
                "in_body": len(overflow or []),
            }

        logger.error(
            "GitHub rejected the review of PR #%s (HTTP %s): %s",
            pull_number,
            resp.status_code,
            resp.text[:500],
        )

        # Safety net: move everything to the body if there are line comments.
        if comments:
            return await _submit_review_fallback(
                client, url, pull_number, comments, commit_id, body, overflow
            )
        return {"ok": False, "status": resp.status_code}
