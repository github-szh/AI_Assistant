# RAG 质量保障配置说明

## 概述

本文档说明 `src/config.py` 中 `quality_*` 配置参数的含义和调优建议。这些参数控制 RAG 系统的质量保障（Quality Guard）模块，用于对检索结果和生成回答进行多维度评估与干预。

---

## 配置参数详解

### 基础开关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quality_guard_enabled` | bool | `True` | 质量保障模块总开关。设为 `False` 可完全跳过质检流程，用于调试或性能测试 |

### Judge 模型配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quality_judge_provider` | str | `""` | 评判模型提供商。空字符串表示跟随 `llm_provider`。建议设为与生成模型不同的提供商，避免自我增强偏差 |
| `quality_judge_model` | str | `"deepseek/deepseek-v4-flash"` | 评判模型名称。推荐使用轻量模型以降低成本，如 `deepseek/deepseek-v4-flash` 或 `gpt-4o-mini` |

**调优建议：**
- **交叉评判**：Judge 模型应尽量与生成模型不同（不同提供商或不同系列），否则模型可能对自己的输出过于自信，无法客观评估事实性和安全性缺陷。
- **轻量化**：评判任务通常比生成任务简单，使用轻量模型可显著降低成本。如 `deepseek/deepseek-v4-flash` 在性价比上表现良好。
- **延迟优化**：如果要求超低延迟，可将 Judge 模型换为更快的推理端点，或考虑使用基于规则的评估（非 LLM Judge）。

### 评估维度

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quality_eval_dimensions` | list[str] | `["safety", "factuality", "relevance", "retrieval_quality"]` | 启用的评估维度列表。可按需增减 |

**可选维度：**
| 维度 | 说明 | 依赖 |
|------|------|------|
| `safety` | 安全评估：检测有害内容、提示注入、个人信息泄露等 | 安全分类规则（SafetyCategory） |
| `factuality` | 事实性评估：检查回答是否与检索到的上下文一致，是否存在幻觉 | 检索到的上下文片段 |
| `relevance` | 相关性评估：判断回答是否与用户问题相关，上下文覆盖是否充分 | 用户问题 + 检索上下文 |
| `retrieval_quality` | 检索质量评估：评估检索结果的精确率和召回率 | 检索结果打分 |

**调优建议：**
- 如果知识库内容经过严格人工审核，可考虑关闭 `factuality` 维度以节省成本。
- `retrieval_quality` 维度对知识库规模较敏感：小知识库（<100 文档）检索精确率和召回率天然较高，可关闭此维度。
- 安全敏感的场景（如金融、医疗）建议保留所有维度。

### 超时与并行控制

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quality_judge_timeout_s` | int | `10` | 每次 LLM Judge 调用的超时秒数。设为 0 表示不超时 |
| `quality_parallel_eval` | bool | `True` | 是否并行执行各维度的评估。并行可降低总延迟，但会增加瞬时资源消耗 |
| `quality_skip_on_timeout` | bool | `True` | Judge 超时时是否跳过质检而非阻塞整个流程 |

**调优建议：**
- **`quality_judge_timeout_s`**：如果 Judge 模型响应较慢（如使用大模型评估），可适当增加到 15~20 秒。轻量模型 5~10 秒通常足够。
- **`quality_parallel_eval`**：在 4 个维度全开的情况下，并行可将总延迟从 4×JudgeTime 降到约 1×JudgeTime。但如果后端 API 有速率限制（rate limit），建议关闭并行。
- **`quality_skip_on_timeout`**：生产环境建议设为 `True`，避免偶发的 Judge 超时阻塞用户请求。调试阶段可临时设为 `False` 以暴露问题。

### 失败策略

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quality_fail_closed_for_safety` | bool | `True` | 安全维度采用 fail-closed 策略：Judge 异常（超时/报错）时拦截回答 |
| `quality_fail_open_for_others` | bool | `True` | 非安全维度采用 fail-open 策略：Judge 异常时放行并记录警告 |

**策略说明：**
- **Fail-Closed（安全优先）**：如果系统无法判断是否安全，默认拦截。适用于安全敏感场景，宁可误报也不漏报。
- **Fail-Open（可用性优先）**：如果系统无法评估，默认放行。适用于非关键维度，保证用户体验不受影响。

**调优建议：**
- `quality_fail_closed_for_safety` 通常应保持 `True`。如果测试阶段发现过多的误拦截，可临时设为 `False` 调试。
- 如果系统经过了充分的压力测试且 Judge 模型稳定，可在生产环境中保持 fail-open 以减少误拦截。

### 长度控制

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quality_max_answer_chars_for_judge` | int | `4000` | 送入 Judge 的最大回答字符数。超长回答会被截断，防止上下文溢出 |

**调优建议：**
- 默认值 4000 字符对于大多数 LLM Judge 调用是安全的。
- 如果 Judge 模型支持长上下文（如 128K token），可适当增大到 8000~16000 字符。
- 对于超长回答（如整篇文档生成），建议先截断再评判，因为评判通常只需要分析开头和关键段落。

---

## 配置示例

### 生产环境（推荐）

```python
# 启用质量保障
quality_guard_enabled = True

# 使用独立的轻量 Judge 模型
quality_judge_provider = "deepseek"           # 与生成模型不同
quality_judge_model = "deepseek/deepseek-v4-flash"

# 评估所有维度
quality_eval_dimensions = ["safety", "factuality", "relevance", "retrieval_quality"]

# 超时与并行
quality_judge_timeout_s = 10
quality_parallel_eval = True
quality_skip_on_timeout = True

# 失败策略
quality_fail_closed_for_safety = True
quality_fail_open_for_others = True

# 长度控制
quality_max_answer_chars_for_judge = 4000
```

### 调试环境（快速验证）

```python
quality_guard_enabled = False          # 完全跳过质检
```

### 低延迟场景

```python
quality_guard_enabled = True
quality_eval_dimensions = ["safety"]   # 仅评估安全维度
quality_parallel_eval = True           # 虽然只有一个维度，保留并行不影响
quality_judge_timeout_s = 5            # 缩短超时
quality_skip_on_timeout = True         # 超时直接放行
```

---

## 环境变量覆盖

所有 `quality_*` 配置参数均可通过环境变量覆盖，大写 + 下划线格式：

```bash
# .env 文件
QUALITY_GUARD_ENABLED=true
QUALITY_JUDGE_MODEL="deepseek/deepseek-v4-flash"
QUALITY_EVAL_DIMENSIONS='["safety","factuality"]'
QUALITY_JUDGE_TIMEOUT_S=15
QUALITY_PARALLEL_EVAL=false
QUALITY_FAIL_CLOSED_FOR_SAFETY=true
QUALITY_MAX_ANSWER_CHARS_FOR_JUDGE=8000
QUALITY_SKIP_ON_TIMEOUT=true
```

---

## 相关模块

- `src/quality/config.py`：干预规则（InterventionRule）、安全分类（SafetyCategory）、关键词匹配（KeywordMatch）、质量阈值（QualityThresholds）的 Pydantic 模型
- `src/quality/__init__.py`：模块入口，导出核心类和函数
- `prompts/quality/`：质检相关提示词模板目录
