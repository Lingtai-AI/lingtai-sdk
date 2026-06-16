# Human status event contract for long-running agent work

Status: design contract / implementation checklist
Related issue: Lingtai-AI/lingtai#146

## Why this exists

Human-facing chat addons already make LingTai feel responsive in several local
ways: Telegram can show typing indicators and placeholder messages; chat
notification headers tell the agent to acknowledge long work; agents can also
manually call the same channel tool before starting a slow operation.

Those pieces are useful, but they are not the runtime contract requested by
Lingtai-AI/lingtai#146.  The missing primitive is a first-class, auditable,
runtime-mediated event that lets an agent explicitly say:

> I am about to do long-running work; deliver this short status update to the
> selected human-facing channel.

That event is **not** the final reply. It is also **not** an internal peer mail
message. It is a human-only progress/status emission with shared runtime safety
and routing semantics.

## Current state and gap

Current LICC v1 is an inbound callback contract from out-of-process MCP servers
into an agent:

- producers call `lingtai.core.mcp.licc.push_inbox_event(...)`;
- events are written under `.mcp_inbox/<mcp>/<event>.json`;
- `lingtai.core.mcp.inbox.validate_event()` accepts the inbound
  `from`/`subject`/`body`/`metadata`/`wake`/`received_at` shape;
- the runtime converts those events into agent-visible notifications.

That direction is MCP → agent.  It does not define an agent → human event broker,
MCP subscription declarations, common rate limits, or status-specific safety
filtering.

Messaging addons also have addon-local affordances:

- Telegram exposes `chat_action`, `placeholder`, send/edit/reply, and automatic
  reactions;
- Telegram/Feishu/WeChat notification headers instruct agents to acknowledge
  promptly and to send progress before long work;
- individual agents can manually call a channel tool before running a slow tool.

Those affordances are still tool calls chosen by the model. They do not provide a
shared, transport-neutral event with a single runtime policy point. They also do
not solve dedupe, audit, delivery capability negotiation, or redaction in one
place.

## Contract goals

A future implementation of `agent.human_status` should satisfy these properties:

1. **Explicit opt-in.** No status update is emitted unless the agent or runtime
   path explicitly requests one for the current work.
2. **Human-only.** The event targets external human-facing messaging surfaces; it
   must not be delivered as `.lingtai/` peer email and must not wake other
   agents.
3. **Not a final reply.** The status update may be shown in the same chat, thread,
   or placeholder lifecycle as the final answer, but its semantic mode is
   progress/status.
4. **Route is explicit or provenance-scoped.** The runtime must not silently guess
   "same as last human message" globally. The safest default route is the active
   human-channel notification provenance for the current turn; absent that, the
   target must be explicitly supplied.
5. **Runtime owns common safety.** Length caps, secret/path redaction, duplicate
   suppression, rate limits, and audit logging are shared by the broker instead
   of being reimplemented differently by every MCP addon.
6. **MCPs declare delivery support.** Messaging MCPs advertise whether they can
   handle human status events and which delivery modes they support.

## Event model

The canonical event name is:

```text
agent.human_status
```

Suggested broker-internal shape:

```jsonc
{
  "type": "agent.human_status",
  "created_at": "2026-05-21T10:40:00Z",
  "agent": "agent-name",
  "target": {
    "kind": "current_notification_source"
  },
  "status": {
    "phase": "working",
    "text": "I’m running the screenshot checks now; this may take a minute.",
    "mode": "status",
    "ttl_seconds": 120
  },
  "correlation": {
    "turn_id": "...",
    "tool_call_id": "optional",
    "notification_channel": "mcp.telegram",
    "notification_event_id": "optional",
    "parent_message_id": "optional placement hint, not reply semantics"
  }
}
```

This shape is deliberately not the existing inbound LICC event shape. It may live
in a future LICC activity/event stream, or in a runtime broker with LICC-style MCP
subscription hooks. The important contract is the semantic boundary: agent →
runtime broker → subscribed human-channel MCP.

## Targeting rules

### `current_notification_source`

The recommended first target kind is:

```jsonc
{ "kind": "current_notification_source" }
```

It means: deliver only if the current turn was caused by a human-facing MCP
notification whose provenance is still active in the runtime context. For
example, a Telegram notification can carry enough provenance for the broker to
route a status event back through the same Telegram account/chat without exposing
raw route identifiers as a general model-default.

Required behavior:

- If there is exactly one active human-channel notification source, the broker may
  route to that source.
- If there is no active human-channel source, delivery is skipped unless an
  explicit safe target is supplied.
- If multiple human-channel sources are simultaneously active, delivery is
  skipped unless the agent disambiguates.
- Skipped delivery is logged as a broker decision, not surfaced as a tool failure
  that invites blind retry.

### Explicit targets

A later explicit-target form can be added, for example:

```jsonc
{
  "kind": "human_channel",
  "channel": "telegram",
  "account": "default",
  "conversation_ref": "jason"
}
```

Explicit targets should prefer aliases or opaque broker-managed route references.
Raw chat IDs, open IDs, email addresses, or platform-specific identifiers should
not be rendered into prompt text unless the addon already exposes them safely.

## Agent-facing API options

The final surface can be incremental. The contract allows either of these paths:

### Option A — intrinsic emitter first

Add a small intrinsic, for example `human_status.emit`, with arguments like:

```jsonc
{
  "target": { "kind": "current_notification_source" },
  "text": "I’m starting the local test suite now; this may take a minute.",
  "phase": "working",
  "ttl_seconds": 120
}
```

Pros:

- fits the current tool-call model;
- easy to test;
- keeps business tool arguments clean;
- can be documented as a voluntary pre-flight call before long work.

