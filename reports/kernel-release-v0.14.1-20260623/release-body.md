# LingTai kernel v0.14.1

Patch release v0.14.1 is a Codex/runtime stability release on top of v0.14.0. It keeps the kernel package version moving after the Responses/WebSocket cache work landed on main and before the release blogs/docs follow-up.

## Highlights

- **Codex Responses WebSocket stability**: persistent WebSocket reuse now supports incremental `previous_response_id` continuation, tool-output freezing, baseline restore, and explicit fresh-epoch resets.
- **Codex cache hygiene**: LingTai can reset stale Codex remote state on schedule or after local summarize, while the runtime comment now warns that repeated summarize calls force `ws_full` and should be batched for Codex.
- **Honest Codex identity**: default ChatGPT Codex requests identify as LingTai (`originator=lingtai`, `User-Agent=LingTai/<version>`); the official CLI-shaped identity remains only an explicit local comparison switch.
- **Daemon/runtime notification durability**: daemon terminal-state notifications and tool-result/runtime metadata paths were tightened after the v0.14.0 release line.
- **ANATOMY freshness**: Codex identity comments and kernel ANATOMY citations were refreshed, with a full local ANATOMY citation pass at 599 citations / 0 issues.

## Compatibility notes

- No user-facing install command changes are required for existing LingTai TUI-managed projects; this is the kernel/runtime package version used by project venv dependency resolution.
- Codex users may see `ws_full` after a summarize-triggered fresh epoch. That is expected: the next request is rebuilt from local history instead of continuing the previous remote chain.
- For Codex specifically, avoid several one-by-one summarize calls in quick succession; group ordinary long-result summaries when possible.

## Validation

- `python -m pytest -q` → `2715 passed, 4 skipped in 304.41s`
- `python -m py_compile src/lingtai/llm/openai/adapter.py`
- `python -m pytest -q tests/test_codex_ws_session.py::test_codex_adapter_comment_explains_epoch_reset_and_summarize_delay tests/test_codex_prompt_cache_key.py` → `42 passed`
- custom all-ANATOMY citation checker → `checked=599`, `issues=0`
- `python -m build`
- `python -m twine check dist/*` → passed for wheel and sdist
- artifact inspection: `__pycache__` / `.pyc` entries = 0 for both wheel and sdist
- `git diff --check`

## Artifact SHA-256

- `952be6499f75df3f83624276c3b9adb0ade8a8862f3f1f57575e0d9a148f7321`  `dist/lingtai-0.14.1-cp312-cp312-macosx_11_0_arm64.whl`
- `9574b9a81f0deb673e71fa090ba925aa3f05f84c2dbf9ccafa040e1381e756a3`  `dist/lingtai-0.14.1.tar.gz`
