"""Tool execution timing helpers."""
import time


class ToolTimer:
    """Context manager for timing tool execution."""
    def __init__(self):
        self._start = 0.0
        self.elapsed_ms = 0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.elapsed_ms = int((time.monotonic() - self._start) * 1000)
        return False
