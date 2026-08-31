"""agentic.py — The iterative fix loop that makes Flow Agent an actual agent.

A single LLM call that emits a patch is a *generator*, not an agent: it never
finds out whether it was right. This module closes that loop.

For each task the agent:

  1. **Gathers its own context** — it lists the repository and decides which
     files to read, using ADK tools rather than being handed a snippet.
  2. **Proposes a patch** as a full replacement file.
  3. **Verifies it** — the patch is written into a disposable clone and the
     repository's own test suite is executed.
  4. **Revises on failure** — the actual pytest output is fed back and the
     agent tries again, up to ``FLOW_MAX_ITERATIONS`` times.

The loop exits early the moment the suite goes green. Every attempt is recorded
so the Human-in-the-Loop reviewer can see not just the final patch but how the
agent got there — including what it tried and why it failed.

Nothing here writes to the real repository; that only happens after approval.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from workspace import RepoWorkspace, TestResult, WorkspaceError

logger = logging.getLogger("flow.agentic")

MAX_ITERATIONS: int = int(os.getenv("FLOW_MAX_ITERATIONS", "3"))
AGENTIC_APP_NAME: str = "flow_agent_loop"

# The tools below are plain module-level functions because the ADK inspects
# their signatures. They operate on whichever workspace the current loop has
# checked out.
_active_workspace: Optional[RepoWorkspace] = None


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------
class Iteration(BaseModel):
    """One propose-verify cycle of the loop."""

    attempt: int
    file_path: str = ""
    tests_passed: bool = False
    tests_skipped: bool = False
    test_summary: str = ""
    note: str = ""


class AgenticResult(BaseModel):
    """Outcome of the full loop."""

    success: bool = False
    verified: bool = Field(
        default=False,
        description="True only when the repository's own tests passed after the patch.",
    )
    file_path: str = ""
    original_snippet: str = ""
    patched_snippet: str = ""
    unified_diff: str = ""
    summary: str = ""
    root_cause: str = ""
    confidence: float = 0.0
    iterations: List[Iteration] = Field(default_factory=list)
    test_summary: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# ADK tools — the agent calls these to gather its own context
# ---------------------------------------------------------------------------
def list_repo_files() -> Dict[str, Any]:
    """List the source files available in the repository under analysis.

    Use this first to orient yourself before deciding which file to read.

    Returns:
        A dict with a ``files`` list of repo-relative paths.
    """
    if _active_workspace is None:
        return {"error": "No repository workspace is active.", "files": []}
    try:
        files = _active_workspace.list_files(
            extensions=[".py", ".toml", ".cfg", ".ini", ".txt"]
        )
        return {"files": files, "count": len(files)}
    except WorkspaceError as exc:
        return {"error": str(exc), "files": []}


def read_repo_file(path: str) -> Dict[str, Any]:
    """Read the full contents of one file from the repository under analysis.

    Args:
        path: Repo-relative path, e.g. ``src/stats.py``.

    Returns:
        A dict with the file ``content``, or an ``error`` if it is missing.
    """
    if _active_workspace is None:
        return {"error": "No repository workspace is active.", "content": ""}
    try:
        return {"path": path, "content": _active_workspace.read_file(path)}
    except WorkspaceError as exc:
        return {"error": str(exc), "content": ""}


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------
LOOP_INSTRUCTION = """\
You are the Flow Agent, an autonomous software-maintenance agent that repairs
real repositories.

You have tools to explore the repository:
  * `list_repo_files()`  — see what files exist
  * `read_repo_file(path)` — read any file's full contents

WORKFLOW
1. Call `list_repo_files()` to orient yourself.
2. Read the file the traceback implicates, plus any test file that covers it.
3. Diagnose the ROOT CAUSE.
4. Produce a corrected version of the ONE file that needs to change.

If you are given feedback from a previous attempt whose tests FAILED, read the
pytest output carefully, work out why your fix did not satisfy the tests, and
produce a different, better fix. Do not repeat a patch that already failed.

RULES
  * Return the COMPLETE corrected file in `patched_content`, not a fragment or
    a diff. It must be valid, runnable code for the whole file.
  * Preserve existing style, imports, docstrings and unrelated code exactly.
  * Make the minimal change that actually fixes the bug.
  * Never weaken or delete a test to make it pass.

Respond with ONLY a valid JSON object, no prose and no markdown fences:
{
  "file_path": "repo/relative/path.py",
  "root_cause": "why the bug happens",
  "summary": "one-line description of the fix",
  "patched_content": "<the complete corrected file>",
  "confidence": 0.0-1.0
}
"""


def _build_loop_agent() -> Any:
    """Construct the ADK agent that drives the loop, or ``None`` if unavailable."""
    try:
        from google.adk.agents import Agent

        return Agent(
            name=AGENTIC_APP_NAME,
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            description="Explores a repository, proposes a fix, and revises it against tests.",
            instruction=LOOP_INSTRUCTION,
            tools=[list_repo_files, read_repo_file],
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Could not build ADK loop agent: %s", exc)
        return None


def _extract_json(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of model output, tolerating stray fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model returned no JSON object.")
    return json.loads(text[start : end + 1])


def _run_agent_once(agent: Any, prompt: str) -> str:
    """Execute one turn of the ADK agent and return its final text."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    runner = InMemoryRunner(agent=agent, app_name=AGENTIC_APP_NAME)
    user_id, session_id = "flow", uuid.uuid4().hex

    async def _go() -> str:
        await runner.session_service.create_session(
            app_name=AGENTIC_APP_NAME, user_id=user_id, session_id=session_id
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        final = ""
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        return final

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running and running.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _go()).result()
    return asyncio.run(_go())


