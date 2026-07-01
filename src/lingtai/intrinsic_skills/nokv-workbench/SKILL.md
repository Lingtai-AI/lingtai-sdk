---
name: nokv-workbench
description: >
  Thin routing manual for NoKV-controlled workbenches. Use when an agent is
  asked to persist task inputs, scripts, outputs, logs, provenance, or run
  manifests through the `nokv_workbench_*` MCP tools instead of ordinary local
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
{"name":"nokv-workbench","summary":"NoKV-controlled workbench artifact namespace.","transport":"stdio","command":"/path/to/nokv","args":["mcp","--profile","workbench","--workbench-root","/workbenches"],"source":"local-nokv"}
```

Activate it from `init.json`:

```json
{
  "mcp": {
    "nokv-workbench": {
      "type": "stdio",
      "command": "/path/to/nokv",
      "args": ["mcp", "--profile", "workbench", "--workbench-root", "/workbenches"]
    }
  }
}
```

The MCP tools are intentionally prefixed with `nokv_` so they do not replace
LingTai's local `read`, `write`, `edit`, `grep`, or `glob` tools.

## Layout

Each workbench id maps to `/workbenches/<id>/` with these sections:

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

with `nokv_workbench_create`.

2. Write inputs, scripts, outputs, and evidence with
   `nokv_workbench_put_file`. Pass `replace=true` only when intentionally
   replacing a prior artifact.

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

`nokv_workbench_commit` publishes `metadata/run_manifest.json`. In v0 this file
is the completion marker. A workbench without it is not complete.

5. Snapshot the committed workbench with `nokv_workbench_snapshot` and cite the
returned `snapshot_id` and `read_version` in final reports or handoff notes.

## Read and search

Use `nokv_workbench_find` to query across workbenches. By default it returns
compact committed-state and manifest summaries rather than full manifest
bodies. Pass `include_manifest=true` only when the full
`metadata/run_manifest.json` envelope is needed.

Use `nokv_workbench_list`, `nokv_workbench_stat`, `nokv_workbench_read`, and
`nokv_workbench_grep` for content inside one workbench. Query tools return
flat `section`, `relative_path`, and `path` fields so follow-up calls can reuse
`section` and `relative_path` directly. NoKV grep is a case-insensitive
literal substring search, not regex. Use LingTai's local `grep` for local
workdir text and NoKV grep for workbench artifacts.

## Commit checklist

Before calling `nokv_workbench_commit`, verify:

- `input/` has the task event and dataset references.
- `scripts/` has code or notebooks needed to reproduce the result.
- `outputs/` has the requested deliverables.
- `metadata/provenance.json` exists when provenance is required.
- `logs/` contains evidence segments rather than one append-only file.
- The manifest lists relative paths inside the workbench sections.
