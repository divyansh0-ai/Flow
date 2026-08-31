# Flow agent

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688)](https://fastapi.tiangolo.com/)
[![Google ADK](https://img.shields.io/badge/agent-Google%20ADK%20%2B%20Gemini-4285F4)](https://google.github.io/adk-docs/)
[![Firestore](https://img.shields.io/badge/state-Cloud%20Firestore-FFA000)](https://cloud.google.com/firestore)
[![Cloud Run](https://img.shields.io/badge/deploy-Cloud%20Run-4285F4)](https://cloud.google.com/run)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

> An autonomous, **Human-in-the-Loop (HITL)** repository-maintenance agent built
> with the **Google Agent Development Kit (ADK)** + **Gemini**, backed by
> **Cloud Firestore**, and packaged for **Google Cloud Run**.
>
> Built for the **All Things Agentic Hackathon**.

The agent ingests a repository error/issue, uses Gemini to **diagnose the root
cause and generate a structured patch**, then **pauses for human approval**
before taking any outward action. Nothing gets "pushed" until a human says yes.

---

## Table of contents

1. [Architecture](#1-architecture)
2. [Project structure](#2-project-structure)
3. [Prerequisites](#3-prerequisites)
4. [Quickstart — run the demo on your laptop](#4-quickstart--run-the-demo-on-your-laptop)
5. [API reference](#5-api-reference)
6. [Deploy to Google Cloud Run](#6-deploy-to-google-cloud-run)
7. [Why this satisfies the challenge](#7-why-this-satisfies-the-challenge)
8. [Security notes](#8-security-notes)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Architecture

```
                                   ┌──────────────────────────────────────┐
   GitHub / CI  ──(webhook)──▶     │        FastAPI  (Cloud Run)          │
   issue + stack trace             │                                      │
                                   │  POST /webhook/analyze               │
                                   │     1. create task  (RECEIVED)       │
                                   │     2. run ADK agent (ANALYZING)     │
                                   │     3. store patch  (PENDING_APPROVAL)│
                                   └───────────────┬──────────────────────┘
                                                   │
                            ┌──────────────────────▼───────────────────────┐
                            │            Google ADK Agent                   │
                            │   model: gemini-3.5-flash                     │
                            │   tools: analyze_error_log, build_unified_diff│
                            │   output: structured PatchProposal (JSON)     │
                            └──────────────────────┬───────────────────────┘
                                                   │
                            ┌──────────────────────▼───────────────────────┐
                            │            Cloud Firestore                    │
                            │   collection: flow_agent_tasks               │
                            │   fields: status, patch, approval_token,      │
                            │           history[] (full audit trail)        │
                            └──────────────────────┬───────────────────────┘
                                                   │
                    ┌──────────────── HUMAN-IN-THE-LOOP GATE ───────────────┐
                    │  approve_cli.py  (terminal prompt)                     │
                    │        or                                             │
                    │  POST /workflow/approve  { task_id, approval_token }   │
                    └──────────────────────┬────────────────────────────────┘
                                           │  (approved)
                                           ▼
                            ┌──────────────────────────────────────────────┐
                            │  Execute final action: mock GitHub PR create  │
                            │  Firestore status ──▶ PR_CREATED              │
                            └──────────────────────────────────────────────┘
```

### State machine

```
RECEIVED → ANALYZING → PENDING_APPROVAL → APPROVED → PR_CREATED
                              │
                              ├──▶ REJECTED   (human declines)
                              └──▶ FAILED     (error during analysis / PR)
```

> **Resilience note:** If the Firestore client or the Gemini API key is not
> available (e.g. a quick local demo), the service automatically falls back to
> an **in-memory store** and a **deterministic heuristic patcher** so the full
> workflow still runs end-to-end. In Cloud Run, real Firestore + Gemini are used.

---

## 2. Project structure

```
flow/
├── agent.py            # ADK agent, tools, Firestore repository, orchestration
├── main.py              # FastAPI app (webhook + approval endpoints)
├── github_client.py      # Real GitHub REST API client (read files, branch, commit, PR)
├── approve_cli.py        # Interactive terminal HITL approval console
├── requirements.txt      # Python dependencies
├── Dockerfile            # Cloud Run container (non-root, $PORT-aware)
├── .dockerignore
├── .gitignore
└── README.md
```

| File                | Responsibility                                                         |
| ------------------- | ------------------------------------------------------------------------ |
| `agent.py`           | ADK agent, tools, Firestore repository, workflow orchestration          |
| `main.py`            | FastAPI app: `/webhook/analyze`, `/workflow/approve`, `/workflow/reject`  |
| `github_client.py`    | Real GitHub API: fetch files, create branch, commit patch, open PR      |
| `approve_cli.py`      | Interactive terminal HITL approval console                              |
| `requirements.txt`    | Python dependencies                                                     |
| `Dockerfile`          | Cloud Run container (non-root, `$PORT`-aware)                            |

---

## 3. Prerequisites

- Python 3.12+
- A **Gemini API key** — https://aistudio.google.com/apikey (optional locally — the app degrades gracefully without one)
- (For persistence/deploy) A **Google Cloud project** with Firestore + Cloud Run enabled

### Environment variables

| Variable                | Purpose                                   | Default              |
| ------------------------ | ------------------------------------------ | ---------------------- |
| `GOOGLE_API_KEY`         | Gemini API key (or `GEMINI_API_KEY`)        | — (falls back)         |
| `GOOGLE_CLOUD_PROJECT`   | GCP project id for Firestore                | ADC default            |
| `GEMINI_MODEL`           | Model name                                  | `gemini-3.5-flash`     |
| `FIRESTORE_COLLECTION`   | Firestore collection name                   | `flow_agent_tasks`    |
| `GITHUB_TOKEN`           | GitHub PAT — **enables real repo mode**      | — (mock PR fallback)   |
| `GITHUB_WEBHOOK_SECRET`  | HMAC secret for webhook signature checks     | — (verification off)   |
| `GITHUB_BRANCH_PREFIX`   | Prefix for generated fix branches            | `flow/fix`       |
| `PORT`                   | Server port (Cloud Run injects this)        | `8080`                 |

---

## 4. Quickstart — run the demo on your laptop

```bash
# 1. Clone the repo
git clone https://github.com/divyansh0-ai/Flow.git
cd Flow

# 2. Create a virtual environment (optional but recommended)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Set your Gemini API key to use the real ADK/Gemini agent
#    instead of the offline heuristic fallback
export GOOGLE_API_KEY="your-gemini-api-key"          # Windows PowerShell: $env:GOOGLE_API_KEY="..."

# 5. Start the server
uvicorn main:app --reload --port 8080
# Windows note: if `uvicorn` isn't recognized (its script dir isn't on PATH),
# run it as a module instead:
#   python -m uvicorn main:app --reload --port 8080
```

Open **http://localhost:8080/docs** for the interactive Swagger UI, or drive
the full demo from the terminal:

### Step 1 — Trigger analysis (creates a `PENDING_APPROVAL` task)

```bash
curl -s -X POST http://localhost:8080/webhook/analyze \
  -H "Content-Type: application/json" \
  -d '{
        "repo": "octocat/hello-world",
        "issue_title": "TypeError: unsupported operand in totals()",
        "issue_body": "Crashes when summing an empty cart.",
        "error_log": "Traceback (most recent call last):\n  File \"src/cart.py\", line 42, in totals\n    return sum(items) / len(items)\nZeroDivisionError: division by zero",
        "source_code": "def totals(items):\n    return sum(items) / len(items)\n"
      }'
```

The response contains a `task_id`, an `approval_token`, and the proposed `patch`:

```json
{
  "task_id": "9f2c...",
  "status": "PENDING_APPROVAL",
  "approval_token": "R8s7...-token",
  "patch": { "summary": "...", "file_path": "src/cart.py", "unified_diff": "..." },
  "message": "Patch generated and stored as PENDING_APPROVAL. ..."
}
```

### Step 2 — Human approval (HITL gate)

**Option A — Interactive terminal prompt** (best for a live demo — it prints
the diff and asks `Approve this fix and create the PR? [y/N]`):

```bash
python approve_cli.py --base-url http://localhost:8080
# prompts for task_id and approval_token if not passed as flags:
python approve_cli.py --base-url http://localhost:8080 \
  --task-id <TASK_ID> --token <APPROVAL_TOKEN>
```

**Option B — Direct API call:**

```bash
curl -s -X POST http://localhost:8080/workflow/approve \
  -H "Content-Type: application/json" \
  -d '{"task_id": "<TASK_ID>", "approval_token": "<APPROVAL_TOKEN>"}'
```

Either path transitions the task to `PR_CREATED` and returns the (mock) `pr_url`.

### Inspect the audit trail

```bash
curl -s http://localhost:8080/tasks/<TASK_ID>
```

Returns the full record, including the `history[]` audit trail (the approval
token is redacted on read).

### Demo tips

- **No `GOOGLE_CLOUD_PROJECT` / Firestore credentials?** The service
  transparently falls back to an in-memory store — still fully functional for
  a live demo, just not persistent across restarts.
- **No `GOOGLE_API_KEY`?** Falls back to a deterministic heuristic patcher so
  the workflow still completes end-to-end. Set the key beforehand if you want
  to show genuine Gemini reasoning in the patch's `root_cause` / `summary`.
- Restarting the server clears the in-memory store — generate a fresh
  `task_id`/`approval_token` pair via `/webhook/analyze` after every restart.

---

## 4b. Real repository mode

By default the agent runs in **mock mode**: it analyzes whatever `source_code`
you pass in the webhook and synthesizes a fake PR URL. Setting a
**`GITHUB_TOKEN`** switches it to **real mode**, where it:

1. **Reads real code** — localizes the failing file from the traceback,
   resolves it against the repo tree, and fetches the actual file contents via
   the GitHub Contents API (so you can omit `source_code` entirely).
2. **Opens a real pull request** — after human approval, it creates a fix
   branch, commits the patched file, and opens a genuine PR with the diff,
   root-cause analysis, and agent confidence in the body.

### Setup

Create a token at **https://github.com/settings/tokens**:

- *Classic:* `repo` scope.
- *Fine-grained:* Repository permissions → **Contents: Read and write** and
  **Pull requests: Read and write**.

```bash
export GITHUB_TOKEN="ghp_your_token_here"      # PowerShell: $env:GITHUB_TOKEN="ghp_..."
python -m uvicorn main:app --port 8080
```

Confirm the mode is active:

```bash
curl -s http://localhost:8080/healthz
# {"status":"ok", ..., "github_mode":"real", ...}
```

Now analyze a real bug **without** passing any source code — the agent fetches
it from the repo itself:

```bash
curl -s -X POST http://localhost:8080/webhook/analyze \
  -H "Content-Type: application/json" \
  -d '{
        "repo": "your-user/your-repo",
        "issue_title": "ZeroDivisionError in calculate_average()",
        "issue_body": "Crashes on an empty scores list.",
        "error_log": "Traceback (most recent call last):\n  File \"src/stats.py\", line 7, in calculate_average\n    return sum(scores) / len(scores)\nZeroDivisionError: division by zero"
      }'
```

Approve it, and a real PR appears on the repository.

> ⚠️ **This writes to a live repository.** The human approval gate is the only
> thing standing between the model's proposal and a real branch + PR. Point it
> at a test repo first. Nothing is written during analysis — reads only.

### Securing the webhook

Set a shared secret and the service will reject payloads whose
`X-Hub-Signature-256` HMAC doesn't match (matching GitHub's webhook scheme):

```bash
export GITHUB_WEBHOOK_SECRET="your-random-secret"
```

---

## 5. API reference

| Method | Path                | Description                                                         |
| ------ | -------------------- | ---------------------------------------------------------------------- |
| `GET`  | `/healthz`            | Liveness probe — returns configured model + Firestore collection      |
| `GET`  | `/`                   | Service banner with links to key endpoints                            |
| `POST` | `/webhook/analyze`     | Ingest an issue, run the agent, store `PENDING_APPROVAL` + patch       |
| `POST` | `/workflow/approve`    | Approve a task by `task_id` + `approval_token` → creates mock PR       |
| `POST` | `/workflow/reject`     | Reject a task by `task_id` + `approval_token` (+ optional `reason`)    |
| `GET`  | `/tasks/{task_id}`     | Fetch a task's full record + audit `history[]` (token redacted)        |

Full request/response schemas are available live at `/docs` (Swagger) or
`/redoc`.

---

## 6. Deploy to Google Cloud Run

```bash
# 0. Set your project + region
export PROJECT_ID="your-gcp-project"
export REGION="us-central1"
gcloud config set project "$PROJECT_ID"

# 1. Enable required services
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

# 2. Create the Firestore database (Native mode) — one-time
gcloud firestore databases create --location="$REGION"

# 3. Store the Gemini key in Secret Manager (recommended)
gcloud services enable secretmanager.googleapis.com
printf "your-gemini-api-key" | gcloud secrets create gemini-api-key --data-file=-

# 4. Build + deploy straight from source (Cloud Build uses the Dockerfile)
gcloud run deploy flow-agent \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GEMINI_MODEL=gemini-3.5-flash" \
  --update-secrets "GOOGLE_API_KEY=gemini-api-key:latest"
```

Cloud Run automatically supplies Application Default Credentials, so the
Firestore client authenticates without a key file. The deploy command prints a
service URL — point your GitHub/CI webhook at
`https://<service-url>/webhook/analyze`.

### Alternative: build the image explicitly

```bash
gcloud builds submit --tag "gcr.io/$PROJECT_ID/flow-agent"
gcloud run deploy flow-agent \
  --image "gcr.io/$PROJECT_ID/flow-agent" \
  --region "$REGION" --allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID" \
  --update-secrets "GOOGLE_API_KEY=gemini-api-key:latest"
```

---

## 7. Why this satisfies the challenge

| Requirement                             | Where                                                        |
| ----------------------------------------- | --------------------------------------------------------------- |
| Google ADK + Gemini (2.5-pro)             | `agent.py` → `build_agent()` / `_run_adk_agent()`               |
| Firestore persistent state / audit        | `agent.py` → `FirestoreRepository`, `history[]` on each doc      |
| FastAPI + Docker + Cloud Run              | `main.py`, `Dockerfile`, deploy commands above                    |
| Ingest via webhook                        | `POST /webhook/analyze`                                          |
| Analyze + generate structured patch       | `generate_patch()` → `PatchProposal`                              |
| HITL gatekeeper (PENDING_APPROVAL)        | `ingest_and_analyze()` + single-use `approval_token`               |
| Approve → execute → PR_CREATED            | `POST /workflow/approve` → `approve_and_execute()`                 |
| Real GitHub PR creation (beyond the mock) | `github_client.py` → `create_real_github_pr()`                     |
| Webhook authenticity                      | `main.py` → `verify_github_signature()` (HMAC-SHA256)              |

---

## 8. Security notes

- The `approval_token` is a single-use, cryptographically-random secret
  (`secrets.token_urlsafe`) compared in constant time (`secrets.compare_digest`).
- Tokens are never returned on `GET /tasks/{id}`.
- No outward action (PR creation) can occur before an explicit human approval.
- For production, restrict Cloud Run ingress and require authenticated invokers
  instead of `--allow-unauthenticated`.

---

## 9. Troubleshooting

| Symptom                                                        | Cause / Fix                                                                                                                             |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `uvicorn : term not recognized` (Windows)                        | The `uvicorn` console script isn't on `PATH`. Run `python -m uvicorn main:app --reload --port 8080` instead.                              |
| `WinError 10013` on startup                                       | Port 8080 is already bound by another running server process. Check with `Get-NetTCPConnection -LocalPort 8080`, stop the old process, or use `--port 8000`. |
| `Task '<id>' is 'REJECTED'/'PR_CREATED', not PENDING_APPROVAL`     | The state machine is working correctly — a task can only be approved once, from `PENDING_APPROVAL`. Generate a fresh task via `/webhook/analyze`. |
| Browser tab shows `ERR_CONNECTION_REFUSED` even though the server is up | Usually a stale cached connection state from before the server started/restarted. Hard-reload (`Ctrl+Shift+R`) or open a new tab.           |
| `Firestore unavailable ... Falling back to in-memory store` in logs | Expected when running locally without `GOOGLE_CLOUD_PROJECT` / Application Default Credentials — not an error, just a local-dev fallback.    |

---

## License

See [LICENSE](LICENSE).
