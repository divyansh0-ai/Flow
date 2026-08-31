"""Tests for examples/buggy_stats.py.

These express the contract the module is supposed to satisfy. The empty-input
cases fail against the unfixed source, which is what gives the Flow Agent a
real signal to iterate against.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.buggy_stats import calculate_average, highest_score


class TestCalculateAverage:
    def test_averages_a_normal_list(self) -> None:
        assert calculate_average([10, 20, 30]) == 20.0

    def test_single_element(self) -> None:
        assert calculate_average([7]) == 7.0

    def test_handles_negative_numbers(self) -> None:
        assert calculate_average([-10, 10]) == 0.0

    def test_empty_list_does_not_raise(self) -> None:
        """An empty list must be handled, not crash with ZeroDivisionError."""
        result = calculate_average([])
        assert result == 0.0


class TestHighestScore:
    def test_returns_maximum(self) -> None:
        assert highest_score([3, 9, 4]) == 9

    def test_single_element(self) -> None:
        assert highest_score([42]) == 42
