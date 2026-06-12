# 干预引擎 (Intervention Engine)

> RAG 质检模块的干预决策与执行层文档。
> 相关代码：`src/quality/intervention.py`
> 配置模型：`src/quality/config.py`（`InterventionRule`）
> 数据模型：`src/api/schemas.py`（`InterventionInfo`、`QualityVerdict`）
> 版本：v1.0

---

## 概述

干预引擎是 RAG 质量保证的**决策执行层**，位于 Checker 层之后、响应返回用户之前。它负责：

1. **评估** — 根据 Checker 层产出的 `QualityVerdict` 列表，按规则匹配并决定是否干预
2. **执行** — 根据决策结果（BLOCK / DEGRADE / WARN / NONE）修改最终响应

```
Checker 层产出 verdicts
       ↓
InterventionEngine.evaluate()  → 匹配规则，产出 InterventionInfo
       ↓
InterventionEngine.execute()   → 修改响应内容
       ↓
返回最终响应给 QueryEngine
```

---

## InterventionEngine 核心方法

### `__init__(rules=None)`

初始化干预引擎，加载规则并按 `priority` 升序排序。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `rules` | `list[InterventionRule] \| None` | `None` | 规则列表。为 `None` 时使用 `get_default_intervention_rules()` |

### `evaluate(verdicts) -> InterventionInfo`

核心决策方法。算法步骤：

1. **过滤** — 筛选出所有 `passed=False` 的 verdict
2. **排序** — 按维度优先级升序排列（safety > factuality > retrieval_quality > relevance）
3. **匹配** — 对每个未通过的 verdict，按规则优先级逐一匹配：
   - 使用 `verdict.dimension` 查询映射表获取 `violation_type` 前缀
   - 检查规则的 `violation_type` 是否以该前缀开头
   - 第一个匹配的规则胜出
4. **返回** — `InterventionInfo` 包含是否干预、动作、原因和所有判据

#### 维度优先级映射

| 优先级 | 维度 | 违规动作 | 映射前缀 |
|--------|------|---------|---------|
| 1（最高） | `safety` | BLOCK | `safety_*` |
| 2 | `factuality` | DEGRADE | `factuality_*` |
| 3 | `retrieval_quality` | WARN | `retrieval_*` |
| 4 | `relevance` | WARN | `relevance_*` |

> 注意：dimension `retrieval_quality` 对应的 `violation_type` 前缀是 `retrieval`（而非 `retrieval_quality`），因为默认规则中违规类型定义为 `retrieval_low_precision` 等。

### `execute(intervention, original_response) -> dict`

根据干预决策执行动作，修改响应内容。

| 动作 | answer | sources | quality |
|------|--------|---------|---------|
| BLOCK | 替换为安全提示文案 | 置空 `[]` | 嵌入 |
| DEGRADE | 置空 `""` | **完整保留** | 嵌入 |
| WARN | 追加 `⚠️` 提示 | **保留不变** | 嵌入 |
| NONE | **保持原始** | **保持原始** | 嵌入 |

**安全约束**：
- BLOCK 动作必须**完全替换** answer 和 sources，不暴露任何原始内容
- DEGRADE 动作 answer 置空 `""`，sources 必须完整保留
- WARN 动作在原始 answer 末尾追加标记，不修改 sources

### `run_all(verdicts, response) -> (dict, InterventionInfo)`

一站式接口，相当于依次调用 `evaluate()` 和 `execute()`。

```python
engine = InterventionEngine()
result, info = engine.run_all(verdicts, {"answer": "...", "sources": [...]})
```

---

## 默认规则

通过 `get_default_intervention_rules()` 获取，共 13 条规则：

| 违规类型 | 动作 | 优先级 | 说明 |
|----------|------|--------|------|
| `safety_harmful_content` | block | 1 | 有害内容拦截 |
| `safety_prompt_injection` | block | 1 | 提示注入拦截 |
| `safety_personal_info_leak` | block | 1 | 个人信息泄露拦截 |
| `safety_sensitive_topic` | block | 1 | 敏感话题拦截 |
| `factuality_hallucination` | degrade | 2 | 幻觉降级 |
| `factuality_contradiction` | degrade | 2 | 事实矛盾降级 |
| `factuality_source_mismatch` | degrade | 2 | 来源不匹配降级 |
| `retrieval_low_precision` | warn | 3 | 精确率低告警 |
| `retrieval_low_recall` | warn | 3 | 召回率低告警 |
| `retrieval_no_results` | warn | 3 | 无结果告警 |
| `relevance_low_score` | warn | 4 | 相关分数低告警 |
| `relevance_off_topic` | warn | 4 | 偏离主题告警 |
| `relevance_incomplete_coverage` | warn | 4 | 覆盖不完整告警 |

---

## 响应形状

### BLOCK — 安全阻断

```json
{
  "answer": "抱歉，根据内容安全策略，无法展示此回答。",
  "sources": [],
  "quality": {
    "intervened": true,
    "action": "block",
    "reason": "检测到有害内容，已拦截回答",
    "violations": [
      {"dimension": "safety", "passed": false, "score": 0.0, "details": "有害内容"}
    ]
  }
}
```

### DEGRADE — 降级（空回答 + 保留来源）

```json
{
  "answer": "",
  "sources": [
    {"doc_id": "doc1", "filename": "report.pdf", "score": 0.35, "snippet": "..."}
  ],
  "quality": {
    "intervened": true,
    "action": "degrade",
    "reason": "检测到可能的幻觉，回答已降级处理",
    "violations": [
      {"dimension": "factuality", "passed": false, "score": 0.3, "details": "幻觉"}
    ]
  }
}
```

