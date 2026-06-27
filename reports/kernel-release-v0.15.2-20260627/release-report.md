# LingTai kernel v0.15.2 release report

## Scope
- Previous tag: `v0.15.1`
- Base candidate head before release branch: `5b29ac963412856a89b75aeb49e58ac4a0ef64d6`
- Included PRs:
  - #534 Delay Codex summarize epoch resets
  - #535 Fix Codex summarize threshold guidance
  - #537 Expose current-session token efficiency metadata
  - #538 Compact meta block guidance payloads
  - Release branch patch: raise `read` default page budget to 100k while retaining the 200k hard cap

## Validation matrix
- [x] `git diff --check`
- [x] `PYTHONPATH=src python -m pytest -q` → `3040 passed, 4 skipped in 291.20s`
- [x] remove `src/**/__pycache__` before build
- [x] `python -m build` → built `lingtai-0.15.2.tar.gz` and `lingtai-0.15.2-cp312-cp312-macosx_11_0_arm64.whl`
- [x] `python -m twine check dist/*` → passed for both artifacts
- [x] archive inspection for `__pycache__` / `.pyc` → `bad=0` for both artifacts
- [x] SHA256 recorded from exact upload artifacts

### Validation note
A first full run without `PYTHONPATH=src` produced three subprocess import failures in the local Anaconda environment (`python -m lingtai` / `lingtai_kernel` not found). Re-running the full suite with `PYTHONPATH=src` passed cleanly, matching the intended source-tree test environment.

## Artifact hashes
```text
2854c7aa7c7630d4f91e86e73395cee3497cf769b3e5e713e66db3a0a3f36aa4  dist/lingtai-0.15.2-cp312-cp312-macosx_11_0_arm64.whl
5bd4976b10b1e4157ea4bb257e5e63932015bea54ef21e382351f4952e8693ae  dist/lingtai-0.15.2.tar.gz
```

## Publish checklist
- [ ] Version/report/cap PR merged
- [ ] GitHub release `v0.15.2` published
- [ ] PyPI `lingtai 0.15.2` uploaded and verified
- [ ] Release URLs verified
- [x] Local validation caveat recorded
