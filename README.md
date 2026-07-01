# PR Code Reviewer

Bot que revisa **Pull Requests do GitHub** automaticamente. Ele recebe eventos de PR
via webhook, envia o diff do código para um LLM e posta comentários de revisão
diretamente no Pull Request.

> **Status atual: Fase 1 — recepção e validação de webhooks.**
> Ainda **não** chama o LLM nem posta comentários. Veja o [Roadmap](#roadmap).

---

## Visão geral (produto final)

```
   GitHub PR (opened/synchronize/reopened)
            │  webhook (HTTP POST)
            ▼
   ┌─────────────────────┐
   │  pr-code-reviewer    │  FastAPI
   │  1. valida HMAC      │
   │  2. busca o diff     │  ── GitHub API ──►
   │  3. envia ao LLM     │  ── LLM ──►
   │  4. posta comentários│  ── GitHub API ──►
   └─────────────────────┘
```

O objetivo é ter um revisor automático que comenta problemas de código, sugestões
e boas práticas em cada PR aberto ou atualizado.

---

## O que já funciona (Fase 1)

- Endpoint `POST /webhook/github` que recebe eventos do GitHub.
- **Validação da assinatura HMAC-SHA256** (`X-Hub-Signature-256`) usando o
  `GITHUB_WEBHOOK_SECRET` — garante que o payload veio mesmo do GitHub.
- Filtragem de eventos `pull_request` nas ações **opened**, **synchronize** e
  **reopened**.
- Log no console com: número do PR, título, repositório, autor e ação.
- Resposta `200 OK` rápida (o GitHub espera resposta em poucos segundos).
- Função `github_client.get_pull_request_diff(...)` já pronta para buscar o diff
  (ainda não é chamada — será usada na Fase 2).

---

## Estrutura do projeto

```
pr-code-reviewer/
├── app/
│   ├── __init__.py
│   ├── main.py           # App FastAPI + rotas de status
│   ├── webhook.py        # Rota que recebe/valida os eventos do GitHub
│   ├── github_client.py  # Funções para chamar a API do GitHub (buscar diff)
│   └── config.py         # Carregamento de variáveis de ambiente
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Como rodar localmente

Requer **Python 3.12+**.

### 1. Criar o ambiente virtual e instalar dependências

```bash
cd ~/pr-code-reviewer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configurar as variáveis de ambiente

```bash
cp .env.example .env
```

Edite o `.env` e preencha:

- `GITHUB_WEBHOOK_SECRET` — um segredo forte e aleatório. Gere com:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
  (o **mesmo** valor precisa ser colado no GitHub ao criar o webhook).
- `GITHUB_TOKEN` — um Personal Access Token do GitHub (veja abaixo).

### 3. Subir o servidor

```bash
uvicorn app.main:app --reload --port 8000
```

Teste que está no ar:

```bash
curl http://localhost:8000/health
# {"status":"healthy"}
```

### 4. Expor a porta com ngrok

O GitHub precisa alcançar seu servidor pela internet. Com o
[ngrok](https://ngrok.com/) instalado:

```bash
ngrok http 8000
```

Copie a URL pública gerada (ex.: `https://abcd-1234.ngrok-free.app`). O seu
endpoint de webhook será:

```
https://abcd-1234.ngrok-free.app/webhook/github
```

> A URL gratuita do ngrok muda a cada reinício — atualize o webhook no GitHub
> quando isso acontecer.

---

## Como configurar o GitHub Webhook

1. Crie (ou use) um repositório de teste no seu GitHub.
2. Vá em **Settings → Webhooks → Add webhook**.
3. Preencha:
   - **Payload URL:** a URL do ngrok + `/webhook/github`
     (ex.: `https://abcd-1234.ngrok-free.app/webhook/github`).
   - **Content type:** `application/json`.
   - **Secret:** o **mesmo** valor de `GITHUB_WEBHOOK_SECRET` do seu `.env`.
   - **Which events?** → **Let me select individual events** → marque apenas
     **Pull requests** (desmarque "Pushes" se estiver marcado).
   - Deixe **Active** ligado.
4. Clique em **Add webhook**. O GitHub envia um evento `ping` — o servidor
   responde `{"msg":"pong"}` e você verá um ✅ na aba **Recent Deliveries**.

### Criar o Personal Access Token (`GITHUB_TOKEN`)

Em **Settings → Developer settings → Personal access tokens**:

- **Classic:** marque o escopo `repo` (ou `public_repo` se o repositório for
  público).
- **Fine-grained:** dê acesso ao repositório de teste com permissão de leitura
  em **Pull requests** e **Contents**.

Cole o token em `GITHUB_TOKEN` no `.env`.

---

## Testar

1. Com o `uvicorn` e o `ngrok` rodando, abra um **Pull Request** no repositório
   de teste (ou faça um novo commit em um PR já aberto para disparar
   `synchronize`).
2. No console do servidor você verá uma linha como:
   ```
   ... | INFO | pr_code_reviewer.webhook | PR #1 | ação=opened | repo=seu-user/repo-teste | autor=seu-user | título=Meu PR de teste
   ```
3. Em **Settings → Webhooks → Recent Deliveries** você pode reenviar
   (**Redeliver**) qualquer evento para depurar sem abrir novos PRs.

---

## Roadmap

- [x] **Fase 1** — Recepção e validação de webhooks do GitHub.
- [ ] **Fase 2** — Buscar o diff do PR e enviar ao LLM.
- [ ] **Fase 3** — Postar os comentários de revisão de volta no PR.
- [ ] **Fase 4** — Refinos: comentários por linha, filtros, deduplicação.

<!-- teste: validação do webhook em Wed Jul  1 20:25:28 -03 2026 -->
