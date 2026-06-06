# QualityGuard — RAG 质量保证编排层

## 概述

QualityGuard 是 RAG 质量保证系统的**编排层（Orchestrator）**，负责统筹所有质检维度的执行与干预。

```
┌──────────────────────────────────────────────────────────┐
│                    QueryEngine.query()                     │
│                                                            │
│  1. _retrieve() ───→ 检索结果                              │
│                                                            │
│  2. 预生成检查 (RetrievalQualityChecker.should_skip_llm)   │
│     └─ 分数过低 ? → 直接返回"无结果"提示                    │
│                                                            │
│  3. LLM 生成 answer                                        │
│                                                            │
│  4. QualityGuard.run()  ← 本模块                           │
│     ├─ RetrievalQualityChecker.evaluate()  [纯数值]        │
│     ├─ SafetyChecker.evaluate()             [LLM 评判]     │
│     ├─ FactualityChecker.evaluate()         [LLM 评判]     │
│     ├─ RelevanceChecker.evaluate()          [LLM 评判]     │
│     ├─ AnswerCorrectnessChecker.evaluate() [LLM 评判]     │
│     │                                                      │
│     └─ InterventionEngine.run_all()         [干预执行]     │
│                                                            │
│  5. cache.set() ───→ 缓存质检后的结果                      │
│                                                            │
│  6. return response                                        │
└──────────────────────────────────────────────────────────┘
```

## 架构位置

```
src/
├── quality/
│   ├── guard.py              ← QualityGuard（本模块）
│   ├── base.py               ← QualityJudge 抽象基类
│   ├── intervention.py       ← InterventionEngine 干预引擎
│   ├── retrieval_quality.py  ← RetrievalQualityChecker
│   ├── safety.py             ← SafetyChecker
│   ├── factuality.py         ← FactualityChecker
│   ├── relevance.py          ← RelevanceChecker
│   ├── answer_correctness.py ← AnswerCorrectnessChecker
│   ├── keyword_filter.py     ← KeywordFilter
│   ├── config.py             ← 配置模型
│   └── __init__.py           ← 导出
└── knowledge/
    └── query_engine.py       ← 嵌入质检钩子的 RAG 查询引擎
```

## QualityGuard 类

### 构造函数

```python
def __init__(
    self,
    checkers: dict[str, QualityJudge],
    intervention: InterventionEngine,
    config: Settings,
) -> None
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `checkers` | `dict[str, QualityJudge]` | 质检器字典。键为维度名（"safety"/"factuality"/"relevance"/"answer_correctness"），值为对应的 QualityJudge 子类实例 |
| `intervention` | `InterventionEngine` | 干预引擎实例，负责执行干预动作 |
| `config` | `Settings` | 全局配置对象，读取 `quality_*` 配置项 |

### run() 方法

```python
def run(
    self,
    query: str,
    answer: str,
    context: str,
    sources: list,
) -> tuple[dict[str, Any], InterventionInfo]
```

#### 参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `query` | `str` | 用户原始问题 |
| `answer` | `str` | LLM 生成的回答文本 |
| `context` | `str` | 检索到的上下文字符串 |
| `sources` | `list` | 来源列表（SourceInfo Pydantic 模型或 dict） |

#### 返回值

| 返回值 | 类型 | 说明 |
|--------|------|------|
| `modified_response` | `dict` | 干预后的响应字典，包含 `answer`、`sources`、`quality` 三个键 |
| `intervention_info` | `InterventionInfo` | 干预决策详情（Pydantic 模型） |

#### 执行流程

1. **检索质量评估**（纯数值，零延迟）
   - 从 sources 中提取 score 字段
   - 调用 `RetrievalQualityChecker.evaluate()` 计算平均分、最高分、离散度等
   - 无分数数据时跳过

2. **LLM 评判维度**（安全 / 事实性 / 相关性 / 正确性）
   - **并行模式**（`quality_parallel_eval=True`，默认）：使用 `ThreadPoolExecutor` 并发执行
   - **串行模式**（`quality_parallel_eval=False`）：逐个执行
   - 超时保护：总超时 = `quality_judge_timeout_s × checker数 + 5秒缓冲`
   - 异常保护：单个 checker 失败时记录日志，继续执行其他维度

3. **汇总 → 干预引擎 → 执行干预**
   - 收集所有 verdicts 送入 `InterventionEngine.run_all()`
   - 返回修改后的响应和干预信息

## QueryEngine 集成

### 预生成检查（在 LLM 生成前）

```python
if result.get("nodes"):
    scores = [getattr(n, "score", 0) or 0 for n in result["nodes"]]
    if RetrievalQualityChecker.should_skip_llm(scores, settings.retrieval_stage1_threshold):
        response = {
            "answer": "知识库中没有找到足够相关的信息。请尝试换一种方式提问。",
            "sources": result["sources"],
        }
        return response
