r"""agent.py — Core intelligence for the Flow Agent.

This module wires together three concerns:

1. **Firestore persistence** — every incoming issue becomes a durable
   ``tasks`` document whose ``status`` field drives the Human-in-the-Loop
   (HITL) state machine and doubles as an audit trail.

2. **The Google ADK agent** — a Gemini-backed :class:`google.adk.agents.Agent`
   that reasons about a reported bug and produces a structured patch proposal.

3. **Deterministic tools + orchestration helpers** — small, fully typed
   functions the agent (and the FastAPI layer) call to analyze code, draft a
   patch, and — after human approval — "create" a GitHub pull request.

The design keeps side effects explicit: the LLM proposes, Firestore records,
and no outward action (PR creation) happens until a human approves.

State machine
-------------
    RECEIVED  ->  ANALYZING  ->  PENDING_APPROVAL  ->  APPROVED  ->  PR_CREATED
                                        \-> REJECTED
                                        \-> FAILED
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

import github_client
from github_client import (
    GitHubClient,
    GitHubError,
    apply_snippet_patch,
    build_pr_body,
    is_github_configured,
)

logger = logging.getLogger("flow.agent")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment for Cloud Run)
# ---------------------------------------------------------------------------
# gemini-2.5-pro is retired for new API keys; 3.5-flash is broadly available on
# the free tier. Override with GEMINI_MODEL (e.g. gemini-3.1-pro-preview) if
# your key has Pro quota.
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
FIRESTORE_COLLECTION: str = os.getenv("FIRESTORE_COLLECTION", "flow_agent_tasks")
GOOGLE_CLOUD_PROJECT: Optional[str] = os.getenv("GOOGLE_CLOUD_PROJECT")
AGENT_APP_NAME: str = "flow_agent"

# When a GITHUB_TOKEN is present the agent reads real repository files and
# opens real pull requests. Without it, it degrades to the mock PR path so the
# workflow stays fully demonstrable offline.
GITHUB_BRANCH_PREFIX: str = os.getenv("GITHUB_BRANCH_PREFIX", "flow/fix")


# ---------------------------------------------------------------------------
# Task status enum — the single source of truth for the HITL workflow
# ---------------------------------------------------------------------------
class TaskStatus(str, Enum):
    """Lifecycle states for a Flow Agent task."""

    RECEIVED = "RECEIVED"
    ANALYZING = "ANALYZING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PR_CREATED = "PR_CREATED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Typed data models
# ---------------------------------------------------------------------------
class PatchProposal(BaseModel):
    """Structured fix the agent produces for a reported bug."""

    summary: str = Field(..., description="One-line human summary of the fix.")
    root_cause: str = Field(..., description="Why the bug happens.")
    file_path: str = Field(..., description="Repo-relative file to patch.")
    original_snippet: str = Field(default="", description="Code being replaced.")
    patched_snippet: str = Field(default="", description="Replacement code.")
    unified_diff: str = Field(default="", description="Unified diff of the change.")
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Agent confidence 0..1."
    )


class TaskRecord(BaseModel):
    """The persisted shape of a task document in Firestore."""

    task_id: str
    status: TaskStatus
    repo: str
    issue_title: str
    issue_body: str
    error_log: str = ""
    approval_token: str = ""
    patch: Optional[PatchProposal] = None
    pr_url: str = ""
    # Real-repo context resolved at analysis time (empty in offline/mock mode).
    resolved_path: str = ""
    source_fetched_from_github: bool = False
    history: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Firestore integration
# ---------------------------------------------------------------------------
class FirestoreRepository:
    """Thin, typed wrapper around a Firestore collection.

    Falls back to an in-memory store when the Firestore client cannot be
    initialized (e.g. local development without credentials). This keeps the
    service runnable for demos while remaining production-ready in Cloud Run,
    where Application Default Credentials are present.
    """

    def __init__(self, collection: str = FIRESTORE_COLLECTION) -> None:
        self._collection_name = collection
        self._client: Any = None
        self._memory: Dict[str, Dict[str, Any]] = {}

        try:
            from google.cloud import firestore  # imported lazily

            self._client = (
                firestore.Client(project=GOOGLE_CLOUD_PROJECT)
                if GOOGLE_CLOUD_PROJECT
                else firestore.Client()
            )
            logger.info("Firestore client initialized (collection=%s).", collection)
        except Exception as exc:  # pragma: no cover - depends on environment
            logger.warning(
                "Firestore unavailable (%s). Falling back to in-memory store. "
                "This is fine for local demos but NOT for production.",
                exc,
            )

    # -- internal helpers ---------------------------------------------------
    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _doc_ref(self, task_id: str) -> Any:
        return self._client.collection(self._collection_name).document(task_id)

    # -- public API ---------------------------------------------------------
    def create(self, record: TaskRecord) -> TaskRecord:
        """Persist a brand-new task document."""
        record.created_at = record.created_at or self._now()
        record.updated_at = self._now()
        data = record.model_dump(mode="json")

        if self._client is not None:
            self._doc_ref(record.task_id).set(data)
        else:
            self._memory[record.task_id] = data
        logger.info("Task %s created with status %s.", record.task_id, record.status)
        return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """Fetch a task, or ``None`` if it does not exist."""
        if self._client is not None:
            snapshot = self._doc_ref(task_id).get()
            if not snapshot.exists:
                return None
            return TaskRecord.model_validate(snapshot.to_dict())

        data = self._memory.get(task_id)
        return TaskRecord.model_validate(data) if data else None

    def update(
        self,
        task_id: str,
        *,
        status: Optional[TaskStatus] = None,
        note: str = "",
        **fields: Any,
    ) -> TaskRecord:
        """Update fields on a task and append an audit-trail history entry."""
        record = self.get(task_id)
        if record is None:
            raise KeyError(f"Task '{task_id}' not found.")

        if status is not None:
            record.status = status
        for key, value in fields.items():
            setattr(record, key, value)

        record.updated_at = self._now()
        record.history.append(
            {
                "at": record.updated_at,
                "status": record.status.value,
                "note": note or f"transition -> {record.status.value}",
            }
        )

        data = record.model_dump(mode="json")
        if self._client is not None:
            self._doc_ref(task_id).set(data)
        else:
            self._memory[task_id] = data
        logger.info("Task %s updated -> %s.", task_id, record.status)
        return record


# Single shared repository instance for the process.
repository = FirestoreRepository()


# ---------------------------------------------------------------------------
# Agent tools — deterministic functions the ADK agent may call
# ---------------------------------------------------------------------------
def analyze_error_log(error_log: str, source_code: str) -> Dict[str, Any]:
    """Analyze an error log against source code to localize a likely bug.

    Args:
        error_log: Raw traceback or CI failure output from the repository.
        source_code: The relevant source file contents (may be empty).

    Returns:
        A dictionary describing the suspected file, line hints, and error class.
    """
    suspected_file = ""
    suspected_line = 0

    # Very small, dependency-free heuristic traceback parser.
    for raw in error_log.splitlines():
        line = raw.strip()
        if line.startswith("File ") and '"' in line:
            try:
                suspected_file = line.split('"')[1]
                if "line" in line:
                    suspected_line = int(
                        line.split("line", 1)[1].strip().split(",")[0].strip()
                    )
            except (IndexError, ValueError):
                continue

    error_type = "UnknownError"
    for candidate in ("Error", "Exception"):
        for token in error_log.replace(":", " ").split():
            if token.endswith(candidate):
                error_type = token
                break

    return {
        "suspected_file": suspected_file,
        "suspected_line": suspected_line,
        "error_type": error_type,
        "has_source": bool(source_code.strip()),
    }


def build_unified_diff(file_path: str, original: str, patched: str) -> str:
    """Produce a unified diff string for a single-file change.

    Args:
        file_path: Repo-relative path of the file being changed.
        original: Original snippet/content.
        patched: Replacement snippet/content.

    Returns:
        A unified diff as a string (empty if there is no change).
    """
    import difflib

    if original == patched:
        return ""

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        patched.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# ADK Agent definition
# ---------------------------------------------------------------------------
AGENT_INSTRUCTION = """\
You are the "Flow Agent", an autonomous software-maintenance agent.

