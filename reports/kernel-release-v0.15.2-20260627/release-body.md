## Highlights
- Codex summarize/cache behavior now waits for the provider-side reconstruction trigger before resetting the websocket epoch, preserving incremental continuation when summarize only changed local visible history (#534).
- Summarize/molt guidance now distinguishes local compaction from delayed provider reconstruction, treats task-boundary molt as the stronger completed-work boundary, and avoids summarize-first when a molt is already planned (#535).
- `_meta.agent_meta.token_efficiency` now exposes current-session token economy (API calls, input/cached tokens, cache rate, average input tokens, and context size/window) so agents and UI surfaces can reason about live token efficiency (#537).
- `_meta` payloads are leaner: notification guidance is now a resident `meta_guidance.notification_handling` hook, per-channel duplicate guidance is removed, tool-result char leaders are capped to top 5, and the post-reconstruction molt reminder is aligned to `0.6 * context_window` (#538).
- `read` now defaults to a 100k-character page budget while preserving the 200k runtime hard cap, matching the new assumption that short-lived long-result inspection is acceptable before summarizing or molting.

## Validation
- Release branch is based on `5b29ac963412856a89b75aeb49e58ac4a0ef64d6` (`Compact meta block guidance payloads (#538)`) on top of `v0.15.1`, with the release PR adding the `0.15.2` version bump and the 100k `read` default cap patch.
- `git diff --check` passed.
- `PYTHONPATH=src python -m pytest -q` passed: `3040 passed, 4 skipped in 291.20s`.
- `python -m build` succeeded; `python -m twine check dist/*` passed; archive inspection found no `__pycache__` or `.pyc` files.
- SHA256: wheel `2854c7aa7c7630d4f91e86e73395cee3497cf769b3e5e713e66db3a0a3f36aa4`; sdist `5bd4976b10b1e4157ea4bb257e5e63932015bea54ef21e382351f4952e8693ae`.

## Contributors
Thanks to: `huangzesen`.

## Compare
https://github.com/Lingtai-AI/lingtai-kernel/compare/v0.15.1...v0.15.2
