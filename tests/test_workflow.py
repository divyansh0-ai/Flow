"""Tests for the Human-in-the-Loop workflow and its security properties.

These cover the guarantees the whole project rests on: that a patch cannot
reach a repository without a valid human approval, and that the state machine
refuses out-of-order transitions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent as flow
from agent import PatchProposal, TaskStatus
from github_client import apply_snippet_patch, build_pr_body


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test an isolated in-memory task store."""
    repo = flow.FirestoreRepository.__new__(flow.FirestoreRepository)
    repo._collection_name = "test_tasks"
    repo._client = None
    repo._memory = {}
    monkeypatch.setattr(flow, "repository", repo)


@pytest.fixture
def stub_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid network/LLM calls; return a deterministic patch."""
    def _fake(repo, title, body, error_log="", source_code=""):
        return PatchProposal(
            summary="stub fix",
            root_cause="stub cause",
            file_path="src/x.py",
            original_snippet="a = 1\n",
            patched_snippet="a = 2\n",
            unified_diff="--- a\n+++ b\n",
            confidence=0.9,
        )
    monkeypatch.setattr(flow, "generate_patch", _fake)
    monkeypatch.setattr(flow, "is_github_configured", lambda: False)


class TestStateMachine:
    def test_analysis_parks_at_pending_approval(self, stub_patch: None) -> None:
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        assert record.status is TaskStatus.PENDING_APPROVAL
        assert record.patch is not None

    def test_approval_advances_to_pr_created(self, stub_patch: None) -> None:
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        done = flow.approve_and_execute(record.task_id, record.approval_token)
        assert done.status is TaskStatus.PR_CREATED
        assert done.pr_url

    def test_cannot_approve_twice(self, stub_patch: None) -> None:
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        flow.approve_and_execute(record.task_id, record.approval_token)
        with pytest.raises(ValueError):
            flow.approve_and_execute(record.task_id, record.approval_token)

    def test_cannot_approve_a_rejected_task(self, stub_patch: None) -> None:
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        flow.reject_task(record.task_id, record.approval_token, "no thanks")
        with pytest.raises(ValueError):
            flow.approve_and_execute(record.task_id, record.approval_token)

    def test_history_records_every_transition(self, stub_patch: None) -> None:
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        done = flow.approve_and_execute(record.task_id, record.approval_token)
        statuses = [entry["status"] for entry in done.history]
        assert "PENDING_APPROVAL" in statuses
        assert "APPROVED" in statuses
        assert "PR_CREATED" in statuses


class TestApprovalSecurity:
    def test_wrong_token_is_rejected(self, stub_patch: None) -> None:
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        with pytest.raises(PermissionError):
            flow.approve_and_execute(record.task_id, "not-the-token")

    def test_empty_token_is_rejected(self, stub_patch: None) -> None:
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        with pytest.raises(PermissionError):
            flow.approve_and_execute(record.task_id, "")

    def test_unknown_task_raises(self) -> None:
        with pytest.raises(KeyError):
            flow.approve_and_execute("does-not-exist", "token")

    def test_tokens_are_unique_per_task(self, stub_patch: None) -> None:
        a = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        b = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        assert a.approval_token != b.approval_token

    def test_no_pr_is_created_without_approval(self, stub_patch: None) -> None:
        """The gate's core promise: a pending task has taken no outward action."""
        record = flow.ingest_and_analyze("o/r", "bug", "body", "trace")
        assert record.pr_url == ""
        assert record.status is TaskStatus.PENDING_APPROVAL


class TestPatchSplicing:
    def test_replaces_the_matching_region(self) -> None:
        full = "def f():\n    return 1\n\ndef g():\n    pass\n"
        out = apply_snippet_patch(full, "    return 1\n", "    return 2\n")
        assert "return 2" in out
        assert "def g():" in out

    def test_whole_file_replacement(self) -> None:
        full = "a = 1\n"
        assert apply_snippet_patch(full, "a = 1\n", "a = 2\n") == "a = 2\n"

    def test_refuses_when_snippet_absent(self) -> None:
        with pytest.raises(ValueError):
            apply_snippet_patch("a = 1\n", "totally different\n", "x\n")

    def test_rejects_empty_original(self) -> None:
        with pytest.raises(ValueError):
            apply_snippet_patch("a = 1\n", "", "x\n")


class TestErrorLogAnalysis:
    def test_extracts_file_from_traceback(self) -> None:
        log = 'File "src/stats.py", line 7, in avg\nZeroDivisionError: division by zero'
        out = flow.analyze_error_log(log, "")
        assert out["suspected_file"] == "src/stats.py"
        assert out["suspected_line"] == 7

    def test_tolerates_unparseable_log(self) -> None:
        out = flow.analyze_error_log("something broke", "")
        assert out["suspected_file"] == ""


class TestPrBody:
    def test_includes_diff_and_task_id(self) -> None:
        body = build_pr_body("task123", "sum", "cause", 0.9, "--- a\n+++ b\n")
        assert "task123" in body
        assert "+++ b" in body
        assert "approved by a human" in body
