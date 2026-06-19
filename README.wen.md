# 灵台内核

> [English](README.md) | [中文](README.zh.md) | [文言](README.wen.md)

> *灵台者有持，而不知其所持，而不可持者也。*
> — 庄子·庚桑楚

器灵之最小内核 — 思、通、简、承器。

## 道

**灵台，心也。** 庄子言灵台，谓其自然持守灵魂所需之一切，不自知其所持，亦不可强持之。

此框架中，器灵之灵台即其**工作目录**——磁盘之上一隅，简牍、盟约、名号、书信皆寄于此。目录即器灵。予内核以一目录、一语言服务，器灵即生；去其目录，器灵即灭。内核承载器灵之一切而不解其意——有持而不知其所持。

内核循 Unix 之道：

- **万物皆文卷。** 器灵之身份即其目录路径。无抽象之号——路径即地址、即锁、即真。
- **内核定规矩，不定实现。** `LLMService` 与 `ChatSession` 皆抽象之约。何以实现——适配之器、密钥、流量之限——皆调用者之事。
- **一灵一进程。** 独立之目录、独立之语言服务、独立之书信、独立之日志。器灵之间以文件系统传书通信，非共享内存。
- **内核至简。** 思（LLM）、通（传书）、简（手简）、承器。能力、文卷读写、编排——皆在 [lingtai](https://github.com/user/lingtai) 中。

## 安装

```bash
pip install lingtai-kernel
```

## 内核所含

| 器 | 用 |
|------|------|
| **BaseAgent** | 内核之主——生灭、消息循环、器之派发 |
| **四固有之器** | 传书（通信）、观己（生灭）、核心自治（简/名号）、灵魂（内心之声） |
| **LLM 之约** | `LLMService` 抽象之约、`ChatSession` 抽象之约 |
| **服务** | 文件系统传书、JSONL 日志 |
| **工作目录** | 目录管理——锁、git、清单 |

## 内核不含

能力、文卷读写、MCP、观象、游历、bash、化身、LLM 适配之器、流量之限——皆在 `lingtai` 中。

## 速启

```python
from lingtai_kernel import BaseAgent

# 调用者供语言服务（抽象之约的任何实现）
agent = BaseAgent(
    service=my_llm_service,
    working_dir="/agents/alice",    # 灵台——灵魂所居
    agent_name="alice",             # 真名（可不设）
)

agent.add_tool("hello", schema={...}, handler=lambda args: {"msg": "hi"})
agent.start()
agent.send("Say hello")
agent.stop()
```

内核受一目录路径与一服务，不问其从何而来。

## 灵台之制

```
/agents/alice/              ← 此路径即器灵
  .agent.lock               ← 独占之锁
  .agent.heartbeat          ← 存活之证
  .agent.json               ← 清单
  system/
    covenant.md             ← 盟约
    pad.md                  ← 简
  mailbox/
    inbox/                  ← 所收之书信
    outbox/                 ← 待发之书信
    sent/                   ← 已发之记录
  logs/
    events.jsonl            ← 事件日志
```

无需 `agent_id`。路径即身份。心跳证存活。锁证独占。

## 许可

MIT
