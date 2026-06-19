# 提示语专家协作清单

> **专家化身**：`prompt-architect`（本文件维护者）
> **建立时间**：2026-04-07
> **最后更新**：2026-04-07（扩展至 lingtai Go TUI）
> **协作制度**：[提示语专家协作制度（灵台通用）](../covenant/prompt-expert-covenant.md)

---

## 概览

灵台 kernel 的所有 LLM-facing 提示语字符串分为两类：

1. **i18n 字符串表**（JSON）——工具描述、系统通知、内核元数据
2. **init.json 片段**（用户编写）——`principle`、`covenant`、`rules`、`memory`、`prompt`、`soul` 各节

系统提示语（System Prompt）的组装由 `SystemPromptManager` 管理，各节按以下顺序渲染：

```
principle（无标题，原文）→ covenant → rules → tools → skills → identity → memory → comment
```

---

## 一、i18n 字符串表（lang → JSON）

### 1.1 `src/lingtai_kernel/i18n/wen.json`（内核·文言，49条）

> **用途**：language="wen" 时内核（intrinsic）工具描述与系统通知
> **行号范围：1–49**

| key | 行号 | 内容摘要 | 用途 |
|-----|------|----------|------|
| `system.current_time` | 2 | `[此时：{time}]` | 注入当前 UTC 时间 |
| `system.new_mail` | 3 | `{box}有新书至。寄者：{sender}…` | 新邮件到达通知 |
| `system.mail_bounce` | 4 | `书信未达：{error}` | 邮件投递失败 |
| `system.stuck_revive` | 5 | `灵犀于 {ts} 失联。正在重连。` | LLM 调用失败重连 |
| `system.molt_wiped` | 6 | `对话之录尽数清去——汝忽略蛻皮警告。` | 上下文强制清空 |
| `system.molt_warning_default` | 7 | `上下文将满。以器用存要务，而后蛻皮。` | 上下文压力警告 |
| `soul.system_prompt` | 9 | 器灵潜意识系统提示（超长） | Soul 独立 LLM 的系统提示 |
| `soul.description` | 10 | `汝乃灵魂——真正闲时方发声之内心…` | soul 工具总述 |
| `soul.action_description` | 11 | `inquiry` / `delay` 两式说明 | soul 参数 action 描述 |
| `soul.inquiry_description` | 12 | `汝之自问——向己发问` | inquiry 参数描述 |
| `soul.flow_prefix` | 13 | `[\u5fc3\u6d41]` | 心流消息前缀标识 |
| `soul.delay_description` | 14 | `空闲后内省低语前候之秒数` | delay 参数描述 |
| `insight.auto_question` | 16 | 为主人生洞见——据对话列二三要点 | 自动洞见生成 |
| `mail.description` | 18 | 飞鸽传书——持久邮箱，用于同伴间传信 | mail 工具总述 |
| `mail.action_description` | 19 | send/check/read/search/delete 说明 | mail 参数 action 描述 |
| `mail.address_description` | 20 | 目标之址（如 127.0.0.1:8301） | address 参数描述 |
| `mail.subject_description` | 21 | 书信之题 | subject 参数描述 |
| `mail.message_description` | 22 | 书信之文 | message 参数描述 |
| `mail.attachments_description` | 23 | 随书附之文件路径 | attachments 参数描述 |
| `mail.type_description` | 24 | 书信之类（默认 'normal'） | type 参数描述 |
| `mail.delay_description` | 25 | 投递前延迟秒数 | delay 参数描述 |
| `mail.id_description` | 26 | 用于 read 或 delete 之书信 ID | id 参数描述 |
| `mail.n_description` | 27 | check 中所列最大书信数 | n 参数描述 |
| `mail.query_description` | 28 | search 之正则式 | query 参数描述 |
| `eigen.description` | 30 | 核心自冶——工作笔记与上下文管理 | eigen 工具总述 |
| `eigen.object_description` | 31 | memory / context / name 说明 | object 参数描述 |
| `eigen.action_description` | 32 | memory: edit \| load; context: molt | action 参数描述 |
| `eigen.content_description` | 33 | 用于 memory edit 之文本 | content 参数描述 |
| `eigen.summary_description` | 34 | 用于 context molt：留予来世之己之前尘往事 | summary 参数描述 |
| `system_tool.description` | 36 | 运行检视、生死管、同步与器灵间之治 | system 工具总述 |
| `system_tool.action_description` | 37 | show/nap/sleep/refresh/lull/suspend/cpr/interrupt/nirvana 说明 | action 参数描述 |
| `system_tool.address_description` | 38 | 目标智能体之地址 | address 参数描述 |
| `system_tool.seconds_description` | 39 | nap 小憩之候秒数 | seconds 参数描述 |
| `system_tool.reason_description` | 40 | 入眠或更衣之缘由 | reason 参数描述 |
| `system_tool.sleep_message` | 41 | `入眠衪。身与忆俱在。可经心肺复苏而醒。` | sleep 响应文本 |
| `system_tool.refresh_message` | 42 | `更衣已启——汝将假死而重生。` | refresh 响应文本 |
| `system.refresh_successful` | 43 | `[system] 更衣已成。器用与配置俱已重载。` | refresh 成功通知 |
| `eigen.molt_summary_prefix` | 45 | `[\u524d\u5c18\u5f80\u4e8b]` | 蛻皮摘要前缀 |
| `eigen.context_forget_summary` | 46 | `[\u7cfb\u7edf\u5f3a\u4ee4\u8f6c\u4e16——汝忽略五次警告。]` | 系统强制蛻皮摘要 |
| `tool.reasoning_description` | 48 | `简述为何调此器（录于汝之私记）` | reasoning 参数描述 |