def _build_prompt(
    repo: str,
    issue_title: str,
    issue_body: str,
    error_log: str,
    attempt: int,
    last_failure: Optional[TestResult],
    last_path: str,
) -> str:
    """Compose the prompt for one attempt, including prior test feedback."""
    parts = [
        f"Repository: {repo}",
        f"Issue title: {issue_title}",
        f"Issue body:\n{issue_body or '(none)'}",
        f"Error log / stack trace:\n{error_log or '(none provided)'}",
    ]

    if attempt > 1 and last_failure is not None:
        parts.append(
            "--- PREVIOUS ATTEMPT FAILED ---\n"
            f"You patched `{last_path}` on attempt {attempt - 1}, but the test "
            f"suite did not pass.\n\n"
            f"pytest output:\n{last_failure.brief()}\n\n"
            "Diagnose why your patch did not satisfy the tests and produce a "
            "different fix. Do not resubmit the same patch."
        )

    parts.append(
        "Explore the repository with your tools, then return the JSON object "
        "described in your instructions."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def run_agentic_fix(
    repo: str,
    issue_title: str,
    issue_body: str = "",
    error_log: str = "",
    token: Optional[str] = None,
    max_iterations: int = MAX_ITERATIONS,
) -> AgenticResult:
    """Explore, patch, test, and revise until the suite passes or attempts run out.

    Args:
        repo: ``owner/name`` of the repository to repair.
        issue_title: Short description of the failure.
        issue_body: Longer description, if any.
        error_log: Traceback or CI output.
        token: GitHub token, required for private repositories.
        max_iterations: Maximum propose-verify cycles.

    Returns:
        An :class:`AgenticResult`. ``verified`` is ``True`` only when the
        repository's own tests passed with the patch applied.
    """
    global _active_workspace

    result = AgenticResult()
    agent = _build_loop_agent()
    if agent is None:
        result.error = "ADK agent unavailable."
        return result

    try:
        workspace = RepoWorkspace(repo=repo, token=token)
        workspace.clone()
    except WorkspaceError as exc:
        result.error = f"Could not prepare workspace: {exc}"
        return result

    _active_workspace = workspace
    last_failure: Optional[TestResult] = None
    last_path = ""

    try:
        # Baseline: does the suite already pass? If so a failing test is not
        # our signal, and we can only offer an unverified patch.
        baseline = workspace.run_tests()
        if baseline.passed:
            logger.info("Baseline suite already passes; patch cannot be test-verified.")

        for attempt in range(1, max_iterations + 1):
            logger.info("Agentic attempt %d/%d for %s", attempt, max_iterations, repo)
            workspace.reset()

            prompt = _build_prompt(
                repo, issue_title, issue_body, error_log,
                attempt, last_failure, last_path,
            )

            try:
                raw = _run_agent_once(agent, prompt)
                payload = _extract_json(raw)
            except Exception as exc:  # noqa: BLE001 - keep looping on bad output
                logger.warning("Attempt %d produced unusable output: %s", attempt, exc)
                result.iterations.append(
                    Iteration(attempt=attempt, note=f"Unusable model output: {exc}")
                )
                continue

            file_path = str(payload.get("file_path", "")).strip()
            patched = payload.get("patched_content", "")
            if not file_path or not patched:
                result.iterations.append(
                    Iteration(attempt=attempt, note="Model omitted file_path or patched_content.")
                )
                continue

            # Capture the original before overwriting, for the audit trail.
            try:
                original = workspace.read_file(file_path)
            except WorkspaceError:
                original = ""

            workspace.write_file(file_path, patched)
            last_path = file_path

            tests = workspace.run_tests()
            last_failure = tests

            result.iterations.append(
                Iteration(
                    attempt=attempt,
                    file_path=file_path,
                    tests_passed=tests.passed,
                    tests_skipped=tests.skipped,
                    test_summary=tests.summary or ("skipped" if tests.skipped else ""),
                    note=str(payload.get("summary", ""))[:300],
                )
            )

            # Record the current best candidate regardless of outcome.
            result.file_path = file_path
            result.original_snippet = original
            result.patched_snippet = patched
            result.unified_diff = workspace.diff()
            result.summary = str(payload.get("summary", ""))
            result.root_cause = str(payload.get("root_cause", ""))
            try:
                result.confidence = float(payload.get("confidence", 0.0))
            except (TypeError, ValueError):
                result.confidence = 0.0
            result.test_summary = tests.summary or ("skipped" if tests.skipped else "")

            if tests.passed:
                result.success = True
                result.verified = True
                logger.info("Tests passed on attempt %d.", attempt)
                break

            if tests.skipped:
                # No suite to verify against; accept the proposal but flag it.
                result.success = bool(result.unified_diff)
                result.verified = False
                logger.info("No tests to verify against; returning unverified patch.")
                break

            logger.info("Attempt %d failed tests: %s", attempt, tests.summary)

        if not result.success and result.unified_diff:
            # Exhausted attempts but we do have a candidate patch.
            result.success = True
            result.verified = False

        if not result.unified_diff and not result.error:
            result.error = "The agent did not produce a usable patch."

        return result
    finally:
        _active_workspace = None
        workspace.cleanup()
