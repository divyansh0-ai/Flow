"""workspace.py — An isolated checkout the agent can read, edit, and test in.

This is what makes the Flow Agent genuinely agentic rather than a single-shot
prompt. Instead of guessing at a fix, the agent gets a real working copy of the
repository and can:

  * list and read files to gather its own context,
  * write candidate patches,
  * **run the test suite and observe the result**,
  * revise based on the actual failure output.

Everything happens in a temporary directory that is deleted afterwards; the
agent never touches the user's checkout, and no write reaches the real
repository until the Human-in-the-Loop gate approves it.

.. warning::
   ``run_tests`` executes the target repository's test suite, which is
   arbitrary code. Only point this at repositories you trust, and prefer
   running the service inside a container. Set ``FLOW_ENABLE_TESTS=0`` to
   disable test execution entirely (the agent then falls back to a single
   proposal with no verification loop).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("flow.workspace")

TESTS_ENABLED: bool = os.getenv("FLOW_ENABLE_TESTS", "1") not in ("0", "false", "False")
TEST_TIMEOUT_SECONDS: int = int(os.getenv("FLOW_TEST_TIMEOUT", "180"))
CLONE_TIMEOUT_SECONDS: int = int(os.getenv("FLOW_CLONE_TIMEOUT", "180"))

# Directories never worth showing the agent when it gathers context.
_IGNORED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".pytest_cache", ".mypy_cache", "dist", "build", ".tox", ".idea", ".vscode",
}


class WorkspaceError(RuntimeError):
    """Raised when the workspace cannot be prepared or operated on."""


@dataclass
class TestResult:
    """Outcome of running the repository's test suite."""

    passed: bool
    exit_code: int
    output: str
    skipped: bool = False
    summary: str = ""

    def brief(self, limit: int = 3000) -> str:
        """Return output trimmed for inclusion in a model prompt."""
        if self.skipped:
            return "Test execution was skipped."
        text = self.output
        if len(text) <= limit:
            return text
        # Keep the tail, where pytest puts the failure summary.
        return "...[truncated]...\n" + text[-limit:]