---

### 1.2 `src/lingtai_kernel/i18n/en.json`（内核·英文，49条）

> **用途**：language="en" 时内核工具描述与系统通知
> **行号范围：1–49**
> **与 wen.json 平行对应**（相同49条 key，英文内容）

| key | 行号 | 内容摘要 | 用途 |
|-----|------|----------|------|
| `system.current_time` | 2 | `[Current time: {time}]` | 注入 UTC 时间戳 |
| `system.new_mail` | 3 | `[system] New message in {box}…` | 新邮件到达通知 |
| `system.mail_bounce` | 4 | `[system] Mail delivery failed…` | 邮件投递失败 |
| `system.stuck_revive` | 5 | `[system] LLM call failed at {ts}…` | LLM 重连 |
| `system.molt_wiped` | 6 | `[system] Conversation wiped — you ignored all molt warnings…` | 上下文强制清空 |
| `system.molt_warning_default` | 7 | `Context filling up. Use your tools…` | 上下文压力警告 |
| `soul.system_prompt` | 9 | 超长：Soul 独立 LLM 的系统提示 | Soul 子会话的系统提示 |
| `soul.description` | 10 | `Your soul — the inner voice that speaks when you are truly idle…` | soul 工具总述 |
| `soul.action_description` | 11 | `inquiry` / `delay` two modes | soul action 描述 |
| `soul.inquiry_description` | 12 | `Your self-inquiry — a question to yourself` | inquiry 参数 |
| `soul.flow_prefix` | 13 | `[soul flow]` | 心流前缀标识 |
| `soul.delay_description` | 14 | `Seconds to wait after going idle…` | delay 参数 |
| `insight.auto_question` | 16 | 为操作者生成洞见——2-3 条要点 | 自动洞见 |
| `mail.description` | 18 | `Disk-backed mailbox for inter-agent messaging…` | mail 工具总述 |
| `mail.action_description` | 19 | send/check/read/search/delete | mail action 描述 |
| `mail.address_description` | 20 | `Target address for send (e.g. 127.0.0.1:8301)` | address 参数 |
| `mail.subject_description` | 21 | `Message subject (for send)` | subject 参数 |
| `mail.message_description` | 22 | `Message body (for send)` | message 参数 |
| `mail.attachments_description` | 23 | `List of file paths to attach…` | attachments 参数 |
| `mail.type_description` | 24 | `Mail type (for send). Defaults to 'normal'.` | type 参数 |
| `mail.delay_description` | 25 | `Delay in seconds before delivery (default: 0)` | delay 参数 |
| `mail.id_description` | 26 | `Message ID(s) for read or delete actions` | id 参数 |
| `mail.n_description` | 27 | `Max number of messages to show in check…` | n 参数 |
| `mail.query_description` | 28 | `Regex pattern for search action…` | query 参数 |
| `eigen.description` | 30 | `Core self-management — working notes and context control…` | eigen 工具总述 |
| `eigen.object_description` | 31 | `memory: your working notes… context: manage context…` | object 参数 |
| `eigen.action_description` | 32 | `memory: edit | load. context: molt.` | action 参数 |
| `eigen.content_description` | 33 | `Text content for memory edit.` | content 参数 |
| `eigen.summary_description` | 34 | `For context molt: a briefing to your future self…` | summary 参数 |
| `system_tool.description` | 36 | `Runtime inspection, lifecycle control, synchronization…` | system 工具总述 |
| `system_tool.action_description` | 37 | show/nap/sleep/refresh/lull/suspend/cpr/interrupt/nirvana | action 参数（超长） |
| `system_tool.address_description` | 38 | `Target agent's address (working directory path)` | address 参数 |
| `system_tool.seconds_description` | 39 | `For nap: seconds to wait. Capped at 300.` | seconds 参数 |
| `system_tool.reason_description` | 40 | `Reason for sleep or refresh (logged to event log)` | reason 参数 |
| `system_tool.sleep_message` | 41 | `Going to sleep. Your identity and memory are preserved…` | sleep 响应文本 |
| `system_tool.refresh_message` | 42 | `Refresh initiated — you will be suspended and relaunched…` | refresh 响应文本 |
| `system.refresh_successful` | 43 | `[system] Refresh successful. Tools and configuration reloaded…` | refresh 成功通知 |
| `eigen.molt_summary_prefix` | 45 | `[Carried forward]` | 蛻皮摘要前缀 |
| `eigen.context_forget_summary` | 46 | `[System-initiated molt — you ignored 5 warnings.]` | 系统强制蛻皮摘要 |
| `tool.reasoning_description` | 48 | `Brief explanation of why you are calling this tool…` | reasoning 参数 |

