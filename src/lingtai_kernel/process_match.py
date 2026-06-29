"""Process-command matcher for LingTai agent runs."""
from __future__ import annotations

import os


def match_agent_run(cmdline: str, working_dir: str) -> str | None:
    """Return the launch form if ``cmdline`` is an agent run for ``working_dir``.

    The matcher is intentionally conservative for the console-script and legacy
    forms: ``lingtai-agent`` / ``lingtai`` must be the command itself or the
    basename of a path. The module form is separate because real launches look
    like ``<python> -m lingtai run <dir>``.

    Residual limitation: ``ps command=`` is a flat string, not the original argv
    vector. A non-LingTai process can still match if its argument text is shaped
    exactly like an absolute LingTai program path followed by ``run <dir>``.
    """
    target = os.path.normpath(working_dir)
    for token, label, program_anchored in (
        (" -m lingtai run ", "module", False),
        ("lingtai-agent run ", "console", True),
        ("lingtai run ", "legacy", True),
    ):
        idx = cmdline.find(token)
        while idx != -1:
            if (not program_anchored) or idx == 0 or cmdline[idx - 1] == "/":
                tail = cmdline[idx + len(token):].strip()
                if tail and os.path.normpath(tail) == target:
                    return label
            idx = cmdline.find(token, idx + 1)
    return None