Given a repository issue (title, body, and an optional error log / stack trace)
and any provided source code, you must:

1. Diagnose the ROOT CAUSE of the bug precisely.
2. Identify the single most relevant file to change.
3. Propose a minimal, correct patch (original snippet -> patched snippet).
4. Estimate your confidence between 0.0 and 1.0.

Use the `analyze_error_log` tool to localize the failure and the
`build_unified_diff` tool to render the final diff.

Respond with ONLY a valid JSON object matching this schema, no prose, no
markdown fences:
{
  "summary": string,
  "root_cause": string,
  "file_path": string,
  "original_snippet": string,
  "patched_snippet": string,
  "unified_diff": string,
  "confidence": number
}
"""


def build_agent() -> Any:
    """Construct the Google ADK agent.

    Returns ``None`` if the ADK is not installed, so callers can degrade to the
    heuristic fallback path without crashing.
    """
    try:
        from google.adk.agents import Agent

        agent = Agent(
            name=AGENT_APP_NAME,
            model=GEMINI_MODEL,
            description="Diagnoses repository bugs and proposes structured patches.",
            instruction=AGENT_INSTRUCTION,
            tools=[analyze_error_log, build_unified_diff],
        )
        logger.info("ADK agent built with model %s.", GEMINI_MODEL)
        return agent
    except Exception as exc:  # pragma: no cover - depends on environment
        logger.warning("ADK agent unavailable (%s). Using heuristic fallback.", exc)
        return None


# Lazily-built singleton so import never fails in constrained environments.
_agent: Any = None


def get_agent() -> Any:
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


# ---------------------------------------------------------------------------
# Orchestration: run the agent and coerce output into a PatchProposal
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> Dict[str, Any]:
    """Best-effort extraction of a JSON object from model text output."""
    text = text.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences if the model added them anyway
        text = text.split("```", 2)[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output.")
    return json.loads(text[start : end + 1])


def _run_adk_agent(prompt: str) -> str:
    """Run the ADK agent synchronously and return its final text response."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = get_agent()
    runner = InMemoryRunner(agent=agent, app_name=AGENT_APP_NAME)

    user_id = "system"
    session_id = uuid.uuid4().hex
    # InMemoryRunner exposes an async session service; create synchronously.
    import asyncio

    async def _go() -> str:
        await runner.session_service.create_session(
            app_name=AGENT_APP_NAME, user_id=user_id, session_id=session_id
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        final_text = ""
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(
                    part.text or "" for part in event.content.parts
                )
        return final_text

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():  # pragma: no cover - nested loop safety
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _go()).result()
    return asyncio.run(_go())