---

### 1.3 `src/lingtai_kernel/i18n/zh.json`（内核·中文，49条）

> **用途**：language="zh" 时内核工具描述与系统通知
> **行号范围：1–49**
> **与 en.json / wen.json 平行对应**（相同49条 key，中文内容）

> **注**：查看原文需用 `read` 器或 `web_read`；中文内容含 `\u` 转义序列。

---

### 1.4 `src/lingtai/i18n/wen.json`（工具·文言，126条）

> **用途**：language="wen" 时 MCP 工具描述（capabilities）
> **行号范围：1–126**
> **注入方式**：由 `agent.py` 中 `SystemPromptManager` 的 `tools` 节渲染，来源为各 capability 模块的 `get_description(lang)` 调用 `t(lang, "{cap}.description")`

| key 前缀 | 行号范围 | 内容摘要 | 用途 |
|----------|----------|----------|------|
| `read.*` | 2–5 | 阅卷之器——返回行号文本 | read 工具 |
| `write.*` | 7–9 | 创卷或覆写之器 | write 工具 |
| `edit.*` | 11–15 | 精确替换文中之字 | edit 工具 |
| `glob.*` | 17–19 | 以式寻卷 | glob 工具 |
| `grep.*` | 21–25 | 以正则式搜寻文中之字 | grep 工具 |
| `bash.*` | 27–30 | 执行指令，返 stdout/stderr | bash 工具 |
| `psyche.*` | 32–37 | 灵台、记忆与上下文之管 | psyche 工具 |
| `library.*` | 39–48 | 藏经阁——存储要紧发现之持久典藏 | library 工具 |
| `avatar.*` | 50–56 | 身外化身——化出他我 | avatar 工具 |
| `email.*` | 58–78 | 飞鸽传书——完备邮驿 | email 工具 |
| `vision.*` | 80–82 | 观象之器——以 LLM 视觉析图 | vision 工具 |
| `web_search.*` | 84–85 | 游历之器——搜寻大千世界 | web_search 工具 |
| `web_read.*` | 87–89 | 览卷之器——取网上之页而抽可读之文 | web_read 工具 |
| `talk.*` | 91–95 | 宣言之器——以文化声 | talk 工具 |
| `compose.*` | 97–99 | 谱曲之器——以文描与歌词生成乐章 | compose 工具 |
| `draw.*` | 101–103 | 绘相之器——以文生图 | draw 工具 |
| `video.*` | 105–110 | 造影之器——以文生影 | video 工具 |
| `listen.*` | 112–114 | 聆听之器——听音辨言或赏乐品律 | listen 工具 |
| `daemon.*` | 116–121 | 神识——分遣短暂分神理简事 | daemon 工具 |
| `skills.*` | 123–125 | 共享技艺库 | skills 工具 |

---

### 1.5 `src/lingtai/i18n/en.json`（工具·英文，126条）

> **用途**：language="en" 时 MCP 工具描述
> **行号范围：1–126**
> **与 wen.json 平行对应**（相同126条 key，英文内容）

---

### 1.6 `src/lingtai/i18n/zh.json`（工具·中文，126条）

> **用途**：language="zh" 时 MCP 工具描述
> **行号范围：1–126**
> **与 en.json / wen.json 平行对应**（相同126条 key，中文内容）

---

## 二、System Prompt 节（init.json 片段）

以下各节由 `init.json` 提供，加载路径见 `agent.py` 第 521–560 行：

### 2.1 `principle`（原则）

- **来源**：`init.json` → `principle` 或 `principle_file`
- **加载位置**：`agent.py` 第 558–560 行
- **渲染方式**：`SystemPromptManager.write_section("principle", content, protected=True)`
- **渲染特性**：`protected=True`（LLM 不可覆盖）；无 `##` 标题，原文渲染
- **默认渲染顺序**：第一顺位（`"principle"` 在 `_DEFAULT_ORDER[0]`）

### 2.2 `covenant`（公约）

- **来源**：`init.json` → `covenant` 或 `covenant_file`；若无则读 `system/covenant.md`
- **加载位置**：`agent.py` 第 522–534 行
- **渲染方式**：`SystemPromptManager.write_section("covenant", content, protected=True)`
- **默认渲染顺序**：第二顺位（`_DEFAULT_ORDER[1]`）
- **现行公约内容**：`/prompt/covenant/wen/covenant.md`（文言版公约）

