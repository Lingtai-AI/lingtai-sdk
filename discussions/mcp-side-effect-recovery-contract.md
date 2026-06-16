# MCP side-effect recovery contract

> **Status:** Design note for `Lingtai-AI/lingtai-kernel#258`; pre-implementation contract.
> **Related umbrella:** `Lingtai-AI/lingtai#188`.
> **Related fixes:** `Lingtai-AI/lingtai-kernel#98`, `#157`, `#231`.
> **Goal:** Make recovery for externally mutating MCP operations explicit enough that kernel runtime changes and addon authors do not accidentally duplicate sends/deletes/edits or leave stale producer state after continuation failure.

## 1. Why this contract exists

LingTai tool execution has two different success boundaries:

1. the tool handler may have already performed an external side effect; and
2. the following LLM continuation may still fail before the agent can reason over the result.

The kernel already protects the highest-risk core history bug: when a post-tool continuation send fails after real tool results exist, `_restore_tool_results_after_continuation_failure(...)` commits those real results back into chat history instead of letting recovery synthesize generic placeholders (`src/lingtai_kernel/base_agent/turn.py`). `tc_wake` uses the same restoration guard for notification-spliced tool results, and regression tests cover single and batched result restoration.

That is necessary but not sufficient for MCP operations that mutate external systems. A chat/email `send`, `reply`, `delete`, `edit`, contact mutation, message move/flag, filesystem write, or future MCP write action can be externally committed even if the agent's next continuation fails. If recovery text or notification mirrors make a retry look safe, the agent or human may duplicate a non-idempotent operation.

This document names the minimum contract every runtime recovery surface and side-effecting MCP producer should satisfy.

## 2. Scope and non-goals

### In scope

- MCP/addon tool calls that mutate external or persistent producer state.
- Kernel recovery after post-tool continuation failure.
- Producer notification refresh/invalidation after known successful side effects.
- Optional idempotency metadata or local operation ledgers where a producer can support them.
- Human/agent-facing recovery wording that distinguishes known success from unknown completion.

### Out of scope

- Re-fixing the core real-tool-result preservation path already covered by `#98` and `#157`.
- Making every external API call idempotent. Some producers cannot provide idempotency keys or stable operation IDs.
- A universal UI redesign. The minimum producer-side obligation is truthful recovery state and no stale notification nudge toward unsafe retry.
- Closing `Lingtai-AI/lingtai#188`; this is a narrower runtime/addon contract extracted from that umbrella.

## 3. Terms

**Producer**
: The tool owner that talks to an external or persistent system, for example the Telegram, IMAP, Feishu, WeChat, WhatsApp, filesystem, or future MCP server implementation.

**Side-effecting operation**
: A tool call that can mutate external state, persistent local state, or a user-visible resource. Examples include `send`, `reply`, `reply_all`, `delete`, `edit`, `move`, `flag`, contact edits, file writes, and future MCP write actions.

**Authoritative state**
: The producer's source of truth after an operation: remote API result, durable local ledger, mailbox/message listing, file content, or another producer-specific verification surface.

**Notification mirror**
: A `.notification/<channel>.json` or related mirrored preview that may be stale relative to authoritative producer state.

**Continuation failure**
: The tool handler has returned or is returning a result, but the following LLM/provider continuation fails before the agent can complete the turn normally.

## 4. Completion-state taxonomy

Recovery surfaces MUST distinguish these states for side-effecting tools:

| State | Meaning | Retry policy |
|---|---|---|
| `not_started` | The runtime knows the handler did not run. | Retry can be considered after fixing the original error. |
| `started_unknown` | The handler may have started, but completion is unknown. | Do not retry blindly. Check authoritative producer state first. |
| `completed_known` | The handler completed and a real result is known. | Do not retry. Preserve and surface the known result; refresh/invalidate affected notification mirrors. |
| `completed_known_continuation_failed` | The side effect completed, but the following LLM continuation failed. | Treat as completed for side-effect safety. Resume reasoning from the real result; require explicit verification before any repeat action. |
| `failed_no_effect_known` | The producer can prove no external effect occurred. | Retry can be considered, but the proof should be in the tool result or recovery metadata. |

If the runtime cannot prove `not_started` or `failed_no_effect_known`, it must prefer `started_unknown` over optimistic retry guidance.

## 5. Minimum runtime contract

When a side-effecting tool has a real result and the following continuation fails, the kernel/runtime MUST:

1. **Preserve the real result.** The committed tool result remains available in chat history or another agent/human recovery surface that carries the result or a structured reference to it. Logs are useful audit evidence, but logs alone do not satisfy this recovery contract. The result must not be replaced by a generic synthesized success/failure notice.
2. **Surface the completion state.** Recovery text and structured metadata should say whether the side effect is known completed, unknown, or known not run.
3. **Discourage blind retry.** Non-idempotent operations must include wording equivalent to: "Do not retry until authoritative producer state has been checked." Existing generic tool-error guidance already says not to blindly retry and to read mutable external state first; side-effecting tools need that message attached to the known operation class, not only to generic exceptions.
4. **Notify the producer when success is known.** If a producer-specific notification mirror can now be stale, call or schedule the producer's refresh/invalidation hook.
5. **Record enough evidence for audit.** Logs or a local operation record should include the tool name, call id, side-effect classification, completion state, producer reference (if any), and whether notification refresh/invalidation was attempted.