@dataclass
class RepoWorkspace:
    """A disposable clone of a repository the agent can experiment in."""

    repo: str
    token: Optional[str] = None
    ref: Optional[str] = None
    root: Optional[Path] = None
    _cleanup: bool = field(default=True, repr=False)

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "RepoWorkspace":
        self.clone()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()

    def clone(self) -> Path:
        """Shallow-clone the repository into a temporary directory."""
        tmp = Path(tempfile.mkdtemp(prefix="flowagent_"))
        url = f"https://github.com/{self.repo}.git"
        if self.token:
            # Credentials stay in-process; never logged.
            url = f"https://x-access-token:{self.token}@github.com/{self.repo}.git"

        cmd = ["git", "clone", "--depth", "1"]
        if self.ref:
            cmd += ["--branch", self.ref]
        cmd += [url, str(tmp)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise WorkspaceError(f"Cloning '{self.repo}' timed out.") from exc

        if result.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            # Redact the token if git echoed the URL back in the error.
            stderr = result.stderr
            if self.token:
                stderr = stderr.replace(self.token, "***")
            raise WorkspaceError(f"Failed to clone '{self.repo}': {stderr[:500]}")

        self.root = tmp
        logger.info("Cloned '%s' into workspace.", self.repo)
        return tmp

    def cleanup(self) -> None:
        """Delete the temporary checkout."""
        if self.root and self._cleanup:
            shutil.rmtree(self.root, ignore_errors=True)
            logger.info("Cleaned up workspace for '%s'.", self.repo)
            self.root = None

    # -- internal ------------------------------------------------------------
    def _require_root(self) -> Path:
        if self.root is None:
            raise WorkspaceError("Workspace is not prepared; call clone() first.")
        return self.root

    def _resolve(self, rel_path: str) -> Path:
        """Resolve a repo-relative path, refusing escapes outside the clone."""
        root = self._require_root()
        candidate = (root / rel_path).resolve()
        if not str(candidate).startswith(str(root.resolve())):
            raise WorkspaceError(f"Path '{rel_path}' escapes the workspace.")
        return candidate

    # -- file operations ------------------------------------------------------
    def list_files(self, extensions: Optional[List[str]] = None, limit: int = 400) -> List[str]:
        """List repo-relative file paths, skipping vendor and cache directories."""
        root = self._require_root()
        found: List[str] = []
        for path in root.rglob("*"):
            if len(found) >= limit:
                break
            if not path.is_file():
                continue
            if any(part in _IGNORED_DIRS for part in path.parts):
                continue
            if extensions and path.suffix not in extensions:
                continue
            found.append(str(path.relative_to(root)).replace("\\", "/"))
        return sorted(found)

    def read_file(self, rel_path: str) -> str:
        """Return the text contents of a file in the workspace."""
        path = self._resolve(rel_path)
        if not path.is_file():
            raise WorkspaceError(f"File '{rel_path}' does not exist in the workspace.")
        return path.read_text(encoding="utf-8", errors="replace")

    def write_file(self, rel_path: str, content: str) -> None:
        """Overwrite a file in the workspace, creating parents as needed."""
        path = self._resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        logger.info("Wrote candidate patch to '%s'.", rel_path)

    # -- verification ---------------------------------------------------------
    def _test_command(self, target: Optional[str] = None) -> Optional[List[str]]:
        """Choose a test command by inspecting the repository's layout.

        Returns ``None`` when no runner can be identified, so the caller can
        report an honest "unverified" result rather than a false failure.
        """
        root = self._require_root()

        # Python: pytest, if the project looks like Python at all.
        looks_python = (
            (root / "pyproject.toml").is_file()
            or (root / "setup.py").is_file()
            or (root / "setup.cfg").is_file()
            or (root / "tests").is_dir()
            or any(root.glob("*.py"))
        )
        if looks_python:
            cmd = ["python", "-m", "pytest", "-q", "--no-header", "-x"]
            if target:
                cmd.append(target)
            return cmd

        # JavaScript/TypeScript: only if a real test script is declared, since
        # npm's default "no test specified" stub exits non-zero and would look
        # like a genuine failure.
        package = root / "package.json"
        if package.is_file():
            try:
                import json as _json

                scripts = _json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
                script = scripts.get("test", "")
                if script and "no test specified" not in script:
                    return ["npm", "test", "--silent"]
            except (ValueError, OSError):
                pass

        return None

    def run_tests(self, target: Optional[str] = None) -> TestResult:
        """Run the repository's pytest suite and capture the outcome.

        Args:
            target: Optional path or node id to narrow the run.

        Returns:
            A :class:`TestResult`. When test execution is disabled or no tests
            exist, ``skipped`` is ``True`` and ``passed`` is ``False``.
        """
        root = self._require_root()

        if not TESTS_ENABLED:
            return TestResult(
                passed=False, exit_code=-1, output="", skipped=True,
                summary="Test execution disabled via FLOW_ENABLE_TESTS=0.",
            )

        cmd = self._test_command(target)
        if cmd is None:
            return TestResult(
                passed=False, exit_code=-1, output="", skipped=True,
                summary="no recognised test runner for this repository",
            )

        try:
            result = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                passed=False, exit_code=-1,
                output=f"Test run exceeded {TEST_TIMEOUT_SECONDS}s and was killed.",
                summary="timeout",
            )
        except FileNotFoundError as exc:
            return TestResult(
                passed=False, exit_code=-1, output=str(exc), skipped=True,
                summary="pytest unavailable",
            )

        output = (result.stdout or "") + (result.stderr or "")

        # pytest exit code 5 == "no tests collected", which is not a failure
        # of the patch, so treat it as a skip rather than a red result.
        if result.returncode == 5:
            return TestResult(
                passed=False, exit_code=5, output=output, skipped=True,
                summary="no tests collected",
            )

        summary = ""
        for line in reversed(output.splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                summary = line.strip()
                break

        return TestResult(
            passed=result.returncode == 0,
            exit_code=result.returncode,
            output=output,
            summary=summary,
        )

    # -- diffing --------------------------------------------------------------
    def diff(self) -> str:
        """Return a unified diff of all uncommitted changes in the workspace."""
        root = self._require_root()
        result = subprocess.run(
            ["git", "diff"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
        return result.stdout or ""

    def changed_files(self) -> List[str]:
        """Return repo-relative paths of files modified in the workspace."""
        root = self._require_root()
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]

    def reset(self) -> None:
        """Discard all working-tree changes, returning to a clean checkout."""
        root = self._require_root()
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
