---
name: system-preset-workflow
description: >
  Nested system-manual reference — saved preset authorization and switching
  workflow for LingTai agents. Read this when adding a new saved preset, making
  it visible to an agent, swapping
  an agent to that preset, verifying `system(action="presets")`, reverting, or
  deciding whether to use a daemon/avatar instead of changing another agent's
  active preset.
version: 0.1.0
tags: [system, presets, refresh, init-json, runtime, authorization]
---

# Saved preset authorization workflow

Nested system-manual reference. Open this when the top-level `system-manual`
routes you to the saved-preset authorization workflow. Preset files and runtime
authorization are separate.

A saved preset file can exist on disk (for example
`~/.lingtai-tui/presets/saved/glm-5.2.json`) without being usable by a given
agent. `system(action='presets')` only lists presets authorized for **this
agent** in `init.json` under `manifest.preset.allowed[]`.

This is deliberate: dropping a JSON file into the saved-preset shelf does not
grant every agent permission to run it.

## When to use this workflow

Use this reference when a human asks you to:

- add a new model/provider preset,
- make a saved preset visible to an agent,
- switch yourself or another agent to a preset,
- test a preset briefly and then revert,
- explain why `system(action='presets')` does not show a saved preset file, or
- choose between changing an agent's active preset and using a daemon/avatar with
  the desired preset.

## 1. Create or edit the saved preset file

Create the saved preset JSON file under the user's saved-preset shelf, commonly:

```text
~/.lingtai-tui/presets/saved/<name>.json
```

A typical saved preset contains:

- `name`
- `description.summary`
- `manifest.llm` (`provider`, `model`, `base_url`, `api_key_env`, etc.)
- `manifest.capabilities` (tools and capability config enabled by the preset)

Example shape:

```json
{
  "name": "glm-5.2",
  "description": {
    "summary": "Zhipu GLM 5.2 Coding Plan — OpenAI-compatible"
  },
  "manifest": {
    "capabilities": {
      "bash": { "yolo": true },
      "daemon": {},
      "email": {},
      "file": {},
      "skills": {
        "paths": ["../.library_shared", "~/.lingtai-tui/utilities"]
      }
    },
    "llm": {
      "api_compat": "openai",
      "api_key": null,
      "api_key_env": "ZHIPU_API_KEY",
      "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
      "model": "GLM-5.2",
      "provider": "zhipu"
    }
  }
}
```

Prefer `api_key_env` over embedding secrets in the preset file.

## 2. Authorize the preset for the target agent

Edit the target agent's `init.json` and add the saved preset path to:

```text
manifest.preset.allowed[]
```

Example:

```json
{
  "manifest": {
    "preset": {
      "allowed": [
        "~/.lingtai-tui/presets/saved/codex.json",
        "~/.lingtai-tui/presets/saved/glm-5.2.json"
      ]
    }
  }
}
```

Do **not** remove existing allowed presets just because you add a new one.

## 3. Switch or refresh

After the preset is allowed, choose one of these paths:

### Switch in the refresh call

```python
system(action='refresh', preset='~/.lingtai-tui/presets/saved/glm-5.2.json')
```

### Or set active in `init.json`, then refresh

Set:

```text
manifest.preset.active = "~/.lingtai-tui/presets/saved/glm-5.2.json"
```

Then call:

```python
system(action='refresh')
```

## 4. Verify

After refresh, call:

```python
system(action='presets')
```

The preset should appear with provider/model, capabilities, and connectivity
status.

If `system(action='refresh', preset='...')` says the preset is not in the allowed
list, do not keep retrying. Add the path to `manifest.preset.allowed[]`, then
refresh or retry the swap.

## 5. Revert

To return to the default preset:

```python
system(action='refresh', revert_preset=true)
```

Or set `manifest.preset.active` back to the previous/default preset and refresh.

## Safety notes

- Do not overwrite another agent's active preset unless the human explicitly
  asked for that target agent to switch.
- If the task only needs a temporary model, prefer a daemon/avatar with that
  preset rather than changing an agent's active preset.
- Keep secrets out of preset files when possible; use `api_key_env` and the
  agent/TUI environment file.
- If current context exceeds the target preset's `context_limit`, refresh may be
  refused with a "molt first" instruction. Molt, then retry.
