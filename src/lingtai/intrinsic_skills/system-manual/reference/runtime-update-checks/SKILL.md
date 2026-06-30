---
name: runtime-update-checks
description: >
  Nested system-manual reference for LingTai runtime/kernel self-checks and
  update nudges: handle `.notification/nudge.json` entries with
  `kind: kernel_version`, identify editable/dev/source installs, compare the
  running, installed, and latest kernel versions, respect the once-per-day
  packaged-runtime check, ask the human before downloading/updating, and refresh
  only when safe.
version: 0.1.0
tags: [lingtai, runtime, kernel, nudge, updates, refresh, editable, dev-mode]
last_changed_at: "2026-06-23T23:50:45-07:00"
---

# Runtime Update Checks

Nested system-manual reference for mechanical runtime/kernel checks and update
nudges. Read this when:

- a `nudge` notification contains an entry with `kind: kernel_version`;
- you suspect the running LingTai kernel is not the code currently installed on
  disk;
- a refresh did not seem to pick up a kernel change;
- you need to tell whether this agent is in editable/source/dev mode; or
- about once per day, a packaged runtime reports that a newer LingTai kernel is
  available.

This reference is about **checking and reporting**. It does not authorize
installing, downloading, publishing, or refreshing across important work without
human confirmation.

## Nudge payload contract

Kernel-owned periodic checks publish mechanical nudges through
`.notification/nudge.json`. The shared channel stores a list under
`data.nudges`; each entry has a unique `kind`. Kernel version/update entries use
`kind: kernel_version`.

Common fields:

| Field | Meaning |
| --- | --- |
| `running` | Version frozen in the currently running process (`lingtai.__version__`). |
| `installed` | Version visible from the installed `lingtai` distribution on disk. |
| `latest` | Latest package version found by the daily packaged-runtime check, or `null` for local refresh nudges. |
| `source` | `installed-distribution` for local refresh nudges, `pypi-json` for package-update nudges. |
| `cadence` | `at-most-once-per-utc-day` for both local refresh and package-update nudges (re-emitted at most once per UTC day per version pair). |
| `suggested_action` | The safe next-step hint; still verify context before acting. |

After handling, clear the channel with:

```text
notification(action="dismiss_channel", channel="nudge")
```

## How to respond

### Local refresh nudge (`source: installed-distribution`)

If `running != installed`, the package on disk differs from the process already
in memory. This usually means code was updated while the agent kept running.

1. Check whether the agent is in the middle of sensitive work, active daemons, or
   a task where refresh would lose useful in-flight state.
2. If safe, call `system(action="refresh", reason="Load installed LingTai kernel update")`.
3. After refresh, verify the import path/version and report any blocker to the
   human or peer who requested the update.

### Package-update nudge (`source: pypi-json`)

If `latest > installed`, a newer LingTai kernel package exists for packaged
runtimes. This check is throttled to at most once per UTC day per agent and is
skipped for editable/source/dev installs.

1. Tell the human the current `running`, `installed`, and `latest` versions.
2. Explain that this is an update availability nudge, not an automatic upgrade.
3. Ask whether they want you to update through their normal LingTai runtime/TUI
   upgrade path.
4. Do **not** download, install, edit config, or refresh until the human gives an
   explicit imperative confirmation for that side effect.

For user-facing instructions, follow the project standing rule: do not present a
bare `pip install --upgrade lingtai` as the normal user upgrade path unless the
human explicitly asks for development/diagnostic PyPI validation.

## Editable/source/dev installs

Package-update nudges are skipped when the kernel can identify the runtime as an
editable install, a source checkout, or a dev/local version. In those modes the
source of truth is usually the local checkout and git state, not the package
index. Diagnose with:

- the Python executable used by the agent runtime;
- `lingtai.__version__`, `lingtai.__file__`, and `lingtai_kernel.__file__`;
- installed distribution metadata, especially `direct_url.json` with
  `dir_info.editable: true`;
- the nearest git checkout, branch, HEAD, dirty state, and relation to remote;
- whether `system.refresh` has reloaded the code already present on disk.

A refresh reloads configuration and imports in a fresh process. It does not pull
new commits, install packages, or publish anything.

## Quick manual check

Use a short local Python probe when the nudge payload is ambiguous:

```bash
python - <<'PY'
import importlib.metadata as md
import sys
import lingtai, lingtai_kernel
print('python=', sys.executable)
print('lingtai_version=', getattr(lingtai, '__version__', 'unknown'))
print('lingtai_dist=', md.version('lingtai'))
print('lingtai_file=', getattr(lingtai, '__file__', 'unknown'))
print('lingtai_kernel_file=', getattr(lingtai_kernel, '__file__', 'unknown'))
try:
    dist = md.distribution('lingtai')
    print('direct_url=', dist.read_text('direct_url.json'))
except Exception as exc:
    print('direct_url_error=', type(exc).__name__, str(exc)[:120])
PY
```

Do not print credentials, tokens, or environment dumps while doing update checks.

## Relationship to notification-manual

`notification-manual` owns the generic channel protocol and dismissal mechanics.
This reference owns the `kernel_version` nudge interpretation and the safe human
handoff for runtime updates.