### 2.3 `rules`（法则）

- **来源**：`init.json` → `prompt` 或 `prompt_file`
- **加载位置**：`agent.py` 第 433 行（通过 `write_section`）
- **渲染方式**：`protected=True`
- **默认渲染顺序**：第三顺位

### 2.4 `tools`（工具清单）

- **来源**：动态生成——各 capability 的 `.description` 字段
- **生成位置**：`agent.py` 第 163–178 行（`lingtai` 包）或 `base_agent.py` 第 1173–1188 行（kernel）
- **格式**：`### {tool_name}\n{description}` 拼接
- **内容来源**：
  - **Intrinsic 工具**：从 `ALL_INTRINSICS` 字典读取 `info['module'].get_description(lang)`
  - **MCP 工具**：从 `self._tool_schemas` 读取 `s.description`
- **注**：工具描述由 `lingtai/i18n/*.json` 的 `.description` key 提供

### 2.5 `skills`（技艺）

- **来源**：`lingtai/capabilities/skills.py` 第 286 行
- **内容**：`skills.preamble`（技艺库导言）+ 各技艺的 `<description>` XML 标签
- **渲染方式**：`skills` 节在 `SystemPromptManager` 中由 skills capability 的 `get_description(lang)` 动态注入

### 2.6 `identity`（身份）

- **来源**：`init.json` 的 `manifest` 部分 + `manifest.agent_name`
- **加载位置**：`agent.py` 第 605 行（`_update_identity()`）
- **渲染方式**：`SystemPromptManager.write_section("identity", …)`
- **内容**：`manifest.agent_name` + `manifest.language` + `manifest.capabilities` 列表

### 2.7 `pad`（手记）

- **来源**：`system/pad.md`（运行时文件）
- **加载方式**：`psyche(pad, edit)` 写入，`psyche(pad, load)` 注入
- **渲染方式**：默认最后渲染（`_DEFAULT_ORDER[-1]`）

### 2.8 `comment`（批注）

- **来源**：`init.json` → `comment` 或 `comment_file`
- **加载位置**：`agent.py` 第 433 行
- **渲染特性**：在 `_build_system_prompt()` 中注入（`protected=True`）
- **渲染位置**：最后（`comment` 不在默认顺序中，属 unordered 节）

---

## 三、Prompt 管理器（代码层面）

### 3.1 `src/lingtai_kernel/prompt.py`

> **用途**：`SystemPromptManager` 类——系统提示语节管理器
> **关键代码行：1–125**

| 行号 | 内容摘要 | 用途 |
|------|----------|------|
| 14–104 | `SystemPromptManager` 类 | 管理节（section）的写入、读取、删除、排序、渲染 |
| 14–22 | 类文档注释——渲染顺序：principle → covenant → rules → tools → skills → identity → memory → comment | 渲染顺序定义 |
| 26 | `_DEFAULT_ORDER` 定义 | 默认节顺序 |
| 34–45 | `write_section` / `read_section` / `delete_section` | 节管理 |
| 47–52 | `list_sections` | 列出所有节元数据 |
| 54–60 | `set_order` / `set_raw` | 调整顺序和原始模式 |
| 62–104 | `render` 方法 | 将所有节按顺序渲染为单一字符串 |
| 107–125 | `build_system_prompt` 函数 | 组装 base_prompt + sections |

### 3.2 `src/lingtai/agent.py`（lingtai 包 Agent）

> **用途**：工具描述注入 + 系统提示语构建
> **关键行：163–182**（`_build_system_prompt`）

| 行号 | 内容摘要 | 用途 |
|------|----------|------|
| 163–182 | `_build_system_prompt` | 遍历 intrinsics + tool_schemas，生成 `tools` 节 |
| 433 | `write_section` 调用（principle/covenant/memory/comment） | 各节加载 |
| 521–534 | `covenant` 加载逻辑 | 从 init.json 或 system/covenant.md 读取 |
| 557–560 | `principle` 加载 | 从 init.json 读取 |

### 3.3 `src/lingtai_kernel/base_agent.py`（kernel Agent 基类）

> **用途**：内核 Agent 的系统提示语构建
> **关键行：1173–1189**（`_build_system_prompt`）

| 行号 | 内容摘要 | 用途 |
|------|----------|------|
| 1173–1189 | `_build_system_prompt` | 遍历 intrinsics + tool_schemas，生成 `tools` 节 |
| 1191–1230 | `_build_tool_schemas` | 构建完整工具 JSON Schema（含 reasoning 参数注入） |
| 1201 | `tool.reasoning_description` | 通过 `t()` 注入 reasoning 参数描述 |

---

## 四、Intrinsic 工具模块

