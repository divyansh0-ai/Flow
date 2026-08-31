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
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from workspace import RepoWorkspace, TestResult, WorkspaceError

logger = logging.getLogger("flow.agentic")

MAX_ITERATIONS: int = int(os.getenv("FLOW_MAX_ITERATIONS", "3"))
AGENTIC_APP_NAME: str = "flow_agent_loop"

# Gemini's free tier rate-limits aggressively; transient 429/503 responses are
# retried rather than being allowed to burn an iteration.
MODEL_RETRIES: int = int(os.getenv("FLOW_MODEL_RETRIES", "3"))
RETRY_BASE_DELAY: float = float(os.getenv("FLOW_RETRY_BASE_DELAY", "10"))
MAX_RETRY_SLEEP: float = float(os.getenv("FLOW_MAX_RETRY_SLEEP", "65"))

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
  * Emit the COMPLETE corrected file, not a fragment or a diff. It must be
    valid, runnable code for the whole file.
  * Preserve existing style, imports, docstrings and unrelated code exactly.
  * Make the minimal change that actually fixes the bug.
  * Never weaken or delete a test to make it pass.

OUTPUT FORMAT — follow exactly.

First a single-line JSON object of metadata (no file contents in it):
{"file_path": "repo/relative/path.py", "summary": "one line", "root_cause": "why", "confidence": 0.9}

Then the corrected file, raw, between these exact markers:
<<<PATCHED_FILE>>>
...the complete corrected file, verbatim...
<<<END_PATCHED_FILE>>>

Write the code between the markers exactly as it should appear on disk: no
escaping, no JSON encoding, no markdown fences, no commentary. Everything
between the markers is written to the file byte for byte.
"""

PATCH_START = "<<<PATCHED_FILE>>>"
PATCH_END = "<<<END_PATCHED_FILE>>>"


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


def _strip_code_fence(body: str) -> str:
    """Remove a wrapping markdown fence if the model added one anyway."""
    stripped = body.strip()
    if not stripped.startswith("```"):
        return body
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _parse_response(text: str) -> Dict[str, Any]:
    """Parse the metadata JSON and the raw file body from a model response.

    File contents are carried between literal markers rather than inside a JSON
    string, because source files routinely contain backslash escapes (regex
    patterns, Windows paths) that make JSON round-tripping fragile.

    Raises:
        ValueError: when neither the marker format nor a JSON fallback yields
            a usable patch.
    """
    text = text or ""

    body = ""
    if PATCH_START in text:
        after = text.split(PATCH_START, 1)[1]
        body = after.split(PATCH_END, 1)[0] if PATCH_END in after else after
        body = _strip_code_fence(body).strip("\r\n")
        header = text.split(PATCH_START, 1)[0]
    else:
        header = text

    # Metadata: first JSON object in the header region.
    meta: Dict[str, Any] = {}
    start, end = header.find("{"), header.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            meta = json.loads(header[start : end + 1])
        except json.JSONDecodeError:
            meta = {}

    if not body:
        # Fallback: an older-style JSON payload carrying the content inline.
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            try:
                payload = json.loads(text[s : e + 1])
                body = str(payload.get("patched_content", ""))
                meta = {**payload, **meta}
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"No {PATCH_START} block and JSON fallback failed: {exc}"
                ) from exc

    if not body:
        raise ValueError("Model response contained no patched file content.")

    meta["patched_content"] = body if body.endswith("\n") else body + "\n"
    return meta


def _retry_delay(exc: Exception, attempt: int) -> Optional[float]:
    """Return how long to wait before retrying, or ``None`` if not retryable.

    Gemini returns 429 (quota) and 503 (overloaded) as transient conditions and
    often names a concrete ``retryDelay``. Honour that when present, otherwise
    back off exponentially.
    """
    text = str(exc)
    if "429" not in text and "503" not in text and "RESOURCE_EXHAUSTED" not in text \
            and "UNAVAILABLE" not in text:
        return None

    # A daily quota will not recover within a request; do not spin on it.
    if "PerDay" in text:
        return None

    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", text)
    if match:
        return min(float(match.group(1)) + 1.0, MAX_RETRY_SLEEP)
    return min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), MAX_RETRY_SLEEP)


def _run_agent_once(agent: Any, prompt: str) -> str:
    """Execute one turn of the ADK agent, retrying transient model errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MODEL_RETRIES + 1):
        try:
            return _run_agent_turn(agent, prompt)
        except Exception as exc:  # noqa: BLE001 - classified by _retry_delay
            last_exc = exc
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt == MODEL_RETRIES:
                raise
            logger.warning(
                "Transient model error (attempt %d/%d); retrying in %.0fs.",
                attempt, MODEL_RETRIES, delay,
            )
            time.sleep(delay)
    raise last_exc if last_exc else RuntimeError("Model call failed.")


def _run_agent_turn(agent: Any, prompt: str) -> str:
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
    workspace: Optional[RepoWorkspace] = None,
) -> AgenticResult:
    """Explore, patch, test, and revise until the suite passes or attempts run out.

    Args:
        repo: ``owner/name`` of the repository to repair.
        issue_title: Short description of the failure.
        issue_body: Longer description, if any.
        error_log: Traceback or CI output.
        token: GitHub token, required for private repositories.
        max_iterations: Maximum propose-verify cycles.
        workspace: An already-prepared workspace to operate in. When supplied,
            it is used as-is and left for the caller to clean up. This exists
            so evaluation harnesses can reconstruct a specific repository state
            (e.g. a commit with a known bug) before the agent runs.

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

    caller_owns_workspace = workspace is not None
    if workspace is None:
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
                payload = _parse_response(raw)
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
        if not caller_owns_workspace:
            workspace.cleanup()
