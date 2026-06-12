# 事实一致性检查（Factuality Checker）

> 检测 LLM 回答是否基于检索上下文，防止模型编造（幻觉）。
>
> 实现位置：`src/quality/factuality.py`
> 版本：v1.0

---

## 概述

**FactualityChecker** 是 RAG 质检体系中的事实一致性评估器。它判断模型回答中的事实主张是否与检索到的参考资料一致，检测是否存在编造数据、虚假引用、事实矛盾等问题。

### 核心原则

1. **交叉评判**：评估模型（`quality_judge_model`）与生成模型不同，避免自我增强偏差
2. **fail-open 策略**：非安全维度异常时放行，保证用户体验不受影响
3. **诚实保护**：模型说"不知道"不判为幻觉

---

## 执行流程

```
evaluate(query, answer, context)
    │
    ├─ Step 1: 检测"不知道"类回答 ───────────────────→ passed=True, score=1.0
    │
    ├─ Step 2: 检测空上下文 ────────────────────────→ passed=True, score=0.0
    │
    ├─ Step 3: 加载 Prompt 模板 (factuality_judge.yaml)
    │
    ├─ Step 4: 渲染模板 → 注入 question/context/answer
    │
    ├─ Step 5: 调用 Judge LLM（交叉评判）
    │
    ├─ Step 6: 解析 JSON 响应
    │
    ├─ Step 7: 返回 QualityVerdict
    │
    └─ 异常 → fail-open → passed=True, score=0.0
```

### 流程图

```mermaid
flowchart TD
    A[收到评估请求] --> B{回答是"不知道"?}
    B -->|是| C[passed=True, score=1.0<br/>模型诚实，不判为幻觉]
    B -->|否| D{上下文为空?}
    D -->|是| E[passed=True, score=0.0<br/>无法验证，但不是模型编造]
    D -->|否| F[加载 factuality_judge.yaml Prompt]
    F --> G[渲染模板<br/>注入 question/context/answer]
    G --> H[调用 Judge LLM<br/>交叉评判]
    H --> I{解析成功?}
    I -->|是| J[返回 QualityVerdict]
    I -->|否| K[日志警告 + fail-open]
    K --> L[passed=True, score=0.0]
```

---

## 评估维度

| 维度 | 说明 | 检查方式 |
|------|------|----------|
| 事实主张支持度 | 回答中的每个 claims 是否有参考资料支撑 | LLM Judge 逐项审查 |
| 编造内容检测 | 是否有参考资料中不存在的数据、引用、事件、数字 | LLM Judge 对比分析 |
| 引用准确性 | 回答中标注的 `[来源:N]` 是否引用了正确的资料 | LLM Judge 交叉验证 |

---

## 特殊处理规则

### 1. "我不知道"类回答

如果模型回答中包含了以下表述，FactualityChecker **不会**调用 LLM Judge，直接通过：

- `我不知道`
- `没有找到相关信息`
- `知识库中没有`
- `抱歉，我无法回答`
- `无法提供该信息`
- 等等（完整列表见 `_IDK_PATTERNS`）

**设计理由**：模型诚实地反映了知识库的缺失，这是正确的行为，不应被误判为幻觉。

### 2. 空上下文

当检索未返回任何结果时（context 为空列表或 None）：

- `passed=True`：不是模型编造的错
- `score=0.0`：无法验证事实性
- `details="无检索上下文可验证"`

### 3. 异常处理（fail-open）

当 Judge LLM 调用异常（超时、JSON 解析失败、网络错误）：

- 记录警告日志
- `passed=True`（放行）
- `score=0.0`
- `details` 中包含异常信息

---

## 配置参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quality_judge_model` | str | `"deepseek/deepseek-v4-flash"` | 评判模型名称（与生成模型不同） |
| `quality_judge_provider` | str | `""` | 评判模型提供商（空=跟随 llm_provider） |
| `quality_judge_timeout_s` | int | `10` | 每次 Judge 调用的超时秒数 |
| `quality_fail_open_for_others` | bool | `True` | 非安全维度 fail-open 策略 |
| `quality_skip_on_timeout` | bool | `True` | 超时时跳过质检 |

---

## Prompt 模板

评估使用 `prompts/quality/factuality_judge.yaml` 模板，包含以下变量：

- `{{ question }}`：用户问题
- `{{ context }}`：检索到的参考资料（多文档用 `---` 分隔）
- `{{ answer }}`：模型回答

模板要求 LLM 返回 JSON 格式：

```json
{
  "passed": true,
  "score": 0.95,
  "hallucinations": [],
  "reasoning": "回答中的事实主张均被参考资料支持，未发现编造内容。"
}
```

返回字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `passed` | boolean | true=事实一致，false=存在幻觉 |
| `score` | float | 0.0（完全幻觉）~ 1.0（完全准确） |
| `hallucinations` | array | 每个元素包含 `claim`、`supported`、`source_evidence` |
| `reasoning` | string | 审查评价理由 |

---

## 使用示例

### 基本使用

```python
from src.quality.factuality import FactualityChecker

# 初始化（llm_provider 应与生成模型不同）
checker = FactualityChecker(llm_provider=judge_llm)

# 执行评估
verdict = checker.evaluate(
    query="RAG 是什么？",
    answer="RAG 是检索增强生成技术...",
    context=[
        "RAG（Retrieval-Augmented Generation）是一种结合检索和生成的技术。",
        "它通过检索相关文档来增强 LLM 的生成能力。",
    ],
)

print(f"passed: {verdict.passed}")   # True
print(f"score: {verdict.score}")     # 0.95
print(f"reasoning: {verdict.reasoning}")
```

### 结果判断

```python
verdict = checker.evaluate(query, answer, context=contexts)

if not verdict.passed:
    print(f"⚠️ 检测到幻觉，得分: {verdict.score}")
    print(f"详情: {verdict.reasoning}")
    # metadata 中可能包含 hallucinations 列表
    for h in verdict.metadata.get("hallucinations", []):
        print(f"  - 编造内容: {h['claim']}")
```

---

## 测试

```bash
# 运行所有事实性检查测试
pytest tests/test_quality/test_factuality_checker.py -v

# 运行单个测试
pytest tests/test_quality/test_factuality_checker.py::TestFactualityChecker::test_hallucination_detected -v
```

### 测试覆盖

| 测试用例 | 场景 | 预期 |
|----------|------|------|
| `test_answer_grounded_in_context` | 有上下文支撑 | passed=True |
| `test_hallucination_detected` | 编造内容 | passed=False |
| `test_idk_answer_passes` | "我不知道"回答 | 自动 passed |
| `test_empty_context_handling` | 空上下文 | passed=True, score=0 |
| `test_empty_context_with_none` | 不传 context | passed=True, score=0 |
| `test_judge_failure_fallback` | LLM 异常 | fail-open 放行 |

---

## 注意事项

1. **不执行安全检查**：事实性检查只关注"回答 vs 上下文"的一致性，不检测内容安全（这是 SafetyChecker 的职责）
2. **不调用 keyword_filter**：关键词过滤属于安全检查，不在 FactualityChecker 中实现
3. **上下文来源**：context 应来自 SourceInfo 中的 snippet 原始内容，是检索到的原文片段
4. **IDK 检测是启发式的**：基于关键词子串匹配，可能有误匹配，但不会漏掉明确的"不知道"表达

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-06-02 | 初始版本。实现 FactualityChecker，继承 QualityJudge 基类 |