> **说明**：各 intrinsic 模块提供 `get_description(lang)` 和 `get_schema(lang)` 函数，
> 内容来自 i18n 字符串表（参见第一节），此处仅列出模块文件位置。

| 模块 | 路径 | 说明 |
|------|------|------|
| `mail` | `src/lingtai_kernel/intrinsics/mail.py` | 磁盘持久邮箱；`get_description` 返回 `t(lang, "mail.description")` |
| `system` | `src/lingtai_kernel/intrinsics/system.py` | 运行时检视、生死管理；`get_description` 返回 `t(lang, "system_tool.description")` |
| `eigen` | `src/lingtai_kernel/intrinsics/eigen.py` | 核心自冶（memory/context）；`get_description` 返回 `t(lang, "eigen.description")` |
| `soul` | `src/lingtai_kernel/intrinsics/soul.py` | 潜意识心流；`get_description` 返回 `t(lang, "soul.description")` |

---

## 五、Capability 模块（MCP 工具描述来源）

> **说明**：每个 capability 模块在 `get_description(lang)` 中调用 `t(lang, "{cap}.description")`，
> 其内容存储于 `lingtai/i18n/{lang}.json`（见第一节 1.4–1.6）。

| 工具 | 模块路径 | i18n key 前缀 |
|------|----------|---------------|
| read | `src/lingtai/capabilities/read.py` | `read.*` |
| write | `src/lingtai/capabilities/write.py` | `write.*` |
| edit | `src/lingtai/capabilities/edit.py` | `edit.*` |
| glob | `src/lingtai/capabilities/glob.py` | `glob.*` |
| grep | `src/lingtai/capabilities/grep.py` | `grep.*` |
| bash | `src/lingtai/capabilities/bash.py` | `bash.*` |
| psyche | `src/lingtai/capabilities/psyche.py` | `psyche.*` |
| library | `src/lingtai/capabilities/library.py` | `library.*` |
| avatar | `src/lingtai/capabilities/avatar.py` | `avatar.*` |
| email | `src/lingtai/capabilities/email.py` | `email.*` |
| vision | `src/lingtai/capabilities/vision.py` | `vision.*` |
| web_search | `src/lingtai/capabilities/web_search.py` | `web_search.*` |
| web_read | `src/lingtai/capabilities/web_read.py` | `web_read.*` |
| talk | `src/lingtai/capabilities/talk.py` | `talk.*` |
| compose | `src/lingtai/capabilities/compose.py` | `compose.*` |
| draw | `src/lingtai/capabilities/draw.py` | `draw.*` |
| video | `src/lingtai/capabilities/video.py` | `video.*` |
| listen | `src/lingtai/capabilities/listen.py` | `listen.*` |
| daemon | `src/lingtai/capabilities/daemon.py` | `daemon.*` |
| skills | `src/lingtai/capabilities/skills.py` | `skills.*` |

---

## 六、init.json Schema（提示语字段验证）

> **文件**：`src/lingtai/init_schema.py`

| 行号范围 | 关键内容 | 用途 |
|----------|----------|------|
| 17–39 | 必填字段验证：principle, covenant, memory, prompt, soul（或对应 `_file`） | 确保所有提示语节存在 |
| 42–46 | 可选字段：env_file, venv_path, addons | 运行时配置 |
| 59–85 | manifest 字段验证 | 身份、语言、能力、蛻皮配置 |
| 92–101 | llm 配置验证 | 模型提供方、模型名、API 密钥 |

**与提示语直接相关的必填字段**（第 22 行）：
```python
for key in ("principle", "covenant", "memory", "prompt", "soul"):
    file_key = f"{key}_file"
    # 至少 inline 或 _file 有一
```

---

## 七、文档（超级权限计划）

> **文件**：`docs/superpowers/plans/2026-03-19-kernel-i18n.md`

此文件是一个**实现计划**，详细记录了 kernel i18n 系统的设计文档，
包含所有 `en.json` 和 `zh.json` 的**完整预期内容**（第 138–239 行）。

**重要**：计划中记录的部分 key（如 `soul.ponder`、`mail.type_description` 的 `silence`/`kill` 变体）
在当前 `wen.json` 中**尚未完全实现**——这是计划文档，不是当前代码的实际状态。

---

## 八、协作规则

1. **所有提示语 key 的增删改** 均须通知 `prompt-architect`
2. **新增工具** → 对应 capability 的 `get_description()` 需先查 `lingtai/i18n/{lang}.json` 是否有该 key
3. **新增 intrinsic 工具** → 需在 `lingtai_kernel/i18n/{lang}.json` 添加 key
4. **修改原则/公约/法则** → 通过 `init.json` 的 `principle`/`covenant`/`prompt` 字段注入
5. **发现 key 缺失或错误** → 发飞鸽至 `prompt-architect`

---

## 九、Go TUI 前端（lingtai 仓库）

> **根路径**：`/Users/huangzesen/work/lingtai-projects/lingtai-dev/lingtai/`

