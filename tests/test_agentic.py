"""Tests for the agentic loop's response parsing.

These exist because of a real failure: the first version carried patched file
contents inside a JSON string, which blew up with ``Invalid \\escape`` the
moment it met a source file containing regex patterns. Source code is full of
backslashes, so the file body is now transported between literal markers and
never JSON-encoded.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic import PATCH_END, PATCH_START, _parse_response


def _response(meta: str, body: str) -> str:
    return f"{meta}\n{PATCH_START}\n{body}\n{PATCH_END}"


class TestParseResponse:
    def test_extracts_metadata_and_body(self) -> None:
        out = _parse_response(
            _response('{"file_path": "a/b.py", "summary": "s", "confidence": 0.9}', "x = 1")
        )
        assert out["file_path"] == "a/b.py"
        assert out["confidence"] == 0.9
        assert out["patched_content"] == "x = 1\n"

    def test_preserves_regex_backslashes(self) -> None:
        """The exact case that broke the JSON-based parser."""
        body = 'import re\n_RE = re.compile(r"\\d+\\s*\\w+")\n'
        out = _parse_response(_response('{"file_path": "s.py"}', body))
        assert "\\d+" in out["patched_content"]
        assert "\\s*" in out["patched_content"]

    def test_preserves_windows_paths_and_escapes(self) -> None:
        body = 'P = "C:\\\\Users\\\\test"\nT = "a\\tb"\n'
        out = _parse_response(_response('{"file_path": "s.py"}', body))
        assert "C:\\\\Users" in out["patched_content"]
        assert "\\t" in out["patched_content"]

    def test_strips_markdown_fence_if_present(self) -> None:
        out = _parse_response(
            _response('{"file_path": "a.py"}', "```python\nx = 1\n```")
        )
        assert out["patched_content"] == "x = 1\n"

    def test_tolerates_missing_end_marker(self) -> None:
        text = '{"file_path": "a.py"}\n' + PATCH_START + "\nx = 1\n"
        assert _parse_response(text)["patched_content"] == "x = 1\n"

    def test_always_newline_terminates(self) -> None:
        out = _parse_response(_response('{"file_path": "a.py"}', "x = 1"))
        assert out["patched_content"].endswith("\n")

    def test_falls_back_to_legacy_json_payload(self) -> None:
        out = _parse_response('{"file_path": "a.py", "patched_content": "y = 2\\n"}')
        assert out["patched_content"] == "y = 2\n"

    def test_raises_when_no_content(self) -> None:
        with pytest.raises(ValueError):
            _parse_response("I could not find the bug, sorry.")

    def test_bad_metadata_json_does_not_lose_the_patch(self) -> None:
        """Malformed metadata must not discard an otherwise valid patch body."""
        out = _parse_response(_response("{not valid json", "x = 1"))
        assert out["patched_content"] == "x = 1\n"