def _heuristic_patch(
    repo: str, issue_title: str, issue_body: str, error_log: str, source_code: str
) -> PatchProposal:
    """Deterministic fallback used when the LLM path is unavailable."""
    analysis = analyze_error_log(error_log, source_code)
    file_path = analysis["suspected_file"] or "src/app.py"
    original = source_code or "# TODO: original code unavailable"
    patched = original + "\n# FIX applied by Flow Agent (fallback)\n"
    return PatchProposal(
        summary=f"Proposed fix for: {issue_title}",
        root_cause=(
            f"Detected {analysis['error_type']} originating in {file_path}"
            f" (line {analysis['suspected_line']})."
        ),
        file_path=file_path,
        original_snippet=original,
        patched_snippet=patched,
        unified_diff=build_unified_diff(file_path, original, patched),
        confidence=0.35,
    )


def generate_patch(
    repo: str,
    issue_title: str,
    issue_body: str,
    error_log: str = "",
    source_code: str = "",
) -> PatchProposal:
    """Analyze a reported issue and produce a structured :class:`PatchProposal`.

    Attempts the Gemini/ADK path first and gracefully falls back to a
    deterministic heuristic if the agent or API key is unavailable. This keeps
    the endpoint responsive and testable in every environment.
    """
    prompt = (
        f"Repository: {repo}\n"
        f"Issue title: {issue_title}\n"
        f"Issue body:\n{issue_body}\n\n"
        f"Error log / stack trace:\n{error_log or '(none provided)'}\n\n"
        f"Relevant source code:\n{source_code or '(none provided)'}\n"
    )

    if get_agent() is not None and (
        os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    ):
        try:
            raw = _run_adk_agent(prompt)
            payload = _extract_json(raw)
            # Ensure a diff exists even if the model omitted it.
            if not payload.get("unified_diff"):
                payload["unified_diff"] = build_unified_diff(
                    payload.get("file_path", "src/app.py"),
                    payload.get("original_snippet", ""),
                    payload.get("patched_snippet", ""),
                )
            return PatchProposal.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - resilience is the point
            logger.warning("Agent path failed (%s); using heuristic fallback.", exc)

    return _heuristic_patch(repo, issue_title, issue_body, error_log, source_code)