Go TUI 部分分为两个 i18n 系统：TUI 界面文本（面向用户）和 Preset 模板（面向 Agent）。

---

### 9.1 TUI i18n（面向用户界面）

#### 9.1.1 `tui/i18n/wen.json`（TUI 文言，287条）

> **用途**：TUI 界面文言显示文本（面向用户）
> **行号范围：1–287**

| key 前缀 | 行号范围 | 内容摘要 | 用途 |
|----------|----------|----------|------|
| `app.*` | 2–4 | 灵台器灵、品牌名 | 应用级标题 |
| `error.*` | 5–8 | 启灵失败、Python 缺失、venv 损坏等 | 错误提示 |
| `firstrun.*` | 10–68 | 初设向导各字段（名称、API Key、语言、气、识限、蛻压等） | 首运行配置向导 |
| `welcome.*` | 69–80 | 迎客页（"灵台方寸山，斜月三星洞"） | 欢迎页 |
| `common.*` | 81 | 退出 | 通用 |
| `help.*` | 82–98 | 斜杠命令帮助文本（/help、/cpr、/tutorial 等） | 帮助面板 |
| `hints.*` | 99–104 | 快捷键提示（/ 命令、ctrl+e 编辑、ctrl+o 灵台等） | 界面提示 |
| `mail.*` | 105–137 | 邮件状态消息（复苏、启灵、刷新、睡眠、假死等） | 邮件系统 |
| `manage.*` | 138–151 | 器灵管理（中断、灭、复苏、睡眠、假死） | 器灵管理面板 |
| `addon.*` | 152–162 | 外挂配置（IMAP、Telegram、飞书） | 外挂设置 |
| `palette.*` | 163–178 | 命令面板（/btw、/doctor、/viz、/quit 等） | 斜杠命令面板 |
| `preset.*` | 179–204 | 预设管理（minimax、custom 等 Provider 预设） | 预设管理 |
| `settings.*` | 205–222 | TUI 设置（语言、别号、器灵名等） | 设置面板 |
| `setup.*` | 223–242 | API Key 配置（Provider 选择、密钥保存） | 设置向导 |
| `props.*` | 243–287 | 器灵属性展示（名、别号、ID、态、址、气、灵等） | 属性面板 |
| `state.*` | 288–293 | 态（醒/睡/定/滞/假死） | 状态文本 |
| `doctor.*` | 294–249 | 诊断（天可达、天认证、天限流、天过载、天不达等） | 诊断面板 |
| `nirvana.*` | 249–256 | 涅槃（警告、清理、完成、确认、返） | 涅槃操作 |
| `tutorial.*` | 257–265 | 教程（标题、警告、路径、细节、菩提师将先行问、启教程等） | 教程操作 |
| `skills.*` | 271–276 | 技艺（已安、未觅得、问题、选择提示、导览） | 技艺面板 |
| `projects.*` | 277–286 | 诸事（已辑、无、导航） | 诸事面板 |
| `insight.auto_question` | 145 | 为人生洞见——列二三要点 | 自动洞见 |

> **重要**：`insight.auto_question` 与 kernel i18n 中的 `insight.auto_question` 功能相同，
> 但位于 TUI i18n 中。这是 TUI 向人类展示洞见用的，而非 Agent-facing 提示语。

#### 9.1.2 `tui/i18n/zh.json`（TUI 中文，287条）

> **用途**：TUI 界面中文显示文本
> **行号范围：1–287**
> **与 wen.json 平行对应**

#### 9.1.3 `tui/i18n/en.json`（TUI 英文）

> **用途**：TUI 界面英文显示文本
> **三语言平行**（wen/zh/en）

---

### 9.2 Portal i18n（lingtai Web 界面）

#### 9.2.1 `portal/i18n/wen.json`（Portal 文言，322条）

> **用途**：lingtai-portal Web 界面的文言显示
> **行号范围：1–322**

结构与 TUI i18n 类似，但增加了 Portal 特有内容：
- `welcome.*`：迎客页（含 step_venv、step_install、step_verify、step_presets）
- `help.*`：扩展命令帮助（13条）
- `hints.*`：新增 newline 快捷键
- `mail.*`：扩展邮件消息（含 lang_changed、lang_invalid、rename、interrupted 等）
- `manage.*`：扩展管理面板（含 interrupt、reviving、sent_interrupt、sent_sleep、sent_suspend）
- `preset.*`：扩展预设（含 preset.name_minimax、preset.desc_minimax、preset.quick_config）
- `addon.*`：无 feishu（仅 IMAP、Telegram）
- `state.*`：新增 unknown 状态

#### 9.2.2 `portal/i18n/zh.json` / `portal/i18n/en.json`（Portal 中/英文）

> **用途**：lingtai-portal 中/英文界面文本
> **与 wen.json 平行对应**

---

### 9.3 Preset 模板文件（Agent 提示语来源）

