DEFAULTS = {
    # Clean-room completion provider. The Claude Agent SDK authenticates
    # through the local Claude CLI / login (no per-request API key), so there
    # is no ``api_key_env`` here — the adapter tolerates a missing key.
    "api_compat": "claude_agent_sdk",
    "base_url": None,
    "model": "sonnet",
}
