---
name: nokv-workbench
description: >
  Thin routing manual for NoKV-controlled workbenches. Use when an agent is
  asked to persist task inputs, scripts, outputs, logs, provenance, or run
  manifests through the `workbench_*` MCP tools instead of ordinary local
  file writes. Covers MCP registration, directory layout, commit discipline,
  segmented logs, and snapshot references.
version: 0.1.0
tags: [nokv, mcp, workbench, artifacts, provenance, snapshots]
---

# NoKV Workbench

Use this skill when a task must write durable artifacts through NoKV rather
than the local LingTai workdir. The authoritative control surface is the NoKV
MCP server started in workbench profile. This skill is only the operating
manual.

## MCP registration

Register the MCP with a per-agent `mcp_registry.jsonl` line like:

```json
{"name":"nokv-workbench","summary":"NoKV-controlled workbench artifact namespace.","transport":"stdio","command":"/path/to/nokv","args":["--server-bind","127.0.0.1:7777","--object-backend","rustfs","--s3-bucket","nokv-lingtai-workbench","mcp","--profile","workbench","--workbench-root","/agents/{agent_id}/wb"],"source":"local-nokv"}
```

Activate it from `init.json`:

```json
{
  "mcp": {
    "nokv-workbench": {
      "type": "stdio",
      "command": "/path/to/nokv",
      "args": ["--server-bind", "127.0.0.1:7777", "--object-backend", "rustfs", "--s3-bucket", "nokv-lingtai-workbench", "mcp", "--profile", "workbench", "--workbench-root", "/agents/{agent_id}/wb"]
    }
  }
}
```

Keep the global NoKV flags before `mcp`, and set them to the same metadata
server and object-store bucket used by the running NoKV service. If your
deployment uses a non-default S3/RustFS endpoint or credentials, add the
matching `--s3-*` flags here as well.

### Per-agent root (tenant isolation)

Each agent gets its own workbench root so agents cannot see or clobber each
other's workbenches on a shared NoKV server. Use the `{agent_id}` placeholder
in `--workbench-root`; LingTai expands it at MCP launch to the agent's stable
address (its `.lingtai/<agent>` directory name):

```
--workbench-root /agents/{agent_id}/wb   ->   /agents/scout/wb   (for agent "scout")
```

`{agent_address}` is an alias for the same value, and `{agent_dir}` expands to
the agent's absolute working directory. The same registry line therefore works
verbatim for every agent — no per-agent editing. This is path-scoped isolation
enforced by the NoKV MCP (an agent's tools can only address paths under its own
root); it is not a server-side ACL, which matches LingTai's local trust model.
Record the owner in each run manifest (e.g. `"owner": "{agent_id}"`) so the
provenance is explicit; the committed `run_manifest.json` also embeds the full
`workbench_path`, which already contains the owning agent id.

The MCP tools are intentionally prefixed with `workbench_` so they do not replace
LingTai's local `read`, `write`, `edit`, `grep`, or `glob` tools.

## TUI runtime preflight

Before installing a workbench-enabled LingTai branch into the TUI runtime,
check the runtime package version:

```bash
~/.lingtai-tui/runtime/venv/bin/python - <<'PY'
import importlib.metadata as md
print(md.version("lingtai"))
PY
```

Do not install a source branch that is older than the runtime package already
used by TUI. Rebase or cherry-pick this workbench skill onto the matching or
newer upstream LingTai release, build/install that branch, then verify that the
runtime can see the skill:

```bash
~/.lingtai-tui/runtime/venv/bin/python - <<'PY'
from pathlib import Path
import lingtai.intrinsic_skills as skills
root = Path(skills.__file__).parent
print((root / "nokv-workbench" / "SKILL.md").exists())
PY
```

## Layout

Each workbench id maps to `<workbench-root>/<id>/` (with the per-agent root
above, `/agents/<agent_id>/wb/<id>/`) with these sections:

```text
input/
scripts/
outputs/
logs/
metadata/
```

Use the sections consistently:

| Section | Contents |
|---|---|
| `input` | task event payloads, dataset references, parameters |
| `scripts` | analysis code, notebooks, reproducibility scripts |
| `outputs` | plots, CSVs, derived datasets, reports |
| `logs` | agent-facing trace excerpts and tool-call evidence |
| `metadata` | provenance, run manifests, audit references |

Do not write LingTai runtime state here. `.agent.lock`, heartbeat files,
mailbox, `.notification/`, `.mcp_inbox/`, and `logs/events.jsonl` stay in the
local LingTai workdir.

## Workflow

1. Create the workbench:

```json
{"id":"spedas-task-001"}
```

with `workbench_create`.

2. Write inputs, scripts, outputs, and evidence with
   `workbench_put_file`. Pass `replace=true` only when intentionally
   replacing a prior artifact. When `section` is set, `path` is relative to
   that section: use `section="outputs", path="spectrum.csv"`, not
   `path="outputs/spectrum.csv"`.

3. For logs, write segmented files rather than appending:

```text
logs/agent_trace/000001.log
logs/tool_calls/000001.jsonl
logs/tool_calls/000002.jsonl
```

4. Commit only after required outputs are present:

```json
{
  "id": "spedas-task-001",
  "manifest": {
    "task": "spedas-task-001",
    "inputs": ["input/event.json", "input/dataset-ref.json"],
    "scripts": ["scripts/analysis.py"],
    "outputs": ["outputs/plot_001.png", "outputs/spectrum.csv"],
    "logs": ["logs/tool_calls/000001.jsonl"],
    "provenance": "metadata/provenance.json"
  }
}
```

`workbench_commit` publishes `metadata/run_manifest.json`. In v0 this file
is the completion marker. A workbench without it is not complete.

5. Snapshot the committed workbench with `workbench_snapshot` and cite the
returned `snapshot_id` and `read_version` in final reports or handoff notes.

## Concurrency

For the MVP, use a parent-created workbench and child-filled files:

- The parent agent creates the workbench, assigns disjoint section-relative
  paths, validates outputs, commits, and snapshots.
- Child or daemon agents only write the paths assigned by the parent. They do
  not create, commit, snapshot, or write `metadata/run_manifest.json`.
- Assign disjoint prefixes such as `outputs/agent-a/` and `logs/agent-a/`.
- Do not let two agents write the same file path. Same-path writes with
  `replace=false` intentionally fail with an exists conflict; treat that as a
  coordination bug, not a reason to bypass with `replace=true`.

## Read and search

Use `workbench_find` to query across workbenches. By default it returns
compact committed-state and manifest summaries rather than full manifest
bodies. Pass `include_manifest=true` only when the full
`metadata/run_manifest.json` envelope is needed.

Use `workbench_list`, `workbench_stat`, `workbench_read`, and
`workbench_grep` for content inside one workbench. Query tools return
flat `section`, `relative_path`, and `path` fields so follow-up calls can reuse
`section` and `relative_path` directly. NoKV grep is a case-insensitive
literal substring search, not regex. Use LingTai's local `grep` for local
workdir text and NoKV grep for workbench artifacts.

## Commit checklist

Before calling `workbench_commit`, verify:

- `input/` has the task event and dataset references.
- `scripts/` has code or notebooks needed to reproduce the result.
- `outputs/` has the requested deliverables.
- `metadata/provenance.json` exists when provenance is required.
- `logs/` contains evidence segments rather than one append-only file.
- The manifest lists relative paths inside the workbench sections.