## 6. Minimum producer/addon contract

A producer that exposes side-effecting MCP tools SHOULD declare or document, per tool action:

- whether the action is side-effecting;
- whether it is naturally idempotent;
- whether the caller may supply an idempotency key or operation id;
- what authoritative state should be checked before retry;
- which notification mirror(s) may become stale after success;
- whether the producer can refresh or invalidate those mirrors after a known successful side effect.

The declaration mechanism is intentionally TBD for implementation PRs. It may become MCP tool metadata, kernel-side registry metadata, addon documentation conventions, or fields attached to `ToolProposal`; this document only fixes the semantics those mechanisms must express.

For third-party MCP tools that provide no LingTai-specific metadata, the kernel should default conservatively for externally mutating actions: do not assume idempotency, surface `started_unknown` when completion cannot be proven, and require authoritative state verification before retry.

For operations that support durable IDs, the producer SHOULD include the stable remote/local ID in the tool result. For operations that do not, the producer SHOULD return enough provenance to verify state manually: target chat/mailbox/path, operation type, timestamp, message/file identifiers if available, and a clear warning if duplicate retry may be unsafe.

## 7. Optional operation ledger

A universal ledger is not required for all producers, but side-effecting producers SHOULD adopt one when possible. A minimal record is:

```json
{
  "operation_id": "producer-or-runtime-stable-id",
  "tool_call_id": "tc_...",
  "producer": "telegram",
  "tool": "send",
  "target_ref": "chat/message/mailbox/path reference",
  "idempotency_key": "optional caller-supplied or producer-supplied key",
  "completion_state": "completed_known",
  "result_ref": "remote message id / file path / mailbox uid / other proof",
  "notification_channels": ["telegram"],
  "created_at": "ISO-8601 timestamp"
}
```

The ledger's purpose is not to replay arbitrary side effects. It is to answer recovery questions: "Did this side effect already happen? What authoritative reference proves it? Which mirror should be refreshed before anyone retries?"

## 8. Notification refresh/invalidation hook

Known successful side effects SHOULD invalidate stale notification mirrors for the affected producer. The hook can be narrow and producer-specific:

- `send` / `reply`: refresh outgoing/inbox conversation state or clear a stale prompt that would invite resending.
- `delete` / `edit`: refresh the affected message preview.
- mail `move` / `flag` / `archive`: refresh affected folder unread and message previews.
- file writes: no external notification hook is required unless another mirrored producer state depends on the file.

A stale generic `system.dismiss` guard, such as the channel-version protection added for notification dismiss races, is adjacent but not enough. The side-effecting producer must know whether its own mirror is now stale and expose a way to refresh or invalidate it.

## 9. Test-design checklist for implementation PRs

A focused implementation PR can start with one mock side-effecting MCP tool and should prove these behaviors. For the first implementation, items 1–4 and 6 are merge-gating; item 5 is merge-gating when a producer hook exists, otherwise the test should assert a structured "not supported" reason; item 7 can be covered by the same mock producer or a follow-up if the first PR only introduces the success/unknown paths:

1. The mock tool records a durable external mutation and returns a real result.
2. The test forces the post-tool LLM continuation to fail.
3. Recovery preserves the real result and marks the operation `completed_known_continuation_failed` rather than emitting a generic placeholder.
4. Recovery wording says not to retry until authoritative state is checked.
5. The producer refresh/invalidation hook is invoked for the affected notification channel, or a structured "not supported" reason is recorded.
6. A separate `started_unknown` case does not claim success and still discourages blind retry.
7. A `failed_no_effect_known` case can be retried only when the producer proves no side effect occurred.

## 10. Current gap summary

As of this design note:

- The core known-result restoration path exists and is tested.
- Generic tool error metadata already discourages blind retry and tells agents to read mutable external state before retry.
- `ToolCallGuard` can warn or deny before dispatch, but it is not a side-effect taxonomy and does not record completion state.
- Some chat/email duplicate-send heuristics exist for narrow `send`/`reply` paths, but the contract does not cover `delete`, `edit`, contact edits, mailbox moves/flags, filesystem writes, optional curated tools such as `cloud_mail`, or future MCP write actions.
- No shared side-effect classification, operation-ledger shape, or producer refresh/invalidation hook contract is currently documented for addon authors.

This design note establishes the shared contract for `#258`; it should not by itself close the issue until runtime/addon tests or follow-up implementation prove the contract in code. That makes `#258` a valid follow-up even though the most direct continuation-history regressions are already fixed.
