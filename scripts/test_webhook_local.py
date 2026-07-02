"""Local webhook flow test — with two modes.

SAFE MODE (default, POST_TO_GITHUB = False):
    1) DETERMINISTIC checks (no cost): severity filter, deduplication and the
       anchored/overflow split.
    2) Full webhook flow OFFLINE (HMAC + BackgroundTasks + real LLM review),
       with the diff fetch mocked and the posting intercepted/printed.
    3) Re-sends the SAME event (same head.sha) to show event deduplication (the
       second one is skipped, spending no tokens).

REAL MODE (POST_TO_GITHUB = True):
    ⚠️ Posts real comments on a real PR. Adjust REAL_OWNER/REAL_REPO/
    REAL_PR_NUMBER. Fetches the real diff, runs the review and posts, no mocks.

Usage:
    source .venv/bin/activate
    python scripts/test_webhook_local.py

    # test another minimum severity:
    MIN_SEVERITY=high python scripts/test_webhook_local.py
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
from unittest.mock import patch

# Make sure the project root is on sys.path, even when run from scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app import github_client, webhook
from app.config import settings
from app.main import app

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

# ---------------------------------------------------------------------------
# CONFIG — change here to enable real mode.
# ---------------------------------------------------------------------------
POST_TO_GITHUB = False  # ⚠️ True = posts real comments on the PR below.
REAL_OWNER = "fernandoludvig"
REAL_REPO = "pr-code-reviewer"
REAL_PR_NUMBER = 3  # adjust to your test PR number.

# Sample diff with intentional problems (SQL injection + hardcoded password).
FAKE_DIFF = '''diff --git a/api/users.py b/api/users.py
--- a/api/users.py
+++ b/api/users.py
@@ -1,3 +1,9 @@
+DB_PASSWORD = "SuperSecret123!"
+def get_user(conn, user_id):
+    cur = conn.cursor()
+    cur.execute("SELECT * FROM users WHERE id = " + str(user_id))
+    return cur.fetchall()
'''


def demo_helpers() -> None:
    """Deterministic checks of the Phase 4 improvements (no LLM, no GitHub)."""
    sample = [
        {"file": "api/users.py", "line": 1, "severity": "high", "comment": "hardcoded password"},
        {"file": "api/users.py", "line": 1, "severity": "low", "comment": "same spot, dup"},
        {"file": "api/users.py", "line": 4, "severity": "medium", "comment": "sql injection"},
        {"file": "api/users.py", "line": 50, "severity": "low", "comment": "style nitpick"},
        {"file": "api/users.py", "line": 999, "severity": "high", "comment": "line outside the diff"},
    ]
    filt = webhook._filter_by_severity(sample, "medium")
    print(f"  severity filter (>= medium):  {len(sample)} -> {len(filt)}  (drops the 'low' ones)")
    ded = webhook._dedupe_comments(filt)
    print(f"  deduplication:                {len(filt)} -> {len(ded)}  (merges same-line ones)")
    anc, ovf = webhook._split_comments(ded, github_client.valid_diff_lines(FAKE_DIFF))
    print(f"  anchor/body split:            {len(anc)} anchored, {len(ovf)} in the body (line 999 outside the diff)")


def _post_event(client: TestClient, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(
        settings.GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    resp = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )
    print(f"HTTP {resp.status_code} -> {resp.json()}")


def run_safe_mode() -> None:
    """Full offline flow; the GitHub posting is only simulated/printed."""
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 999,
            "title": "Local test PR",
            "user": {"login": "fernandoludvig"},
            "head": {"sha": "fakesha1234567890"},
        },
        "repository": {
            "full_name": "fernandoludvig/pr-code-reviewer",
            "name": "pr-code-reviewer",
            "owner": {"login": "fernandoludvig"},
        },
    }

    async def fake_get_diff(owner, repo, pull_number):
        print(f"[mock] get_pull_request_diff({owner}, {repo}, {pull_number})")
        return FAKE_DIFF

    async def fake_submit_review(owner, repo, pull_number, comments, commit_id, **kw):
        print("\n===== PAYLOAD THAT WOULD BE POSTED TO GITHUB (mock) =====")
        print(json.dumps(
            {"commit_id": commit_id, "event": kw.get("event", "COMMENT"),
             "body": kw.get("body", "🤖 Automated code review"),
             "overflow": kw.get("overflow"), "comments": comments},
            indent=2, ensure_ascii=False,
        ))
        print("========================================================")
        return {"ok": True, "fallback": False, "posted_line_comments": len(comments)}

    print("--- 1) Deterministic checks (filter / dedupe / split) ---")
    demo_helpers()

    with patch.object(github_client, "get_pull_request_diff", fake_get_diff), \
         patch.object(github_client, "submit_pr_review", fake_submit_review):
        client = TestClient(app)
        print("\n--- 2) Full webhook flow (real LLM) ---")
        _post_event(client, payload)
        print("\n--- 3) SAME event again (repeated head.sha) should be skipped ---")
        _post_event(client, payload)


def run_real_mode() -> None:
    """⚠️ REAL flow: fetches the diff, reviews and POSTS the review on the PR."""
    print(
        f"⚠️  REAL MODE — will post comments on PR #{REAL_PR_NUMBER} of "
        f"{REAL_OWNER}/{REAL_REPO}."
    )
    asyncio.run(
        webhook.process_pull_request(
            REAL_OWNER, REAL_REPO, REAL_PR_NUMBER, "Real review test", None
        )
    )


if __name__ == "__main__":
    if POST_TO_GITHUB:
        run_real_mode()
    else:
        print("SAFE MODE (offline). To post for real, set "
              "POST_TO_GITHUB = True at the top of the script.\n")
        run_safe_mode()
