# PR Code Reviewer

A bot that automatically reviews **GitHub Pull Requests**. It receives PR events
via webhook, sends the code diff to an LLM and posts review comments directly on
the Pull Request.

> **Status: Phase 4 — robustness.**
> End-to-end flow (receive the PR → fetch the diff → LLM → **post the review on
> the PR**), now with a severity filter, deduplication, line-anchored comments
> with body overflow, and an anti-reprocessing cache. See the
> [Roadmap](#roadmap) and the [Known limitations](#known-limitations).

---

## Overview (final product)

```
   GitHub PR (opened/synchronize/reopened)
            │  webhook (HTTP POST)
            ▼
   ┌─────────────────────┐
   │  pr-code-reviewer    │  FastAPI
   │  1. validate HMAC    │
   │  2. fetch the diff   │  ── GitHub API ──►
   │  3. send to the LLM  │  ── LLM ──►
   │  4. post comments    │  ── GitHub API ──►
   └─────────────────────┘
```

The goal is an automated reviewer that comments on code issues, suggestions and
best practices on every PR that is opened or updated.

---

## What already works

### Phase 1 — Webhook reception and validation

- `POST /webhook/github` endpoint that receives GitHub events.
- **HMAC-SHA256 signature validation** (`X-Hub-Signature-256`) using the
  `GITHUB_WEBHOOK_SECRET` — ensures the payload really came from GitHub.
- Filtering of `pull_request` events for the **opened**, **synchronize** and
  **reopened** actions.
- Console logging of: PR number, title, repository, author and action.
- Fast `200 OK` response (GitHub expects a response within a few seconds).

### Phase 2 — Diff fetch + LLM analysis

- For each relevant event, fetches the **real diff** of the PR via the GitHub
  API (`github_client.get_pull_request_diff`).
- Sends the diff to an OpenAI LLM (`gpt-4o-mini` by default) that acts as a
  **senior code reviewer**, looking for bugs, security risks, bad practices,
  duplicated code and performance issues (`llm_reviewer.review_diff`).
- The model responds **in JSON**; the response is parsed with error handling
  (malformed JSON → logs and returns an empty list, without crashing the app).
- Very large diffs are **truncated** (~6000 tokens) to avoid blowing past the
  context or spending too much.
- The diff fetch + review run in the **background** (`BackgroundTasks`), so the
  webhook keeps responding `200 OK` to GitHub immediately.
- The generated comments (file, approximate line, severity, comment) are
  **logged to the console**.

### Phase 3 — Posting the review on the PR

- Posts the comments back to the PR via
  `POST /repos/{owner}/{repo}/pulls/{n}/reviews`
  (`github_client.submit_pr_review`), as **line comments**.
- Always uses `event="COMMENT"` — the bot **only suggests**, it never approves
  (`APPROVE`) nor blocks (`REQUEST_CHANGES`) the PR.
- Each comment carries a severity emoji: 🔴 high, 🟡 medium, 🟢 low.
- If **there are no problems**, it posts a simple positive review:
  `✅ Automated review: no critical issues found.`
- **Fallback:** the GitHub API only accepts comments on lines that are part of
  the diff. If a line comment is rejected (HTTP 422), the bot reposts everything
  as a **general review** (comments in the body), without crashing the app.
- Requires a `GITHUB_TOKEN` with **Pull requests: Read and write** permission.

### Phase 4 — Robustness

- **Severity filter** (`MIN_SEVERITY`): only posts what is at or above the
  threshold (`low` < `medium` < `high`; default `medium`).
- **Deduplication:** if the LLM generates two comments for the same
  `(file, line)`, it keeps only the one with the highest severity.
- **Precise anchoring with overflow:** instead of dumping everything into the
  body when a line is not in the diff, the bot **parses the diff** to know which
  lines are commentable. Valid comments stay **anchored to the line**; only the
  ones that fall outside the diff go to the review **body** — nothing is
  discarded.
- **Anti-reprocessing:** an in-memory cache stores the already-reviewed
  `head.sha` per PR (TTL). Rapid pushes that fire multiple `synchronize` events
  don't spend LLM calls twice for the same commit.

---

## Project structure

```
pr-code-reviewer/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app + status routes
│   ├── webhook.py        # Route that receives/validates events + orchestrates the review
│   ├── github_client.py  # GitHub API: fetch diff, post review, parse lines
│   ├── llm_reviewer.py   # LLM code review (OpenAI)
│   ├── dedup_cache.py    # In-memory TTL cache (anti-reprocessing)
│   └── config.py         # Environment variable loading
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Running locally

Requires **Python 3.12+**.

### 1. Create the virtual environment and install dependencies

```bash
cd ~/pr-code-reviewer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure the environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

- `GITHUB_WEBHOOK_SECRET` — a strong, random secret. Generate one with:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
  (the **same** value must be pasted into GitHub when creating the webhook).
- `GITHUB_TOKEN` — a GitHub Personal Access Token (see below).
- `OPENAI_API_KEY` — OpenAI API key, used for the LLM review.
  Create one at <https://platform.openai.com/api-keys>.
- `OPENAI_MODEL` *(optional)* — model used for the review. Default: `gpt-4o-mini`.
- `MIN_SEVERITY` *(optional)* — minimum severity to post a comment:
  `low` < `medium` < `high`. Default: `medium` (posts medium and high).

### 3. Start the server

```bash
uvicorn app.main:app --reload --port 8000
```

Check that it is up:

```bash
curl http://localhost:8000/health
# {"status":"healthy"}
```

### 4. Expose the port with ngrok

GitHub needs to reach your server over the internet. With
[ngrok](https://ngrok.com/) installed:

```bash
ngrok http 8000
```

Copy the generated public URL (e.g. `https://abcd-1234.ngrok-free.app`). Your
webhook endpoint will be:

```
https://abcd-1234.ngrok-free.app/webhook/github
```

> The free ngrok URL changes on each restart — update the webhook on GitHub
> when that happens.

---

## Configuring the GitHub Webhook

1. Create (or use) a test repository on your GitHub.
2. Go to **Settings → Webhooks → Add webhook**.
3. Fill in:
   - **Payload URL:** the ngrok URL + `/webhook/github`
     (e.g. `https://abcd-1234.ngrok-free.app/webhook/github`).
   - **Content type:** `application/json`.
   - **Secret:** the **same** value as `GITHUB_WEBHOOK_SECRET` in your `.env`.
   - **Which events?** → **Let me select individual events** → check only
     **Pull requests** (uncheck "Pushes" if it is checked).
   - Keep **Active** on.
4. Click **Add webhook**. GitHub sends a `ping` event — the server responds
   `{"msg":"pong"}` and you will see a ✅ in the **Recent Deliveries** tab.

### Creating the Personal Access Token (`GITHUB_TOKEN`)

In **Settings → Developer settings → Personal access tokens**:

- **Classic:** check the `repo` scope (or `public_repo` if the repository is
  public).
- **Fine-grained:** grant access to the test repository with **Contents: Read**
  and **Pull requests: Read and write** (write is required from Phase 3 on so
  the bot can post the review on the PR).

Paste the token into `GITHUB_TOKEN` in `.env`.

---

## Testing

### Locally, without opening a real PR

The `scripts/test_webhook_local.py` script exercises the whole pipeline offline
(HMAC + `BackgroundTasks` + real LLM review), mocking the diff fetch and the
GitHub posting:

```bash
source .venv/bin/activate
python scripts/test_webhook_local.py
```

It prints the deterministic checks (filter/dedupe/split), the payload that would
be posted, and demonstrates the event deduplication. To test another minimum
severity: `MIN_SEVERITY=high python scripts/test_webhook_local.py`.

To post for real on a test PR, set `POST_TO_GITHUB = True` and `REAL_PR_NUMBER`
at the top of the script.

### End-to-end with GitHub

1. With `uvicorn` and `ngrok` running, open a **Pull Request** on the test
   repository (or push a new commit to an open PR to fire `synchronize`).
2. In the server console you will see a line like:
   ```
   ... | INFO | pr_code_reviewer.webhook | PR #1 | action=opened | repo=your-user/test-repo | author=your-user | title=My test PR
   ```
3. In **Settings → Webhooks → Recent Deliveries** you can **Redeliver** any
   event to debug without opening new PRs.

---

## Roadmap

- [x] **Phase 1** — GitHub webhook reception and validation.
- [x] **Phase 2** — Fetch the PR diff and analyze it with an LLM (comments in the log).
- [x] **Phase 3** — Post the review comments back to the PR (`COMMENT` review).
- [x] **Phase 4** — Robustness: severity filter, deduplication, precise anchoring
  with overflow, and an anti-reprocessing cache.

---

## Known limitations

Points that are intentionally simplified for a portfolio scope and that, in real
production, would deserve improvement:

- **The anti-reprocessing cache is in-memory only** (`app/dedup_cache.py`): a
  dict in the process, with a TTL. It **resets when the server restarts** and is
  **not shared across multiple instances**. In production it would use Redis or
  a database to persist and coordinate across replicas.
- **The line number comes from the LLM:** anchoring depends on the model
  estimating the line correctly. Parsing the diff mitigates this (lines outside
  the diff go to the body), but it does not guarantee the semantically perfect
  line.
- **No retry/queueing:** if the LLM or GitHub call fails, the event is only
  logged — there is no retry with backoff nor a queue (e.g. Celery/RQ).
- **Large-diff truncation:** very large diffs are cut (~6000 tokens), so huge
  PRs may not be fully reviewed.
- **Processing via `BackgroundTasks`:** runs in the same server process; high
  load would benefit from a dedicated worker/queue.
```