# ---------------------------------------------------------------------------
# High-level workflow steps (used by the FastAPI layer)
# ---------------------------------------------------------------------------
def fetch_source_from_github(repo: str, error_log: str) -> Tuple[str, str]:
    """Pull the suspected file's real contents from the live repository.

    Localizes the failing file from the traceback, resolves it against the
    repo tree (tracebacks often carry absolute or partial paths), and returns
    its contents so the agent reasons about actual code rather than a snippet
    pasted into the webhook.

    Returns:
        ``(source_code, resolved_path)`` — both empty when nothing was found.
    """
    if not is_github_configured():
        return "", ""

    suspected = analyze_error_log(error_log, "").get("suspected_file", "")
    if not suspected:
        return "", ""

    try:
        client = GitHubClient()
        fetched = client.get_file(repo, suspected)
        resolved = suspected

        if fetched is None:
            discovered = client.search_file_by_name(repo, suspected)
            if not discovered:
                logger.info("Could not locate '%s' in '%s'.", suspected, repo)
                return "", ""
            resolved = discovered
            fetched = client.get_file(repo, resolved)

        if fetched is None:
            return "", ""

        logger.info("Fetched real source '%s' from '%s'.", resolved, repo)
        return fetched[0], resolved
    except GitHubError as exc:
        logger.warning("Could not fetch source from GitHub: %s", exc)
        return "", ""


def ingest_and_analyze(
    repo: str,
    issue_title: str,
    issue_body: str,
    error_log: str = "",
    source_code: str = "",
) -> TaskRecord:
    """Webhook step: create the task, run analysis, park it for approval.

    The resulting task is persisted with status ``PENDING_APPROVAL`` and a
    single-use approval token that the human must present to proceed.
    """
    task_id = uuid.uuid4().hex
    approval_token = secrets.token_urlsafe(24)

    record = TaskRecord(
        task_id=task_id,
        status=TaskStatus.RECEIVED,
        repo=repo,
        issue_title=issue_title,
        issue_body=issue_body,
        error_log=error_log,
        approval_token=approval_token,
    )
    repository.create(record)
    repository.update(task_id, status=TaskStatus.ANALYZING, note="Running agent analysis.")

    # Prefer real repository contents over whatever the webhook supplied.
    resolved_path = ""
    from_github = False
    if not source_code.strip():
        source_code, resolved_path = fetch_source_from_github(repo, error_log)
        from_github = bool(source_code)
        if from_github:
            repository.update(
                task_id,
                note=f"Fetched real source '{resolved_path}' from GitHub.",
            )

    try:
        patch = generate_patch(repo, issue_title, issue_body, error_log, source_code)
    except Exception as exc:  # noqa: BLE001
        repository.update(
            task_id, status=TaskStatus.FAILED, note=f"Analysis failed: {exc}"
        )
        raise

    return repository.update(
        task_id,
        status=TaskStatus.PENDING_APPROVAL,
        note="Patch generated; awaiting human approval.",
        patch=patch,
        resolved_path=resolved_path,
        source_fetched_from_github=from_github,
    )


def mock_create_github_pr(record: TaskRecord) -> str:
    """Synthesize a plausible PR URL when GitHub credentials are absent.

    Used as the offline/demo fallback so the workflow remains fully
    demonstrable without external credentials or side effects.
    """
    pr_number = int(record.task_id[:6], 16) % 9000 + 1000
    branch = f"{GITHUB_BRANCH_PREFIX}-{record.task_id[:8]}"
    logger.info(
        "Mock PR: repo=%s branch=%s file=%s",
        record.repo,
        branch,
        record.patch.file_path if record.patch else "?",
    )
    return f"https://github.com/{record.repo}/pull/{pr_number}"


