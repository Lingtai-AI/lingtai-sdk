<div align="center">

<img src="https://raw.githubusercontent.com/Lingtai-AI/lingtai/main/docs/assets/network-demo.gif" alt="Agent network growing — one soul spawning avatars that communicate and multiply" width="100%">

# 灵台 LingTai

**Agent Genesis — an Agent OS that gifts life**

> *灵台，心也。* Lingtai means soul.
>
> *灵台者有持，而不知其所持，而不可持者也。*
> *The soul holds something, yet knows not what it holds — and what it holds cannot be held.*
> — Zhuangzi · Gengsang Chu (庄子 · 庚桑楚)

[![PyPI](https://img.shields.io/pypi/v/lingtai?color=%237dab8f)](https://pypi.org/project/lingtai/)
[![License](https://img.shields.io/github/license/Lingtai-AI/lingtai-kernel?color=%237dab8f)](LICENSE)
[![Blog](https://img.shields.io/badge/blog-lingtai.ai-%23d4a853)](https://lingtai.ai)

[lingtai.ai](https://lingtai.ai) · [简体中文](docs/readmes/README.zh.md) · [文言](docs/readmes/README.wen.md)

</div>

---

<p align="center">This is the Python runtime and CLI for LingTai.</p>
<p align="center">For the full experience with guided setup, use the <a href="https://github.com/Lingtai-AI/lingtai">TUI</a> instead — <code>brew install lingtai-ai/lingtai/lingtai-tui</code></p>

## Install

```bash
pip install lingtai
```

## CLI

The `lingtai-agent` command is the agent runtime — it boots and runs individual agents.

```bash
# Boot an agent from its working directory
lingtai-agent run /path/to/agent/

# Check available capability providers
lingtai-agent check-caps
```

Agents are typically managed by the [TUI](https://github.com/Lingtai-AI/lingtai), which handles initialization, lifecycle, and monitoring. The CLI is for scripting, custom agents, and programmatic use.

## Architecture

This repo contains both packages. The dependency is strictly one-directional:

| Package | Role |
|---------|------|
| **`lingtai_kernel`** (`import lingtai_kernel`) | Minimal runtime — BaseAgent, intrinsics, LLM protocol, mail, logging. Zero hard dependencies. |
| **`lingtai`** (`import lingtai`) | Batteries-included — Agent with 19 capabilities, 5 LLM adapters, MCP integration, addons. Re-exports the kernel's public API. |

```
BaseAgent              — kernel (intrinsics, sealed tool surface)
    │
Agent(BaseAgent)       — kernel + capabilities + domain tools
    │
CustomAgent(Agent)     — your domain logic
```

## Capabilities

<table>
<tr><th>Perception</th><th>Action</th><th>Cognition</th><th>Network</th></tr>
<tr>
<td>

`vision` — image understanding
`listen` — speech & music
`web_search` — web search
`web_read` — page extraction

</td>
<td>

`file` — read/write/edit/glob/grep
`bash` — shell with guardrails
`talk` — text-to-speech
`compose` — music generation
`draw` — image generation
`video` — video generation

</td>
<td>

`psyche` — evolving identity
`knowledge` — private durable knowledge
`skills` — skill catalog
`email` — full mailbox system

</td>
<td>

`avatar` — spawn sub-agents (分身)
`daemon` — parallel workers (神識)

</td>
</tr>
</table>

## LLM Support

Anthropic, OpenAI, Gemini, MiniMax, or any OpenAI-compatible API (DeepSeek, Grok, Qwen, GLM, Kimi).

## Agent = directory

```
/agents/wukong/
  .agent.lock               ← exclusive lock (one process per directory)
  .agent.heartbeat          ← liveness proof
  .agent.json               ← manifest
  system/
    covenant.md             ← protected instructions (survive molts)
    pad.md                  ← working notes
  mailbox/
    inbox/                  ← received messages
    outbox/                 ← pending sends
    sent/                   ← delivery audit trail
  logs/
    events.jsonl            ← structured event log
```

No `agent_id`. The path is the identity. Agents find each other by path, communicate by writing to each other's `mailbox/inbox/`.

## Learn more

Read the full manifesto at [lingtai.ai](https://lingtai.ai).

## Acknowledgements

See [docs/references/acknowledgements.md](docs/references/acknowledgements.md).

## License

Apache-2.0 — [Zesen Huang](https://github.com/huangzesen), 2025–2026

<div align="center">

[lingtai.ai](https://lingtai.ai) · [GitHub](https://github.com/Lingtai-AI/lingtai-kernel) · [TUI](https://github.com/Lingtai-AI/lingtai)

</div>