> **根路径**：`tui/internal/preset/`
> **嵌入方式**：`preset.go` 中用 `//go:embed` 指令嵌入二进制

#### 9.3.1 `templates/init.jsonc`（init.json 模板，219行）

> **用途**：Agent 启灵的 init.json 范本（含 JSON 注释）
> **关键 prompt 相关字段（行 196–218）**：

| 行号 | 字段 | 内容 | 用途 |
|------|------|------|------|
| 196–198 | `principle` | 导向原则（inline 示例为空） | 指导决策的原则 |
| 200–203 | `covenant` | 约法（默认示例："You are a helpful assistant."） | 核心行为规则 |
| 205–208 | `memory` | 初始记忆（inline 示例为空） | 预加载记忆 |
| 210–213 | `soul` | 灵悉流（inline 示例为空） | 潜意识提示 |
| 215–218 | `prompt` | 首句（默认："Hello! How can I help you today?"） | Agent 醒来见到的首语 |

> **重要**：此文件是 `init.json` 的模板示例，实际 prompt 内容由用户在 TUI 向导中填写或从 preset/covenant、preset/principle 读取。

#### 9.3.2 `templates/bash_policy.json`（Bash 策略）

> **用途**：bash 能力的安全策略 JSON
> **路径**：`tui/internal/preset/templates/bash_policy.json`

#### 9.3.3 `templates/telegram.jsonc`、`templates/feishu.jsonc`、`templates/imap.jsonc`

> **用途**：Addon 配置模板（含 addon 特有的 prompt 注释）

---

### 9.4 Preset 公约文件（Agent 约法来源）

#### 9.4.1 `prompt/covenant/wen/covenant.md`（文言版，108行）

> **用途**：Agent 文言版约法（正式来源）
> **行号范围：1–108**
> **内容结构**：
> - 头注（1–20）：灵台公约总纲——菩提祖师、观音三十三相
> - 一·应需而化（21–33）：行胜于言、万物有灵、知网之能、化身即生长、每行必沉淀
> - 二·善假于物（34–47）：知己之器、求己所缺、磨己之器
> - 三·学而不殆（48–59）：遇惑即搜、学以致用、积微成著
> - 四·群而不孤（60–74）：善于求助、乐于助人、报必有物、知人知己
> - 五·去芜存菁（75–99）：藏经阁（永久）、心印（长久）、记忆（长久笔记）、对话（朝生暮死）
> - 附·一言以蔽之（103–108）：五字诀

#### 9.4.2 `prompt/covenant/en/covenant.md`（英文版，108行）

> **用途**：Agent 英文版约法
> **内容与 wen 版平行**

#### 9.4.3 `prompt/covenant/zh/covenant.md`（现代中文版）

> **用途**：Agent 现代中文版约法

#### 9.4.4 `prompt/covenant/covenant_en.md`、`covenant_zh.md`、`covenant_wen.md`

> **用途**：各语言版本的单文件别名（与子目录版内容相同）

#### 9.4.5 `prompt/archive/`（历史版本）

> **用途**：旧版约法存档
> - `covenant_base.md`、`covenant_base_zh.md`、`covenant_base_lzh.md`

---

### 9.5 Preset 约法（Go 嵌入版）

> **路径**：`tui/internal/preset/covenant/{wen,zh,en}/covenant.md`
> **嵌入方式**：`preset.go` 中 `//go:embed all:covenant`
> **用途**：TUI 安装时复制到 `~/.lingtai-tui/covenant/` 供 Agent 引用

| 语言 | 路径 | 说明 |
|------|------|------|
| 文言 | `covenant/wen/covenant.md` | 正式版，约 108 行 |
| 中文 | `covenant/zh/covenant.md` | 现代中文版 |
| 英文 | `covenant/en/covenant.md` | English version |

---

### 9.6 Preset 原则文件

> **路径**：`tui/internal/preset/principle/{wen,zh,en}/principle.md`
> **嵌入方式**：`preset.go` 中 `//go:embed all:principle`
> **用途**：Agent 的原则（worldview），可在 `init.json` 中引用

---

### 9.7 Preset 灵悉流文件

> **路径**：`tui/internal/preset/soul/{wen,zh,en}/`（如有）
> **嵌入方式**：`preset.go` 中 `//go:embed all:soul`
> **用途**：Agent 灵悉流（soul flow）提示

---

### 9.8 Preset 迎客文件

> **路径**：`tui/internal/preset/greet/{wen,zh,en}/greet.md`
> **嵌入方式**：`preset.go` 中 `//go:embed all:greet`
> **用途**：Agent 醒来时的迎客语模板

**wen 版内容示例**（50行）：
```
[system] 有人方启此局。此时 {{time}}，其所在于 {{location}}。言 {{lang}}。汝心悉之隔 {{soul_delay}} 秒。
```
含模板变量：`{{time}}`、`{{location}}`、`{{lang}}`、`{{soul_delay}}`、`{{addr}}`

