"""examples/buggy_stats.py — A deliberately broken module for demoing the agent.

This file contains a real, reproducible bug. It exists so the Repo Health
Taskmaster can be demonstrated end-to-end against a live repository: the agent
reads THIS file from GitHub, diagnoses the fault, proposes a patch, waits for
human approval, and then opens a real pull request fixing it.

Reproduce the bug:

    >>> from examples.buggy_stats import calculate_average
    >>> calculate_average([])
    ZeroDivisionError: division by zero
"""

from __future__ import annotations

from typing import List


def calculate_average(scores: List[float]) -> float:
    """Return the mean of ``scores``.

    BUG: divides by ``len(scores)`` without guarding against an empty list,
    raising ZeroDivisionError instead of handling the empty case.
    """
    if not scores:
      return 0.0
    return sum(scores) / len(scores)


def highest_score(scores: List[float]) -> float:
    """Return the largest value in ``scores``."""
    return max(scores)
