"""Default configuration for the claude-code provider.

``api_key_env`` is intentionally absent: the ``claude`` CLI owns authentication
(stored OAuth credentials / ``CLAUDE_CODE_OAUTH_TOKEN``), so LingTai needs no
key. ``base_url`` is None — there is no HTTP endpoint; the adapter shells out to
the local binary.
"""

DEFAULTS = {
    "base_url": None,
    # Model alias passed to ``claude --model``. "sonnet"/"opus"/"haiku" resolve
    # to the latest of that tier; full ids (e.g. "claude-sonnet-4-5") also work.
    "model": "sonnet",
}
