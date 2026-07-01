# PR Code Reviewer

Bot que revisa **Pull Requests do GitHub** automaticamente. Ele recebe eventos de PR
via webhook, envia o diff do código para um LLM e posta comentários de revisão
diretamente no Pull Request.

> **Status atual: Fase 3 — postagem da review no PR.**
> Fluxo completo: recebe o PR → busca o diff → gera comentários com um LLM →
> **posta a review de volta no Pull Request** (event `COMMENT`, sem aprovar nem
> bloquear). Veja o [Roadmap](#roadmap).

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

## O que já funciona

### Fase 1 — Recepção e validação de webhooks

- Endpoint `POST /webhook/github` que recebe eventos do GitHub.
- **Validação da assinatura HMAC-SHA256** (`X-Hub-Signature-256`) usando o
  `GITHUB_WEBHOOK_SECRET` — garante que o payload veio mesmo do GitHub.
- Filtragem de eventos `pull_request` nas ações **opened**, **synchronize** e
  **reopened**.
- Log no console com: número do PR, título, repositório, autor e ação.
- Resposta `200 OK` rápida (o GitHub espera resposta em poucos segundos).

### Fase 2 — Busca do diff + análise via LLM

- Para cada evento relevante, busca o **diff real** do PR via GitHub API
  (`github_client.get_pull_request_diff`).
- Envia o diff a um LLM da OpenAI (`gpt-4o-mini` por padrão) que atua como
  **revisor de código sênior**, procurando bugs, riscos de segurança, más
  práticas, código duplicado e problemas de performance
  (`llm_reviewer.review_diff`).
- O modelo responde **em JSON**; a resposta é parseada com tratamento de erro
  (JSON malformado → loga e retorna lista vazia, sem quebrar o app).
- Diffs muito grandes são **truncados** (~6000 tokens) para não estourar o
  contexto nem gastar demais.
- A busca do diff + revisão rodam em **background** (`BackgroundTasks`), então o
  webhook continua respondendo `200 OK` de imediato ao GitHub.
- Os comentários gerados (arquivo, linha aproximada, severidade, comentário)
  são **logados no console**.

### Fase 3 — Postagem da review no PR

- Posta os comentários de volta no PR via
  `POST /repos/{owner}/{repo}/pulls/{n}/reviews`
  (`github_client.submit_pr_review`), como **comentários por linha**.
- Usa sempre `event="COMMENT"` — o bot **só sugere**, nunca aprova (`APPROVE`)
  nem bloqueia (`REQUEST_CHANGES`) o PR.
- Cada comentário leva um emoji de severidade: 🔴 alta, 🟡 média, 🟢 baixa.
- Se **não houver problemas**, posta uma review positiva simples:
  `✅ Revisão automática: nenhum problema crítico encontrado.`
- **Fallback:** a API do GitHub só aceita comentários em linhas que fazem parte
  do diff. Se a review por linha for rejeitada (HTTP 422), o bot reposta tudo
  como uma **review geral** (comentários no corpo), sem derrubar a aplicação.
- Requer `GITHUB_TOKEN` com permissão **Pull requests: Read and write**.

---

## Estrutura do projeto

```
pr-code-reviewer/
├── app/
│   ├── __init__.py
│   ├── main.py           # App FastAPI + rotas de status
│   ├── webhook.py        # Rota que recebe/valida os eventos + orquestra a revisão
│   ├── github_client.py  # Funções para chamar a API do GitHub (buscar diff)
│   ├── llm_reviewer.py   # Revisão de código via LLM (OpenAI)
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
- `OPENAI_API_KEY` — chave da API da OpenAI, usada na revisão via LLM.
  Gere em <https://platform.openai.com/api-keys>.
- `OPENAI_MODEL` *(opcional)* — modelo usado na revisão. Padrão: `gpt-4o-mini`.

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
- **Fine-grained:** dê acesso ao repositório de teste com **Contents: Read** e
  **Pull requests: Read and write** (a escrita é necessária a partir da Fase 3
  para o bot postar a review no PR).

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
- [x] **Fase 2** — Buscar o diff do PR e analisá-lo com um LLM (comentários no log).
- [x] **Fase 3** — Postar os comentários de revisão de volta no PR (review `COMMENT`).
- [ ] **Fase 4** — Refinos: deduplicação, filtros por severidade, retry por comentário.