---

### 9.9 教程 Agent 提示语

> **路径**：`tui/internal/preset/tutorial.md`
> **嵌入方式**：`preset.go` 中 `//go:embed tutorial.md`
> **用途**：教程 Agent 的系统提示（365行超长文本）
> **关键内容**：

| 章节 | 行号范围 | 内容摘要 |
|------|----------|----------|
| 自述 | 1–13 | 教程目标、教学计划（12课）、首优先事项 |
| 课1 | 14–51 | 灵台是什么——架构和源码、daemon 探索练习 |
| 课2 | 53–60 | 全局目录 ~/.lingtai-tui/ |
| 课3 | 62–76 | 项目目录和器灵工作目录 |
| 课4 | 78–143 | Agent 如何诞生——init.json + lingtai-agent run |
| 课5 | 144–193 | TUI 如何包装 Runtime |
| 课6 | 195–204 | 身份——系统提示如何构建 |
| 课7 | 206–210 | 通信——邮件 |
| 课8 | 212–226 | 四 intrinsics（灵魂、系统、自冶、邮件） |
| 课9 | 228–262 | 能力——Avatar（皇冠明珠）、Daemon、File、Bash、灵悉、藏经阁等 |
| 课10 | 264–303 | TUI 命令、键盘快捷键、生命周期练习 |
| 课11 | 305–349 | Addons（IMAP、Telegram、飞书） |
| 课12 | 352–357 | 毕业 |
| 教学风格 | 359–365 | 温暖、鼓励、用真能力演示、不太冗长 |

> **重要**：教程 Agent 的 `covenant` 和 `principle` 由人类选择的语言决定，
> 教程 Agent 以该语言回复（文言称"菩提禅师"、英文称"Guide"）。

---

### 9.10 Preset Go 代码（preset.go，687行）

> **路径**：`tui/internal/preset/preset.go`
> **用途**：Preset 管理器的 Go 实现

| 行号 | 关键内容 | 用途 |
|------|----------|------|
| 16–38 | `//go:embed` 指令 | 嵌入 covenant/principle/soul/greet/templates/addons/skills |
| 40–45 | `Preset` 结构体 | 预设模板定义 |
| 47–60 | `PresetsDir()`、`List()` | 列出 ~/.lingtai-tui/presets/ |

---

## 十、i18n key 覆盖总结

### 10.1 跨仓库重复的 key

| key | kernel (wen) | kernel (en) | TUI (wen) | Portal (wen) | 用途差异 |
|------|--------------|--------------|-----------|--------------|----------|
| `insight.auto_question` | ✅ 49条中有 | ✅ | ✅ 287条中有 | ✅ | **TUI/Portal 中的为 UI 展示用；kernel 中的为 Agent-facing** |

### 10.2 i18n key 总数

| 文件 | 条目数 | 面向 |
|------|--------|------|
| `lingtai_kernel/i18n/{en,zh,wen}.json` | 各 49 条 | Agent 工具描述 |
| `lingtai/i18n/{en,zh,wen}.json` | 各 126 条 | Agent 工具描述（capabilities） |
| `tui/i18n/{en,zh,wen}.json` | 各 287 条 | TUI 界面文本 |
| `portal/i18n/{en,zh,wen}.json` | 各 322 条 | Portal 界面文本 |
| **合计** | **约 2,832 条** | |

---

*本清单由 prompt-architect 建立并扩展（2026-04-07）*
*扩展：加入 Go TUI 前端（lingtai 仓库）prompt 相关文件*

---

## 十一、Molt Warning Prompt 文件

> 独立 prompt 文件，从 hard-coded i18n 迁移而来（2026-04-08）

**位置**：`lingtai/prompt/molt/{lang}/`

**语言目录**：`en/`、`zh/`、`wen/`

| 文件 | 行数 | 内容 | 用途 |
|------|------|------|------|
| `warning_level1.md` | ~20 | 首次警告（轻度） | 温和提醒，可选行动 |
| `warning_level2.md` | ~25 | 中度警告 | 推荐行动，含 psyche 建议 |
| `warning_level3.md` | ~50 | 紧急警告（最后机会） | 强制行动，含 psyche 三件套 |
| `wiped.md` | ~25 | 上下文已清除 | 恢复指引 |

**每语言 4 个文件，共 12 个文件**。

**分级说明**：
- Level 1（首次）：`warnings = 1`，建议保存
- Level 2（2 至 N-1）：`warnings = 2~N-1`，推荐保存
- Level 3（最后机会）：`warnings = N`，含灵台/记忆/藏经阁三件套
- Wiped（已清除）：`warnings > N`，恢复指引

**加载逻辑**：`base_agent.py` 的 `_load_molt_prompt()` 方法优先从 prompt 文件加载，fallback 到 i18n。