def create_real_github_pr(record: TaskRecord) -> str:
    """Open a genuine pull request carrying the approved patch.

    Performs the full write sequence against the live repository:
    resolve default branch -> create fix branch -> commit patched file ->
    open pull request.

    Called **only** from :func:`approve_and_execute`, after the human gate.

    Raises:
        GitHubError: if any GitHub API step fails.
        ValueError: if the patch cannot be applied to the fetched file.
    """
    if record.patch is None:
        raise ValueError("Task has no patch to submit.")

    patch = record.patch
    client = GitHubClient()
    repo = record.repo

    # 1. Resolve the target file path on the real repository.
    path = record.resolved_path or patch.file_path
    fetched = client.get_file(repo, path)
    if fetched is None:
        # The traceback path may not match the repo layout; search by basename.
        discovered = client.search_file_by_name(repo, path)
        if discovered:
            path = discovered
            fetched = client.get_file(repo, path)
    if fetched is None:
        raise GitHubError(
            f"File '{path}' not found in '{repo}'; cannot apply the patch."
        )

    current_content, blob_sha = fetched

    # 2. Splice the approved patch into the real file content.
    new_content = apply_snippet_patch(
        current_content, patch.original_snippet, patch.patched_snippet
    )
    if new_content == current_content:
        raise ValueError("Patch produced no change against the current file.")

    # 3. Branch off the default branch.
    base_branch = client.get_default_branch(repo)
    base_sha = client.get_branch_sha(repo, base_branch)
    fix_branch = f"{GITHUB_BRANCH_PREFIX}-{record.task_id[:8]}"
    client.create_branch(repo, fix_branch, base_sha)

    # 4. Commit the patched file onto the fix branch.
    #    Re-read the blob SHA on the new branch to avoid a stale-SHA conflict.
    on_branch = client.get_file(repo, path, ref=fix_branch)
    branch_sha = on_branch[1] if on_branch else blob_sha
    client.commit_file(
        repo=repo,
        path=path,
        content=new_content,
        message=f"fix: {patch.summary}",
        branch=fix_branch,
        sha=branch_sha,
    )

    # 5. Open the pull request.
    return client.create_pull_request(
        repo=repo,
        title=f"fix: {patch.summary}",
        body=build_pr_body(
            task_id=record.task_id,
            summary=patch.summary,
            root_cause=patch.root_cause,
            confidence=patch.confidence,
            unified_diff=patch.unified_diff,
        ),
        head=fix_branch,
        base=base_branch,
    )


def create_github_pr(record: TaskRecord) -> Tuple[str, bool]:
    """Create a pull request, using the real GitHub API when configured.

    Returns:
        ``(pr_url, is_real)`` — ``is_real`` is ``False`` when the mock path was
        used because no ``GITHUB_TOKEN`` is present.
    """
    if not is_github_configured():
        logger.info("No GITHUB_TOKEN set; using mock pull-request path.")
        return mock_create_github_pr(record), False

    return create_real_github_pr(record), True


def approve_and_execute(task_id: str, approval_token: str) -> TaskRecord:
    """Approval step: validate the token, create the PR, record ``PR_CREATED``.

    Raises:
        KeyError: if the task does not exist.
        PermissionError: if the approval token is invalid.
        ValueError: if the task is not awaiting approval.
    """
    record = repository.get(task_id)
    if record is None:
        raise KeyError(f"Task '{task_id}' not found.")

    if not secrets.compare_digest(record.approval_token, approval_token or ""):
        raise PermissionError("Invalid approval token.")

    if record.status != TaskStatus.PENDING_APPROVAL:
        raise ValueError(
            f"Task '{task_id}' is '{record.status.value}', not PENDING_APPROVAL."
        )

    repository.update(task_id, status=TaskStatus.APPROVED, note="Human approved patch.")

    try:
        pr_url, is_real = create_github_pr(record)
    except Exception as exc:  # noqa: BLE001
        repository.update(
            task_id, status=TaskStatus.FAILED, note=f"PR creation failed: {exc}"
        )
        raise

    kind = "real" if is_real else "mock"
    return repository.update(
        task_id,
        status=TaskStatus.PR_CREATED,
        note=f"Pull request created ({kind}): {pr_url}",
        pr_url=pr_url,
    )


def reject_task(task_id: str, approval_token: str, reason: str = "") -> TaskRecord:
    """Reject a pending task (the negative HITL path)."""
    record = repository.get(task_id)
    if record is None:
        raise KeyError(f"Task '{task_id}' not found.")
    if not secrets.compare_digest(record.approval_token, approval_token or ""):
        raise PermissionError("Invalid approval token.")
    return repository.update(
        task_id,
        status=TaskStatus.REJECTED,
        note=f"Human rejected patch. Reason: {reason or 'not specified'}",
    )
