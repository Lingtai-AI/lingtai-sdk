# 审计：内核中一切「系统所诱之文入」注入点

> **来源**：mimo-pro 审计回复，2026-05-04（mailbox `20260504T220552-d3e3`）
> **审计范围**：`src/lingtai_kernel/` 与 `src/lingtai/`
> **方法**：grep + read，无运行代码
> **每条记**：触发条件 → 注入处（文件:行） → 注入形 → 注入路径 → 是否有 dismiss/lifecycle

---

## 编者按 — 此卷之用

本卷为「envelope 系统」设计之素材底卷。当前内核中存在七条不同的「系统注入路径」（见末尾分类表），各自语义、结构、可控性皆异。设计目标：评估能否将其统一为单一信封通道（以 `tc_inbox` 为骨架），并定义每路径之 routing policy。

待续讨论：
- 哪些路径**必须**保留独立通道（如 system prompt 重建、molt warnings）？
- 哪些路径**应**迁入 `tc_inbox` 以获得 dismiss/coalesce/replace 能力？
- 信封结构应如何抽象（source、policy、payload）？

具体讨论开始之前，先以本卷为参考。

---

## 一、合成之 tool-call 对（synthetic ToolCallBlock + ToolResultBlock pairs）

### 1.1 灵魂心流之声（soul.flow）

- **触发**：`soul_delay` 秒定时器到，且器灵处 ACTIVE/IDLE
- **注入处**：`intrinsics/soul/flow.py:133-261` → `_run_consultation_fire()`
- **注入形**：合成 `(ToolCallBlock{name="soul", args={action:"flow"}}, ToolResultBlock)` 对
- **注入路径**：
  1. `flow.py:215` 调 `build_consultation_pair()` 构造对
  2. `flow.py:216` 入队 `tc_inbox.enqueue(InvoluntaryToolCall(..., coalesce=True, replace_in_history=True))`
  3. `flow.py:239` 发 `MSG_TC_WAKE` 入 `agent.inbox`
  4. 主循环 `_handle_tc_wake()` (`turn.py:404-488`) 将对拼接入 wire chat
  5. 或 `_drain_tc_inbox()` → `tc_inbox.drain_into()` (`tc_inbox.py:92-136`) 在安全边界处拼接
- **Coalesce**：`source="soul.flow"`，同源替换，`replace_in_history=True` 保证 history 中至多一对
- **Dismiss**：无 notif_id dismiss 路径；靠 `replace_in_history` 自然替换旧对

### 1.2 系统通知（邮件到达、退信、daemon 等）

- **触发**：`MailService` 收到新邮件 → `_on_mail_received()` 回调
- **注入处**：`base_agent/messaging.py:73-78` → `_enqueue_system_notification()`
- **注入形**：合成 `system(action="notification", notif_id=..., source=..., ref_id=..., received_at=...)` 对
- **注入路径**：
  1. `messaging.py:103-114` 构造 `ToolCallBlock` + `ToolResultBlock`
  2. `messaging.py:115-123` 入队 `tc_inbox.enqueue(InvoluntaryToolCall(..., coalesce=False, replace_in_history=False))`
  3. `messaging.py:138` 发 `MSG_TC_WAKE` 唤醒主循环
  4. 同上 `_handle_tc_wake()` 或 `draft_into()` 拼接
- **Dismiss**：`intrinsics/system/notification.py:1-59` → `interface.remove_pair_by_notif_id()` + `tc_inbox.remove_by_notif_id()` 双路径
- **自动 dismiss**：邮件通知在器灵回复该邮件时自动 dismiss（`_pending_mail_notifications` 追踪）

### 1.3 close_pending_tool_calls 之合成占位

- **触发**：LLM 回合中断（超时、AED 恢复、tc_wake 拼接失败等）
- **注入处**：`llm/interface.py:344-420` → `close_pending_tool_calls(reason)`
- **注入形**：对每个未答之 `ToolCallBlock`，合成 `ToolResultBlock(content=f"[aborted: {reason}]", synthesized=True)`
- **注入路径**：直接修改 `interface.entries`，无 tc_inbox
- **调用处**：
  - `session.py:191-195`（health_check pre-send pairing）
  - `turn.py:204`（AED 恢复循环内）
  - `turn.py:468`（tc_wake 单项拼接失败）
  - `turn.py:483`（tc_wake 外层错误恢复）

---

## 二、预置之系统提示（system prompt 每轮组装）

