"""Teste local do fluxo do webhook — com dois modos.

MODO SEGURO (padrão, POST_TO_GITHUB = False):
    Roda o fluxo completo OFFLINE: HMAC + filtro + BackgroundTasks + review via
    LLM (chamada real à OpenAI), mas SEM tocar no GitHub. A busca do diff é
    mockada e a postagem da review é interceptada e apenas IMPRESSA — mostra
    exatamente o payload que seria enviado ao GitHub.

MODO REAL (POST_TO_GITHUB = True):
    ⚠️ Posta comentários DE VERDADE num PR real. Ajuste REAL_OWNER/REAL_REPO/
    REAL_PR_NUMBER para o seu PR de teste. Busca o diff real, roda a revisão e
    chama submit_pr_review() sem mocks.

Uso:
    source .venv/bin/activate
    python scripts/test_webhook_local.py
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
from unittest.mock import patch

# Garante que a raiz do projeto está no sys.path, mesmo rodando de scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app import github_client, webhook
from app.config import settings
from app.main import app

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO — mude aqui para ativar o modo real.
# ---------------------------------------------------------------------------
POST_TO_GITHUB = False  # ⚠️ True = posta comentário REAL no PR abaixo.
REAL_OWNER = "fernandoludvig"
REAL_REPO = "pr-code-reviewer"
REAL_PR_NUMBER = 2  # ajuste para o número do seu PR de teste.

# Diff de exemplo com problemas propositais (SQL injection + senha hardcoded).
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


def run_safe_mode() -> None:
    """Fluxo completo offline; a postagem no GitHub é apenas simulada/impressa."""
    payload = {
        "action": "opened",
        "pull_request": {
            "number": 999,
            "title": "PR de teste local",
            "user": {"login": "fernandoludvig"},
            "head": {"sha": "fakesha1234567890"},
        },
        "repository": {
            "full_name": "fernandoludvig/pr-code-reviewer",
            "name": "pr-code-reviewer",
            "owner": {"login": "fernandoludvig"},
        },
    }
    body = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(
        settings.GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()

    async def fake_get_diff(owner, repo, pull_number):
        print(f"[mock] get_pull_request_diff({owner}, {repo}, {pull_number})")
        return FAKE_DIFF

    async def fake_submit_review(owner, repo, pull_number, comments, commit_id, **kw):
        print("\n===== PAYLOAD QUE SERIA POSTADO NO GITHUB (mock) =====")
        print(json.dumps(
            {"commit_id": commit_id, "event": kw.get("event", "COMMENT"),
             "body": kw.get("body", "🤖 Revisão automática de código"),
             "comments": comments},
            indent=2, ensure_ascii=False,
        ))
        print("======================================================\n")
        return {"ok": True, "fallback": False, "posted_line_comments": len(comments)}

    with patch.object(github_client, "get_pull_request_diff", fake_get_diff), \
         patch.object(github_client, "submit_pr_review", fake_submit_review):
        client = TestClient(app)
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


def run_real_mode() -> None:
    """⚠️ Fluxo REAL: busca o diff, revisa e POSTA a review no PR de verdade."""
    print(
        f"⚠️  MODO REAL — vai postar comentários no PR #{REAL_PR_NUMBER} de "
        f"{REAL_OWNER}/{REAL_REPO}."
    )
    asyncio.run(
        webhook.process_pull_request(
            REAL_OWNER, REAL_REPO, REAL_PR_NUMBER, "Teste de review real", None
        )
    )


if __name__ == "__main__":
    if POST_TO_GITHUB:
        run_real_mode()
    else:
        print("MODO SEGURO (offline). Para postar de verdade, defina "
              "POST_TO_GITHUB = True no topo do script.\n")
        run_safe_mode()
