# LingTai kernel v0.13.1

Patch release for the Python runtime/kernel package after the tool-result metadata and summarize-guidance work.

## Highlights

- Refines tool-result runtime guidance metadata and removes the legacy metadata block path.
- Adds packaged summarize guidance/manual routing and mirrors active runtime guidance to `system/guidance.json` on boot/refresh.
- Clarifies Codex cache-affinity docs and Telegram contact wording.
- Strengthens resident procedures guidance so session-journal children use the canonical `<YYYY-MM-DD>-molt-<molt-count>-<slug>` naming form.

## Compatibility notes

- PyPI package version: `0.13.1`.
- No migration-version change.
- `system/guidance.json` is a runtime mirror of packaged guidance; agents may need refresh to see the new resident prompt wording.

## Validation

- `git diff --check` passed.
- `python -m pytest -q` passed: 2523 passed, 4 skipped.
- `python -m build` passed after removing test-generated `__pycache__` directories.
- `python -m twine check dist/*` passed.

## Artifact SHA-256

```text
e476637365e581b2e5644bf13562f2ecf1a3df8e83bfd4b383e8afed3076b502  lingtai-0.13.1-cp312-cp312-macosx_11_0_arm64.whl
8714db1167dae54fc9b05bb2909104168ff92a033b2955d33a51e8bafc04213d  lingtai-0.13.1.tar.gz
```