- **触发**：每次 `session.send()` → `update_system_prompt_batches()` / `update_system_prompt()`
- **注入处**：`prompt.py:146-164` → `build_system_prompt()` → `SystemPromptManager.render()`
- **注入形**：系统提示文本，以 `"\n\n---\n\n"` 拼接各段
- **注入路径**：
  1. `session.py:213-216` 在每次 `send()` 开头重建
  2. `base_agent/prompt.py:9-15` → `_build_system_prompt()` 调 `_refresh_tool_inventory_section()` + `build_system_prompt()`
  3. `agent.py:265-280` 覆写以注入工具描述
- **段序**（`prompt.py:39-53`）：
  - Batch 1（不动）：`principle` → `covenant` → `tools` → `procedures` → `comment`
  - Batch 2（少变）：`rules` → `brief` → `library` → `codex` → `identity` → `pad`
- **每段来源**：
  - `covenant`：init.json `covenant` 字段 → `system/covenant.md`（`agent.py:936-943`）
  - `principle`：init.json `principle` 字段 → `system/principle.md`（`agent.py:968-975`）
  - `procedures`：init.json `procedures` 字段 → `system/procedures.md`（`agent.py:978-985`）
  - `rules`：`system/rules.md`（非 init.json）（`agent.py:946-957`）
  - `brief`：init.json `brief` 字段 → `system/brief.md`（`agent.py:988-997`）
  - `comment`：init.json `comment` 字段（`agent.py:1000-1004`）
  - `pad`：`system/pad.md`（`agent.py:960-965`）
  - `identity`：由 `psyche.lingtai` 管理
  - `codex`：由 `codex` 工具管理
  - `library`：由 `library` 能力管理
  - `tools`：由 `_refresh_tool_inventory_section()` 动态生成

---

## 三、以 [system] [kernel] 等前缀之文

### 3.1 [system] 前缀 — .prompt 信号文件

- **触发**：heartbeat 线程每秒检测 `.prompt` 文件存在
- **注入处**：`base_agent/lifecycle.py:208-221`
- **注入形**：`agent.send(content, sender="system")` → 变为 `MSG_REQUEST` 消息，sender="system"
- **注入路径**：文件内容 → `_make_message(MSG_REQUEST, "system", content)` → `agent.inbox.put(msg)` → 主循环 `_handle_request()` → 作为 user-role 文本送入 LLM

### 3.2 [system] 前缀 — LICC MCP 事件

- **触发**：`MCPInboxPoller` 每 0.5 秒扫描 `.mcp_inbox/<mcp_name>/*.json`
- **注入处**：`core/mcp/inbox.py:103-116` → `_format_notification()`
- **注入形**：`"[system] New event from MCP '{mcp_name}'.\n  From: {sender}\n  Subject: {subject}\n  {preview}"`
- **注入路径**：`_dispatch_event()` → `_make_message(MSG_REQUEST, "system", notification)` → `agent.inbox.put(msg)` → 主循环处理

### 3.3 [system] 前缀 — _notify() 通用通知

- **触发**：addon 调用 `agent.notify(sender, text)`
- **注入处**：`base_agent/messaging.py:151-158` → `_notify()`
- **注入形**：`_make_message(MSG_REQUEST, sender, text)` — sender 由调用者指定
- **注入路径**：同上，走 `agent.inbox` → 主循环

### 3.4 [kernel] 前缀

- grep 结果中无 `[kernel]` 字面量注入。`[kernel]` 前缀可能仅见于文档/计划中，非实际运行时注入。（注：OpenAI overflow recovery 等场景可能有，但未在当前代码中见。）

---

## 四、信号文件所诱之注入

| 信号文件 | 触发 | 效果 | 文件:行 |
|----------|------|------|---------|
| `.prompt` | heartbeat 检测 | 注入文件内容为 `[system]` 消息 | `lifecycle.py:209-221` |
| `.clear` | heartbeat 检测 | 强制 molt（context_forget） | `lifecycle.py:224-242` |
| `.inquiry` | heartbeat 检测 | 触发 `_run_inquiry()`，文件内容为问题 | `lifecycle.py:244-284` |
| `.rules` | heartbeat 检测 | 更新 `system/rules.md` → 重建系统提示 | `lifecycle.py:407-442` |
| `.sleep` | heartbeat 检测 | 设 ASLEEP 状态 | `lifecycle.py:196-206` |
| `.suspend` | heartbeat 检测 | 设 SUSPENDED 状态 | `lifecycle.py:184-194` |
| `.refresh` | heartbeat 检测 | 触发刷新流程 | `lifecycle.py:171-182` |
| `.interrupt` | heartbeat 检测 | 设 cancel_event | `lifecycle.py:162-169` |