Cons:

- costs an extra tool call before the long operation;
- cannot attach atomically to another tool call unless the runtime later adds
  side-channel metadata.

### Option B — tool-call side metadata

Allow ordinary tool calls to include side metadata outside the tool business
arguments:

```jsonc
{
  "tool": "bash.run",
  "args": { "command": "python -m pytest ..." },
  "human_status": {
    "target": { "kind": "current_notification_source" },
    "text": "I’m running the focused tests now; I’ll report failures or the PR link next.",
    "phase": "working"
  }
}
```

Pros:

- status and the long operation are selected together;
- no extra model/tool turn;
- better for "entering a long tool call" semantics.

Cons:

- requires an envelope-level tool-call extension;
- must be carefully separated from tool arguments so MCPs and non-messaging tools
  do not grow ad-hoc messaging fields.

### Option C — broader agent activity stream

`agent.human_status` can be one human-safe event in a wider runtime stream:

- `agent.started_tool`
- `agent.human_status`
- `agent.finished_tool`
- `agent.needs_attention`

Only explicitly human-safe events should be eligible for addon delivery.

## Broker safety requirements

The broker must apply common policy before delivery:

- **Length cap:** default status text cap should be small, e.g. 280–500 chars.
- **No raw tool arguments by default:** command lines, URLs with tokens, full file
  paths, environment variable names, and internal IDs are not copied unless the
  agent explicitly writes safe prose.
- **Secret/path redaction:** apply the existing project redaction helpers where
  available; at minimum catch obvious tokens, API-key-like strings, and absolute
  local paths.
- **Rate limit:** per agent + route, e.g. no more than one status every N seconds,
  with a small burst allowance for phase changes.
- **Deduplicate:** suppress identical or near-identical "I am working" updates in
  a tight ReAct loop.
- **Audit log:** record requested/delivered/suppressed status events with reason
  codes, without logging secrets or the full unsanitized text.
- **Delivery failure semantics:** failure to deliver a status should not abort the
  underlying work. It should be logged and, when useful, surfaced to the agent as
  a nonblocking warning.

## MCP subscription contract

Messaging MCPs should be able to declare status delivery support. One possible
shape:

```jsonc
{
  "licc_subscriptions": [
    {
      "event": "agent.human_status",
      "targets": ["current_notification_source", "telegram"],
      "delivery_modes": ["message", "placeholder", "edit", "typing"],
      "max_text_chars": 500
    }
  ]
}
```

The exact registration surface can follow the MCP registry/catalog shape chosen
by the runtime. Required semantics:

- no subscription means no delivery;
- unsupported target/mode is skipped with an auditable broker decision;
- addons may choose a platform-native delivery style (message, placeholder edit,
  typing indicator, Feishu/Lark message, etc.) while preserving `mode=status`;
- addons should not make their own independent routing guesses that contradict
  the broker target.

## Transcript and context accounting

The agent should know when it already sent a status update, but the human status
must not pollute normal reply semantics.

A future implementation should record a compact synthetic result or runtime note
such as:

```jsonc
{
  "status_event": "agent.human_status",
  "delivery": "delivered",
  "channel": "mcp.telegram",
  "mode": "status",
  "text_preview": "I’m running the focused tests now..."
}
```

This gives future context enough information to avoid duplicate progress updates
without treating the status as the final answer.

## Test checklist for implementation PRs

A code implementation that claims to close Lingtai-AI/lingtai#146 should include
at least these tests:

1. **No default emission:** long tool calls do not send status unless requested.
2. **Current notification source routing:** a status targeted at
   `current_notification_source` routes only when an active human MCP notification
   provenance exists.
3. **Ambiguous/no source suppression:** no source or multiple sources suppresses
   delivery with a logged reason.
4. **Human-only:** status is not written to internal `.lingtai/` mail and does not
   wake peers.
5. **Not final reply:** final response still flows through the normal channel;
   status is separately marked as `mode=status`.
6. **Safety filter:** long text, obvious secrets, absolute local paths, and raw
   tool-argument leakage are capped/redacted.
7. **Rate limit/dedupe:** repeated identical status requests in one tool loop are
   collapsed or suppressed.
8. **MCP subscription:** an addon without `agent.human_status` support is not
   invoked; a subscribed addon receives the sanitized broker payload.
9. **Telegram or Feishu demonstration:** at least one real messaging addon handles
   the event using an existing send/placeholder/typing primitive.
10. **Audit evidence:** requested/delivered/suppressed events are queryable in
    runtime logs without exposing secrets.

## Non-goals

- Do not close this issue by adding only more prompt guidance; prompt guidance is
  useful but not a runtime contract.
- Do not add `human_status` business arguments to unrelated tools such as `bash`,
  `read`, `vision`, or arbitrary MCP calls.
- Do not send automatically to the last human channel without explicit target or
  current notification provenance.
- Do not use internal `.lingtai/` mail as the delivery path.
- Do not treat a status update as a final answer or as approval/authorization.

## Incremental path

A safe implementation sequence is:

1. Add the broker-side event type, target model, safety filter, audit records, and
   tests with no addon delivery.
2. Add a minimal intrinsic emitter (`human_status.emit`) targeting
   `current_notification_source`.
3. Add one addon subscription/handler, preferably Telegram because it already has
   message, placeholder, edit, and typing primitives.
4. Add rate-limit/dedupe and transcript notes.
5. Consider envelope-level tool-call metadata once the intrinsic path proves the
   semantics.

Until those implementation steps land, this document is a contract/spec only. It
should be referenced by implementation PRs and should not by itself close
Lingtai-AI/lingtai#146.
