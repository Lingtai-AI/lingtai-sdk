"""Tests for ToolTimer."""
import time

from lingtai_kernel.tool_timing import ToolTimer


def test_tool_timer_measures_elapsed_ms():
    with ToolTimer() as timer:
        time.sleep(0.01)
    assert timer.elapsed_ms >= 10
    assert timer.elapsed_ms < 500  # generous upper bound to avoid flakes