---

## 五、molt / context-overflow 之恢复辞

- **触发**：`turn.py:356-391` 在 `_handle_request()` 中，`get_context_pressure()` ≥ 阈值
- **注入形**：直接拼接在 user message content 之前
- **注入路径**：

| 条件 | 注入内容 | 文件:行 |
|------|----------|---------|
| `pressure >= molt_hard_ceiling` | `_t(lang, 'system.molt_wiped')` | `turn.py:360-366` |
| `pressure >= molt_pressure`，warnings ≤ max | `_t(lang, 'system.molt_warning_level{N}')` + 状态行 | `turn.py:367-391` |
| warnings > max_warnings | 强制 `context_forget()` + `_t(lang, 'system.molt_wiped')` | `turn.py:373-378` |

- **拼接形**：`f"{molt_prompt}\n{status}\n\n{content}"` — 直接前置于用户消息

---

## 六、mid-turn drain 所诱之 tc_inbox 对

- **触发**：每次 adapter 的 `send()` 内部，在消息已提交到 interface 但 API 调用之前
- **注入处**：`base_agent/__init__.py:634-691` → `_install_drain_hook()` 设置 `pre_request_hook`
- **注入路径**：
  1. `session.py` 的 `ChatSession` 在 `send()` 时调 `pre_request_hook`
  2. hook 调 `_drain_tc_inbox_for_hook()` → `tc_inbox.drain_into()`
  3. 拼接到 `interface.entries`
- **效果**：邮件通知和灵魂心流可在当前 tool-call 链中即时送达，不必等到回合结束
- **与 turn-boundary drain 的区别**：mid-turn drain 拼接的对仅在**下一次** API 调用中可见（对 server-state adapters）；对 canonical-interface adapters 则在当前请求中即可见

---

## 七、AED / stuck-revive / watchdog 之复辞

### 7.1 AED 恢复消息

- **触发**：LLM 调用失败，AED 重试
- **注入处**：`turn.py:239-241`
- **注入形**：`_t(lang, "system.stuck_revive", ts=ts, tool_calls=err_desc)` → `_make_message(MSG_REQUEST, "system", aed_msg)`
- **注入路径**：直接替换 `msg` 变量，下一轮 AED 循环用此 msg 重试

### 7.2 .llm_hang 信号

- **触发**：`session.send()` 阻塞超 120 秒
- **注入处**：`turn.py:46-57` → 写 `.llm_hang` JSON 信号文件
- **注入形**：文件写入，非文本注入。但器灵醒来时会检测此文件并拒绝唤醒（`turn.py:157-164`）

### 7.3 Health check 合成占位

- **触发**：`session.send()` 前，interface 尾部有未答 tool_calls
- **注入处**：`session.py:191-195` → `close_pending_tool_calls(reason="health_check:pre_send_pairing")`
- **注入形**：合成 `[aborted: health_check:pre_send_pairing]` ToolResultBlock

---

## 八、MCP / addon 所诱之事件

### 8.1 LICC inbox（MCP 外部进程事件）

- **触发**：MCP 子进程写 JSON 文件到 `.mcp_inbox/<name>/<id>.json`
- **注入处**：`core/mcp/inbox.py:119-136` → `_dispatch_event()`
- **注入形**：`[system] New event from MCP '{name}'.\n  From: ...\n  Subject: ...\n  ...`
- **注入路径**：`_make_message(MSG_REQUEST, "system", notification)` → `agent.inbox`
- **与 tc_inbox 的区别**：LICC 事件走**文本通道**（MSG_REQUEST），不走合成 tool-call 对

### 8.2 邮件到达

- **触发**：`FilesystemMailService` 检测到新邮件文件
- **注入处**：`base_agent/messaging.py:11-78`
- **注入形**：合成 `system(action="notification")` tool-call 对（见 §1.2）
- **内容**：由 i18n 模板 `system.new_mail` 生成，含：box、address、name、subject、sent_at、preview（截断 500 字）、tool 名

### 8.3 邮件退信

- **触发**：邮件投递失败
- **注入路径**：同上，`source="email.bounce"`

---

## 九、time_veil / 状态通报（每 tool result 附 meta）

### 9.1 text-input 前缀

- **触发**：每次 `_handle_request()` 在发 LLM 之前
- **注入处**：`turn.py:393-395`
- **注入形**：`render_meta(agent, meta)` → `"[2026-05-04T14:59:47-07:00 | 上下文：8.9% (系统 35512 + 对话 0)]"`
- **注入路径**：直接拼接在 user content 之前：`f"{prefix}\n\n{content}"`

