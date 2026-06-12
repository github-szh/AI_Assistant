# Oh-My-OpenAgent Agent 使用指南

> OpenCode/Sunshine.Coder 中 oh-my-openagent 插件提供的 AI Agent 说明

---

## Sisyphus vs Prometheus

| | Sisyphus | Prometheus |
|---|---|---|
| **角色** | 包工头 + 施工队 | 只出方案（不写代码） |
| **工作方式** | 接到任务 → 拆解 → 分派 → 调其他 Agent → 检查 → 迭代 | 接到任务 → 访谈你 → 追问边界 → 输出计划 → 停，等你确认 |
| **输出** | 可运行的代码 + 改完的文件 | 一份结构化的执行计划 |
| **写代码** | ✅ 自己和子 Agent 都写 | ❌ 不写，只规划 |
| **停的条件** | 任务完成或你喊停 | 计划写好了就停，等你批复 |

### 用法

```
"我有个想法但没想清楚"     → Prometheus（先聊明白）
"我知道要干什么了"         → Sisyphus（直接干）
"复杂需求，想看计划再动手"  → Prometheus → 你审核 → Sisyphus/Atlas
```

---

## Agent 分级推荐

### 第一梯队：日常必用

| Agent | 一句话 | 什么时候用 |
|---|---|---|
| **Sisyphus** | 总指挥，自动拆任务 → 分派 → 检验 → 不完成不停止 | **大部分时候就选它**，说需求就行 |
| **Atlas** | 纯执行者，拿着计划按步骤干 | 你已经有明确计划，直接让它干活 |

### 第二梯队：特定场景利器

| Agent | 一句话 | 什么时候用 |
|---|---|---|
| **Prometheus** | 先访谈你再动手，彻底搞懂需求 | 需求模糊时先用它聊清楚，产出一份计划再交给 Atlas |
| **Explore** | 只读扫描，不写代码 | 快速了解陌生代码库 |
| **Hephaestus** | 全栈自主开发，自己探索自己做 | 给一个目标让它端到端完成 |

### 第三梯队：辅助位

| Agent | 一句话 | 什么时候用 |
|---|---|---|
| **Librarian** | 精确查代码和文档 | 查找调用链、追踪逻辑 |
| **Momus** | 挑刺专家 | Code Review、上线前检查 |
| **Metis** | 复杂架构推理 | 技术选型、方案对比 |

### 可忽略

| Agent | 原因 |
|---|---|
| **Oracle** | 和 Metis 定位重叠，后者更强 |
| **Multimodal-looker** | 看图用，需要多模态模型支持 |

---

## 推荐使用流程

```
需求模糊 → Prometheus（访谈，出计划）
              ↓
           Sisyphus（总指挥，自动调度 Atlas/Hephaestus/Explore）
              ↓
需求清楚 → Atlas（直接按步骤执行）

代码审查 → Momus
查代码   → Librarian / Explore
架构决策 → Metis
```

> 90% 的情况直接选 **Sisyphus**，它内部会自动调其他 Agent。需要把控节奏时才手动切 **Atlas**。

---

## 需求讨论 vs 架构设计

| 场景 | Agent | 说明 |
|---|---|---|
| **需求讨论** | **Prometheus** | 访谈模式，反问澄清，产出结构化计划 |
| **架构设计** | **Metis** | 技术选型、设计模式、系统架构决策 |

---

## 当前 Agent 配置

所有 Agent 和 Category 统一使用 `sunshine-coder/ssc-chat-latest` 模型。

配置文件：`~/.config/opencode/oh-my-openagent.json`

---

## 故障排查：Sunshine.Coder 中 Agent 不显示

### 现象

重启 sunny 后，Agent 选择器中只显示 `Build`、`Plan`、`Hephaestus`、`Atlas`，其余 Agent（Sisyphus、Prometheus、Oracle 等）全部消失。

### 根因

oh-my-openagent 每个 Agent 在源码中硬编码了 **模型回退链（fallbackChain）**，要求特定的 AI 提供商才能激活：

```
Sisyphus   → 需要 anthropic/claude-opus-4-7 或 openai/gpt-5.4 或 opencode/kimi-k2.5
Prometheus → 需要 anthropic/claude-opus-4-7 或 openai/gpt-5.4
Oracle     → 需要 openai/gpt-5.4 或 google/gemini-3.1-pro
...
Hephaestus → 需要 openai/gpt-5.4（sunshine-coder 的 @ai-sdk/openai-compatible 被识别为 openai）
Atlas      → 同上
```

**而 sunny.exe 是精简版，只内置了 `@ai-sdk/openai-compatible` 一个通用适配器：**

```
npm 安装的 opencode-ai (完整版):
  插座 ├─ opencode    → 连 opencode.ai，提供 gpt-5-nano, big-pickle, kimi-k2.5...
  插座 ├─ anthropic   → 连 Anthropic，提供 claude-opus-4-7, claude-sonnet-4-6...
  插座 ├─ openai      → 连 OpenAI，提供 gpt-5.4, gpt-5-nano...
  插座 └─ (自定义)    → sunshine-coder, deepseek...

sunny.exe (精简版):
  插座 └─ @ai-sdk/openai-compatible → 一个万能转接头
           ├─ sunshine-coder
           └─ deepseek
```

Sunshine.Coder 是企业内网定制版，为了简洁和安全裁剪掉了需要外网 API key 的提供商。

### 解决方案

在 `oh-my-openagent.json` 中：

1. **添加 `agent_definitions`** 数组，显式声明所有 Agent 名称，绕过模型回退检查
2. **补上缺失的 `sisyphus`** Agent 配置

```jsonc
{
  "$schema": "...",
  "agent_definitions": [
    "sisyphus",
    "hephaestus",
    "oracle",
    "librarian",
    "explore",
    "multimodal-looker",
    "prometheus",
    "metis",
    "momus",
    "atlas",
    "sisyphus-junior"
  ],
  "agents": {
    "sisyphus": {
      "model": "sunshine-coder/ssc-chat-latest"
    },
    ...
  }
}
```

### 教训

`oh-my-openagent.json` 中的 `model` 字段只是默认值，Agent 能否激活取决于**源码中的 fallbackChain 能否匹配到可用提供商**。`agent_definitions` 可以强制注册 Agent 绕过此限制。