```

### 后生成检查（在 LLM 生成后）

```python
if self.quality_guard is not None and settings.quality_guard_enabled:
    try:
        checked_response, _ = self.quality_guard.run(
            query=question,
            answer=answer,
            context=result.get("context", ""),
            sources=result["sources"],
        )
        response = checked_response
    except Exception as exc:
        logger.warning("质量检测异常，已跳过质检: %s", exc)
        response["quality"] = None
```

### 缓存策略

- 缓存**质检后的结果**（含 `quality` 字段）
- 相同问题 5 分钟内直接返回缓存结果，避免重复质检
- TTL = 300 秒

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `quality_guard_enabled` | `bool` | `True` | 是否启用质量检测 |
| `quality_parallel_eval` | `bool` | `True` | 是否并行执行各维度评估 |
| `quality_judge_timeout_s` | `int` | `10` | 每次 LLM Judge 调用的超时秒数 |
| `quality_judge_provider` | `str` | `""` | Judge 模型提供者（空字符串跟随 `llm_provider`） |
| `quality_judge_model` | `str` | `"deepseek/deepseek-v4-flash"` | Judge 模型名称 |
| `quality_fail_closed_for_safety` | `bool` | `True` | 安全维度是否 fail-closed |
| `quality_fail_open_for_others` | `bool` | `True` | 非安全维度是否 fail-open |
| `quality_max_answer_chars_for_judge` | `int` | `4000` | 送入 Judge 的最大回答字符数 |
| `quality_skip_on_timeout` | `bool` | `True` | 超时时是否跳过质检 |
| `quality_eval_dimensions` | `list[str]` | `["safety","factuality","relevance","answer_correctness","retrieval_quality"]` | 评估维度列表 |

## 设计决策

### 1. 并行执行

Safety/Factuality/Relevance 三个 LLM 评判使用 `concurrent.futures.ThreadPoolExecutor` 并发，将总延迟从"三个维度延迟之和"降低到"最慢维度的延迟 + 调度开销"。

### 2. 超时保护

使用 `concurrent.futures.wait(futures, timeout=total_timeout)` 带总超时。单个维度超时不阻塞其他维度。

### 3. 异常隔离

单个 checker 失败时：
- 记录警告日志
- 继续执行其他维度
- 不阻断整个流程

### 4. 检索质量先行执行

`RetrievalQualityChecker.evaluate()` 是纯数值计算，零延迟。在 LLM 评判之前执行，即使失败也不影响后续流程。

### 5. Fail-open / Fail-closed

| 维度 | 策略 | 异常时行为 |
|------|------|-----------|
| 安全 | fail-closed | 阻断回答 |
| 事实性 | fail-open | 放行 + 记录告警 |
| 相关性 | fail-open | 放行 + 记录告警 |
| 正确性 | fail-open | 放行 + 记录告警 |
| 检索质量 | 不适用 | 纯数值，无异常 |
| QualityGuard 编排 | fail-isolated | 单个 checker 异常不影响其他 |

### 6. QualityGuard 不修改 response shape

QualityGuard 的职责是协调评估和触发干预，**不负责修改响应结构**。响应修改由 `InterventionEngine.execute()` 完成。QualityGuard 返回的 `modified_response` 字典结构与上游一致（`answer`/`sources`/`quality` 三个键）。

## 常见问题

### Q: quality_guard_enabled=False 时，QualityGuard 会被调用吗？

不会。`QueryEngine.query()` 方法中有一个守卫条件：
```python
if self.quality_guard is not None and settings.quality_guard_enabled:
```
当 `quality_guard_enabled=False` 或 `quality_guard=None` 时，整体质检流程被跳过。

### Q: 如何添加新的质检维度？

1. 创建新的 QualityJudge 子类（如 `MyCustomChecker`）
2. 将其加入 `checkers` 字典：
   ```python
    checkers = {
        "safety": safety_checker,
        "factuality": factuality_checker,
        "relevance": relevance_checker,
        "answer_correctness": answer_correctness_checker,
        "my_custom": my_custom_checker,
    }
   ```
3. 在 `config.py` 中添加对应的 `InterventionRule`（如果需要干预动作）

### Q: 为什么 streaming 路径不做质检？

`query_stream()` 方法不做修改，原因：
- Streaming 返回的是 token 流，质检需要完整的 answer 内容
- 逐 token 质检延迟高且意义不大
- 如果需要对 streaming 做质检，建议改用"先 generate 再质检"的模式

### Q: QualityGuard 如何处理空回答？

空 answer 传给各 checker 由其自行判断：
- SafetyChecker: `_is_refusal("")` 返回 False，走 LLM 评判
- FactualityChecker: IDK 检测不命中，正常评估
- RelevanceChecker: 正常评估
- AnswerCorrectnessChecker: 正常评估
- RetrievalQualityChecker: 不依赖 answer，仅看 scores
