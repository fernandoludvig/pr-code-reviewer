"""Teste local do fluxo completo do webhook — SEM abrir um PR real.

Simula uma entrega de webhook do GitHub (evento pull_request/opened) com
assinatura HMAC válida, mockando apenas a busca do diff (github_client), para
não depender da API do GitHub. O restante roda de verdade: validação HMAC,
BackgroundTasks e a revisão via LLM (review_diff faz uma chamada real à OpenAI).

Uso:
    source .venv/bin/activate
    python scripts/test_webhook_local.py
"""

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

from app import github_client
from app.config import settings
from app.main import app

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

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

payload = {
    "action": "opened",
    "pull_request": {
        "number": 999,
        "title": "PR de teste local",
        "user": {"login": "fernandoludvig"},
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
    print(f"[mock] get_pull_request_diff({owner}, {repo}, {pull_number}) -> diff fake")
    return FAKE_DIFF


# TestClient executa as BackgroundTasks de forma síncrona após a resposta,
# então os logs da revisão aparecem logo após o POST retornar.
with patch.object(github_client, "get_pull_request_diff", fake_get_diff):
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
    print(f"\nHTTP {resp.status_code} -> {resp.json()}")
