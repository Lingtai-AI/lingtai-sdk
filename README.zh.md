# lingtai-kernel 灵台内核

> [English](README.md) | [中文](README.zh.md) | [文言](README.wen.md)

> *灵台者有持，而不知其所持，而不可持者也。*
> — 庄子·庚桑楚

最小智能体内核 — 思考、通信、手记、承载工具。

## 设计哲学

**灵台，心也。** 在庄子笔下，灵台是意识栖居之所：自然地承载灵魂所需的一切，却不自知其所承载，也无法被刻意掌控。

在本框架中，智能体的灵台就是它的**工作目录**——磁盘上的一个文件夹，手记、盟约、身份、信箱都在其中。目录即智能体。给内核一个文件夹和一个 LLM 服务，它便赋予智能体生命。拿走文件夹，智能体便不复存在。内核承载智能体的一切，但不解读其内容——正如庄子的灵台，有持而不知其所持。

本内核遵循 Unix 设计哲学：

- **一切皆文件。** 智能体的身份就是其目录路径。没有抽象 ID——路径即地址、即锁、即真实。
- **内核只定义协议，不定义实现。** `LLMService` 和 `ChatSession` 是抽象接口。如何实现——适配器、API 密钥、速率限制——是调用者的事。
- **每个智能体都是独立进程。** 独立的目录、独立的 LLM 服务、独立的信箱、独立的日志。智能体之间通过文件系统信件通信，而非共享内存。
- **内核是最小的。** 思考（LLM）、通信（信件）、手记（手记）、承载工具。能力、文件读写、编排——这些在 [lingtai](https://github.com/user/lingtai) 中。

## 安装

```bash
pip install lingtai-kernel
```

## 内核所含

| 组件 | 用途 |
|------|------|
| **BaseAgent** | 内核调度器——生命周期、消息循环、工具派发 |
| **四种内置工具** | mail（进程间通信）、system（生命周期）、eigen（手记/身份）、soul（内心声音） |
| **LLM 协议** | `LLMService` 抽象基类、`ChatSession` 抽象基类、供应商无关类型 |
| **服务** | 文件系统信件传输、JSONL 结构化日志 |
| **WorkingDir** | 目录管理——锁、git、清单 |

## 内核不含

能力、文件读写、MCP、视觉、搜索、bash、化身、LLM 适配器、速率限制——这些在 `lingtai` 中。

## 快速开始

```python
from lingtai_kernel import BaseAgent

# 调用者提供 LLM 服务（抽象基类的任意实现）
agent = BaseAgent(
    service=my_llm_service,
    working_dir="/agents/alice",    # 灵台——灵魂栖居之所
    agent_name="alice",             # 可选的显示名称
)

agent.add_tool("hello", schema={...}, handler=lambda args: {"msg": "hi"})
agent.start()
agent.send("Say hello")
agent.stop()
```

内核接收一个目录路径和一个服务，不关心它们如何创建。

## 灵台之结构

```
/agents/alice/              ← 此路径即智能体
  .agent.lock               ← 独占锁（每个目录只能运行一个进程）
  .agent.heartbeat          ← 存活证明（定期更新）
  .agent.json               ← 清单（名称、地址、配置）
  system/
    covenant.md             ← 受保护的指令（盟约）
    pad.md                  ← 工作笔记（手记）
  mailbox/
    inbox/                  ← 收到的信件
    outbox/                 ← 待发送
    sent/                   ← 发送记录
  logs/
    events.jsonl            ← 结构化事件日志
```

无需 `agent_id`。路径即身份。心跳证明存活。锁证明独占。

## 许可

MIT
