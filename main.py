"""main.py — FastAPI backend for the Repo Health Taskmaster Agent.

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

import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import agent as taskmaster

logger = logging.getLogger("taskmaster.api")

app = FastAPI(
    title="Repo Health Taskmaster Agent",
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
    patch: Optional[taskmaster.PatchProposal] = None
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
    patch: Optional[taskmaster.PatchProposal] = None
    message: str


# ---------------------------------------------------------------------------
# Health / metadata
# ---------------------------------------------------------------------------
@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    """Liveness probe for Cloud Run."""
    return {
        "status": "ok",
        "model": taskmaster.GEMINI_MODEL,
        "collection": taskmaster.FIRESTORE_COLLECTION,
    }


@app.get("/", tags=["ops"])
def root() -> dict:
    """Human-friendly service banner."""
    return {
        "service": "Repo Health Taskmaster Agent",
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
def webhook_analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    """Ingest a repo issue, run the agent, and gate the fix behind approval.

    Returns the generated patch plus a single-use ``approval_token`` that a
    human must present to ``/workflow/approve`` before any PR is created.
    """
    try:
        record = taskmaster.ingest_and_analyze(
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
        record = taskmaster.approve_and_execute(payload.task_id, payload.approval_token)
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
        record = taskmaster.reject_task(
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
    record = taskmaster.repository.get(task_id)
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
