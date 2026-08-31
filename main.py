"""main.py — FastAPI backend for the Flow Agent.

Exposes the Human-in-the-Loop (HITL) workflow over HTTP so it can run on
Google Cloud Run:

    POST /webhook/analyze  -> ingest an issue, run the agent, park at
                              PENDING_APPROVAL, and return an approval token.
    POST /workflow/approve -> validate the token, create the (mock) PR, and
                              transition the task to PR_CREATED.
    POST /workflow/reject  -> the negative HITL path.
    GET  /tasks/{task_id}  -> inspect a task's current state and audit trail.
    GET  /healthz          -> Cloud Run liveness probe.

Run locally:
    uvicorn main:app --reload --port 8080
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import agent as flow

# Shared secret configured on the GitHub webhook. When set, every inbound
# payload must carry a valid X-Hub-Signature-256 header.
GITHUB_WEBHOOK_SECRET: Optional[str] = os.getenv("GITHUB_WEBHOOK_SECRET")

logger = logging.getLogger("flow.api")

app = FastAPI(
    title="Flow Agent",
    description=(
        "Autonomous repo-maintenance agent (Google ADK + Gemini) with a "
        "Human-in-the-Loop approval gate backed by Firestore."
    ),
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    """Payload for POST /webhook/analyze."""

    repo: str = Field(..., examples=["octocat/hello-world"], description="owner/name")
    issue_title: str = Field(..., description="Short title of the reported issue.")
    issue_body: str = Field(default="", description="Full issue description.")
    error_log: str = Field(default="", description="Traceback or CI failure output.")
    source_code: str = Field(default="", description="Relevant source file contents.")


class AnalyzeResponse(BaseModel):
    task_id: str
    status: str
    approval_token: str
    patch: Optional[flow.PatchProposal] = None
    message: str


class ApproveRequest(BaseModel):
    """Payload for POST /workflow/approve and /workflow/reject."""

    task_id: str
    approval_token: str
    reason: str = Field(default="", description="Optional note (used on reject).")


class TaskResponse(BaseModel):
    task_id: str
    status: str
    pr_url: str = ""
    patch: Optional[flow.PatchProposal] = None
    message: str


# ---------------------------------------------------------------------------
# Webhook authenticity
# ---------------------------------------------------------------------------
def verify_github_signature(body: bytes, signature: Optional[str]) -> None:
    """Validate GitHub's HMAC-SHA256 webhook signature.

    No-op when ``GITHUB_WEBHOOK_SECRET`` is unset (local development).

    Raises:
        HTTPException: 401 when the signature is missing or does not match.
    """
    if not GITHUB_WEBHOOK_SECRET:
        return

    if not signature:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Missing X-Hub-Signature-256 header."
        )

    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature."
        )


# ---------------------------------------------------------------------------
# Health / metadata
# ---------------------------------------------------------------------------
# Google's frontend intercepts /healthz on Cloud Run before it reaches the
# container, so /health is the canonical probe and /healthz is kept as an
# alias for local use.
@app.get("/health", tags=["ops"])
@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    """Liveness probe. Reports which backing services are actually live."""
    return {
        "status": "ok",
        "model": flow.GEMINI_MODEL,
        "state_backend": flow.repository.backend,
        "collection": flow.FIRESTORE_COLLECTION,
        "github_mode": "real" if flow.is_github_configured() else "mock",
        "webhook_signature_verification": bool(GITHUB_WEBHOOK_SECRET),
    }


@app.get("/", tags=["ops"])
def root() -> dict:
    """Human-friendly service banner."""
    return {
        "service": "Flow Agent",
        "docs": "/docs",
        "endpoints": ["/webhook/analyze", "/workflow/approve", "/workflow/reject"],
    }


# ---------------------------------------------------------------------------
# Step 1 + 2 + 3: ingest, analyze, park at PENDING_APPROVAL
# ---------------------------------------------------------------------------
@app.post(
    "/webhook/analyze",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["workflow"],
)
async def webhook_analyze(
    payload: AnalyzeRequest,
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
) -> AnalyzeResponse:
    """Ingest a repo issue, run the agent, and gate the fix behind approval.

    Returns the generated patch plus a single-use ``approval_token`` that a
    human must present to ``/workflow/approve`` before any PR is created.
    """
    verify_github_signature(await request.body(), x_hub_signature_256)

    try:
        record = flow.ingest_and_analyze(
            repo=payload.repo,
            issue_title=payload.issue_title,
            issue_body=payload.issue_body,
            error_log=payload.error_log,
            source_code=payload.source_code,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Analysis failed.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Analysis failed: {exc}",
        ) from exc

    return AnalyzeResponse(
        task_id=record.task_id,
        status=record.status.value,
        approval_token=record.approval_token,
        patch=record.patch,
        message=(
            "Patch generated and stored as PENDING_APPROVAL. "
            "Approve via POST /workflow/approve with this task_id + approval_token."
        ),
    )


# ---------------------------------------------------------------------------
# Step 4: human approval -> execute final action -> PR_CREATED
# ---------------------------------------------------------------------------
@app.post("/workflow/approve", response_model=TaskResponse, tags=["workflow"])
def workflow_approve(payload: ApproveRequest) -> TaskResponse:
    """Approve a pending task; the agent then creates the (mock) pull request."""
    try:
        record = flow.approve_and_execute(payload.task_id, payload.approval_token)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Approval failed.")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return TaskResponse(
        task_id=record.task_id,
        status=record.status.value,
        pr_url=record.pr_url,
        patch=record.patch,
        message=f"Approved and executed. Pull request created: {record.pr_url}",
    )


@app.post("/workflow/reject", response_model=TaskResponse, tags=["workflow"])
def workflow_reject(payload: ApproveRequest) -> TaskResponse:
    """Reject a pending task (negative HITL path); no PR is created."""
    try:
        record = flow.reject_task(
            payload.task_id, payload.approval_token, payload.reason
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rejection failed.")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return TaskResponse(
        task_id=record.task_id,
        status=record.status.value,
        patch=record.patch,
        message="Task rejected; no pull request was created.",
    )


# ---------------------------------------------------------------------------
# Inspection / audit trail
# ---------------------------------------------------------------------------
@app.get("/tasks/{task_id}", tags=["workflow"])
def get_task(task_id: str) -> JSONResponse:
    """Return the full task record, including the Firestore audit history."""
    record = flow.repository.get(task_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Task not found.")
    # Never leak the approval token on read.
    data = record.model_dump(mode="json")
    data.pop("approval_token", None)
    return JSONResponse(content=data)


# ---------------------------------------------------------------------------
# Local entrypoint (Cloud Run uses the Dockerfile CMD instead)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