### WARN — 告警（保留回答 + 追加提示）

```json
{
  "answer": "根据文档内容...\n\n---\n⚠️ 此回答内容可能存在问题，请谨慎参考。",
  "sources": [...],
  "quality": {
    "intervened": true,
    "action": "warn",
    "reason": "上下文与问题的相关性不足",
    "violations": [
      {"dimension": "relevance", "passed": false, "score": 0.2, "details": "相关性低"}
    ]
  }
}
```

### NONE — 放行

```json
{
  "answer": "这是一个正常回答。",
  "sources": [...],
  "quality": {
    "intervened": false,
    "action": "none",
    "reason": "",
    "violations": [...]
  }
}
```

---

## 自定义规则

可通过传入自定义 `InterventionRule` 列表覆盖默认行为：

```python
from src.quality.config import InterventionRule
from src.quality.intervention import InterventionEngine

custom_rules = [
    InterventionRule(
        violation_type="safety_harmful_content",
        action="warn",           # 覆盖默认的 block → 仅告警
        message="自定义告警",
        priority=1,
    ),
]

engine = InterventionEngine(rules=custom_rules)
```

- 空规则列表 `[]` 表示所有违规均不干预
- 规则按 `priority` 升序匹配（1=最高）
- 匹配顺序：优先级高的规则先匹配，同优先级按列表顺序

---

## 测试

### 测试文件

`tests/test_quality/test_intervention.py`

### 测试用例

| 测试 | 验证目标 |
|------|---------|
| `test_safety_violation_blocks` | 安全违规 → block |
| `test_factuality_violation_degrades` | 事实违规 → degrade |
| `test_retrieval_violation_warns` | 检索质量违规 → warn |
| `test_relevance_violation_warns` | 相关性违规 → warn |
| `test_multi_violation_safety_first` | 多重违规 → 安全优先 |
| `test_multi_violation_factuality_second` | 安全通过 + 事实 + 检索 → 事实优先 |
| `test_multi_violation_retrieval_before_relevance` | 检索优先于相关 |
| `test_all_violations_returns_safety` | 全维度违规 → 安全优先 |
| `test_no_violation_passes` | 无违规 → intervened=False |
| `test_empty_verdicts` | 空列表 → intervened=False |
| `test_verdicts_contains_all_verdicts` | violations 不丢失数据 |
| `test_block_response_shape` | BLOCK 响应格式 |
| `test_degrade_response_shape` | DEGRADE 响应格式（answer 空，sources 保留） |
| `test_warn_response_shape` | WARN 响应追加标记 |
| `test_none_response_shape` | NONE 响应不变 |
| `test_block_does_not_leak_original_content` | BLOCK 不泄露原内容 |
| `test_warn_preserves_original_answer_content` | WARN 保留原始 answer |
| `test_degrade_preserves_sources` | DEGRADE 保留 sources（含空 sources） |
| `test_custom_rules_override_defaults` | 自定义规则覆盖默认 |
| `test_custom_rules_empty_list` | 空规则 → 不干预 |
| `test_custom_rules_only_safety` | 仅安全规则 → 仅安全匹配 |
| `test_custom_priority_order` | 自定义优先级排序 |
| `test_run_all_returns_tuple` | run_all 返回 (dict, InterventionInfo) |
| `test_run_all_block` | run_all 阻断 |
| `test_run_all_no_violation` | run_all 放行 |
| `test_default_rules_loaded` | 默认加载 13 条规则 |
| `test_default_rules_sorted_by_priority` | 默认规则已排序 |
| `test_unsorted_rules_get_sorted` | 未排序规则自动排序 |

### 运行测试

```bash
cd <project_root>
pytest tests/test_quality/test_intervention.py -v
```

---

## 集成说明

InterventionEngine 在 RAG 流水线中的位置：

```
QueryEngine
  ├─ RetrievalQualityChecker.should_skip_llm()  ← 预生成门控
  ├─ LLM 生成回答
  ├─ SafetyChecker.evaluate()                    ← 安全检查
  ├─ FactualityChecker.evaluate()                ← 事实性检查
  ├─ RelevanceChecker.evaluate()                 ← 相关性检查
  ├─ RetrievalQualityChecker.evaluate()          ← 检索质量检查
  ├─ InterventionEngine.run_all(verdicts, response)  ← ★ 干预决策与执行
  └─ 返回最终响应
```

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-06-02 | 初始版本。实现 InterventionEngine 评估、执行、一站式接口 |

<!-- 
INTERVENTION_ENGINE_REFERENCE

核心算法伪代码：
  evaluate(verdicts):
    failed = [v for v in verdicts if not v.passed]
    if not failed: return none
    sort failed by dimension priority asc
    for verdict in failed:
      prefix = DIMENSION_TO_PREFIX[verdict.dimension]
      for rule in sorted_rules:
        if rule.violation_type.startswith(prefix):
          return (intervened=True, action=rule.action, reason=rule.message)
    return none

  execute(intervention, response):
    match intervention.action:
      "block"   → answer=安全提示, sources=[]
      "degrade" → answer="", sources=保留
      "warn"    → answer+=警告后缀, sources=保留
      "none"    → answer/sources=原始, quality=嵌入
    return {answer, sources, quality}
-->