### 9.2 tool-result stamp

- **触发**：每次 tool 执行完毕
- **注入处**：`meta_block.py:201-223` → `stamp_meta(result, meta, elapsed_ms)`
- **注入形**：将 `build_meta()` 的 dict 键值平铺到 tool result dict 中：
  - `current_time`：ISO 时戳（time-aware 时）
  - `context`：`{system_tokens, history_tokens, usage}`
  - `stamina_left_seconds`：剩余秒数
  - `_elapsed_ms`：本次 tool 调用耗时
- **注入路径**：`ToolExecutor` 在每个 tool result 返回前调用 `stamp_meta()`

### 9.3 build_meta() 数据源

- `meta_block.py:31-148` → 综合以下数据：
  - `time_veil.now_iso(agent)` → 当前时戳（受 time_awareness 控制）
  - `session._system_prompt_tokens` / `_tools_tokens` / `_latest_input_tokens` → token 分解
  - `agent._uptime_anchor` + `agent._config.stamina` → 剩余时间

---

## 十、其他注入点

### 10.1 _concat_queued_messages() 消息合并

- **触发**：主循环取出消息后，检查 inbox 是否有排队的同类型消息
- **注入处**：`turn.py:272-315`
- **注入形**：多个 MSG_REQUEST / MSG_USER_INPUT 消息以 `"\n\n"` 合并为一条
- **效果**：不产生新注入点，但改变了消息内容的边界

### 10.2 Auto-insight

- **触发**：`insights_interval` > 0 且连续回合数达到阈值
- **注入处**：`turn.py:254-264` → `_run_inquiry(agent, question, source="auto")`
- **注入形**：走 soul inquiry 路径，结果作为 `soul(action="inquiry")` tool result 注入

### 10.3 Soul inquiry（/btw 或 auto-insight）

- **触发**：`.inquiry` 信号文件或 auto-insight
- **注入处**：`intrinsics/soul/inquiry.py`
- **注入形**：独立 LLM 会话，结果作为 `soul(action="inquiry")` tool-call 对注入主对话
- **注入路径**：结果通过 `build_inquiry_pair()` 构造合成对 → `tc_inbox`

### 10.4 灵魂心流之 consultation 拒绝

- **触发**：心流 consultation 中器灵尝试调用工具
- **注入处**：`intrinsics/soul/consultation.py:458-467`
- **注入形**：`ToolResultBlock(content=_CONSULTATION_TOOL_REFUSAL)` — 完整 consultation system prompt 作为拒绝内容
- **注意**：此注入发生在**独立的 consultation session** 中，不影响主对话

---

## 总结：注入路径分类

| 路径 | 注入形 | 可 dismiss？ | 来源 |
|------|--------|-------------|------|
| `tc_inbox` → wire chat | 合成 tool-call 对 | 通知类可；心流类不可 | §1 |
| `agent.send(sender="system")` | user-role 消息 | 不可 | §3.1, §3.3 |
| `_make_message(MSG_REQUEST, "system", ...)` | user-role 消息 | 不可 | §3.2, §8.1 |
| system prompt 每轮重建 | system-role 提示 | N/A（每轮重建） | §2 |
| user content 前缀拼接 | 文本前置 | 不可 | §5, §9.1 |
| tool result dict 平铺 | 结构化字段 | 不可 | §9.2 |
| `close_pending_tool_calls()` | 合成 ToolResultBlock | 不可（已固化） | §1.3, §7.3 |

---

## 对 single-slot replace 模式之评估

当前已用 single-slot replace 模式者：
- **soul.flow**：`coalesce=True, replace_in_history=True` — 入队时替换队列中同源项，拼接时替换 history 中旧对。✅ 已是 single-slot

可考虑改为 single-slot replace 而非逐次 append 者：
- **系统通知（邮件到达）**：当前 `coalesce=False, replace_in_history=False` — 每封邮件一条通知，history 中累积。若邮件频繁，可考虑按 source coalesce（如 `source="email"`），但会丢失中间邮件的通知。需权衡。（注：`email-unread-digest-notification-patch.md` 已设计为 `coalesce=True, replace_in_history=True`，本表反映**补丁前**状态。）
- **LICC MCP 事件**：走文本通道（MSG_REQUEST），每条独立。若需 single-slot，需改为走 tc_inbox 路径并设 `coalesce=True`。
- **AED recovery 消息**：每次替换 msg 变量，已是 single-slot（仅在 AED 循环内）。
- **molt 警告**：每次拼接在 content 前，已是 single-slot（每轮一次）。
