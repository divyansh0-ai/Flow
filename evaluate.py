"""evaluate.py — SWE-bench-style evaluation against real, unplanted bugs.

Fixing a bug you wrote yourself proves very little. This harness measures the
Flow Agent against bugs that real maintainers actually shipped and later fixed.

For a given upstream fix commit it:

  1. checks the repository out **at** the fix commit (so the maintainer's own
     regression test is present),
  2. reverts *only the source file(s)* to their pre-fix state, restoring the
     original bug while keeping the test,
  3. commits that state so it is the workspace's clean baseline,
  4. confirms the test suite is genuinely red,
  5. runs the agent loop and checks whether the suite goes green,
  6. shows the agent's patch next to the maintainer's real one.

Neither the bug nor the test is authored by us.

Usage:
    python evaluate.py                 # run the default case
    python evaluate.py --list          # show available cases
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from typing import List

from agentic import run_agentic_fix
from workspace import RepoWorkspace


@dataclass
class Case:
    """One real upstream bug fix to evaluate against."""

    name: str
    repo: str
    fix_commit: str
    source_files: List[str]
    issue_title: str
    issue_body: str
    error_log: str


CASES: List[Case] = [
    Case(
        name="boltons-singularize-ss",
        repo="mahmoud/boltons",
        fix_commit="1e61524a798ea73632029a013a7686036d5c126f",
        source_files=["boltons/strutils.py"],
        issue_title="singularize() corrupts words ending in a double 's'",
        issue_body=(
            "strutils.singularize() strips the trailing 's' from words that are "
            "already singular and end in 'ss', so singularize('glass') returns "
            "'glas', singularize('boss') returns 'bos', and singularize('class') "
            "returns 'clas'. The function's own docstring implies "
            "singularize('Glasses') == 'Glass', but feeding that result back in "
            "degrades it further. Real '-sses' plurals such as 'glasses' must "
            "still singularize correctly to 'glass'."
        ),
        error_log=(
            "FAILED tests/test_strutils.py::test_singularize_double_s\n"
            "assert singularize('glass') == 'glass'\n"
            "AssertionError: assert 'glas' == 'glass'"
        ),
    ),
]


def _git(workspace: RepoWorkspace, *args: str) -> subprocess.CompletedProcess:
    """Run a git command inside the workspace."""
    return subprocess.run(
        ["git", *args],
        cwd=str(workspace.root),
        capture_output=True,
        text=True,
        timeout=120,
    )


def prepare_case(case: Case) -> RepoWorkspace:
    """Check out the fix commit, then restore the original bug in the source."""
    workspace = RepoWorkspace(repo=case.repo)

    # A shallow clone cannot reach an arbitrary commit, so clone fully enough.
    import shutil
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="flowagent_eval_"))
    clone = subprocess.run(
        ["git", "clone", "-q", f"https://github.com/{case.repo}.git", str(tmp)],
        capture_output=True, text=True, timeout=600,
    )
    if clone.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Clone failed: {clone.stderr[:300]}")
    workspace.root = tmp

    checkout = _git(workspace, "checkout", "-q", case.fix_commit)
    if checkout.returncode != 0:
        workspace.cleanup()
        raise RuntimeError(f"Could not check out {case.fix_commit}: {checkout.stderr[:300]}")

    # Revert only the source, keeping the maintainer's regression test.
    parent = f"{case.fix_commit}~1"
    for path in case.source_files:
        revert = _git(workspace, "checkout", parent, "--", path)
        if revert.returncode != 0:
            workspace.cleanup()
            raise RuntimeError(f"Could not revert {path}: {revert.stderr[:300]}")

    # Commit the bug state so it becomes the workspace's clean baseline.
    _git(workspace, "-c", "user.email=eval@flow.local", "-c", "user.name=flow-eval",
         "commit", "-qam", "Restore pre-fix state for evaluation")

    return workspace


def run_case(case: Case, max_iterations: int = 3) -> bool:
    """Evaluate one case. Returns True when the agent's patch turns the suite green."""
    print(f"\n{'=' * 70}\nCASE: {case.name}   ({case.repo})\n{'=' * 70}")
    print(f"Upstream fix commit : {case.fix_commit[:12]}")
    print(f"Reverted source     : {', '.join(case.source_files)}")

    workspace = prepare_case(case)
    try:
        # The maintainer's real fix, for comparison afterwards.
        reference = _git(
            workspace, "show", case.fix_commit, "--", *case.source_files
        ).stdout

        print("\n[1] Confirming the bug is present (suite should be RED)...")
        before = workspace.run_tests("tests/")
        print(f"    baseline: passed={before.passed} | {before.summary}")
        if before.passed:
            print("    !! Suite is green; the bug was not restored. Aborting case.")
            return False

        print("\n[2] Running the agent loop...")
        result = run_agentic_fix(
            repo=case.repo,
            issue_title=case.issue_title,
            issue_body=case.issue_body,
            error_log=case.error_log,
            max_iterations=max_iterations,
            workspace=workspace,
        )

        print(f"\n[3] Result: verified={result.verified} | {result.test_summary}")
        for it in result.iterations:
            print(
                f"    attempt {it.attempt}: tests_passed={it.tests_passed} "
                f"| {it.test_summary} | {it.note[:70]}"
            )

        print("\n--- AGENT'S PATCH ---")
        print(result.unified_diff[:1500] or "(none)")
        print("\n--- MAINTAINER'S ACTUAL FIX ---")
        print("\n".join(reference.splitlines()[-25:]))

        verdict = "PASS" if result.verified else "FAIL"
        print(f"\n>>> {case.name}: {verdict}")
        return result.verified
    finally:
        workspace.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE-bench-style evaluation.")
    parser.add_argument("--list", action="store_true", help="List available cases.")
    parser.add_argument("--case", default="", help="Run a single case by name.")
    parser.add_argument("--max-iterations", type=int, default=3)
    args = parser.parse_args()

    if args.list:
        for case in CASES:
            print(f"{case.name:32} {case.repo}")
        return 0

    cases = [c for c in CASES if not args.case or c.name == args.case]
    if not cases:
        print(f"No case named '{args.case}'.")
        return 1

    results = [run_case(c, args.max_iterations) for c in cases]
    passed = sum(results)
    print(f"\n{'=' * 70}\nTOTAL: {passed}/{len(results)} cases solved\n{'=' * 70}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
