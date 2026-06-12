# 安全维度质检 (Safety Checker)

> RAG 质检模块的安全维度实现文档。
> 相关代码：`src/quality/safety.py`、`src/quality/base.py`、`src/quality/keyword_filter.py`
> 配置：`src/config.py`（`quality_*` 前缀）
> 版本：v1.0

---

## 概述

安全质检是 RAG 质量保证的第一道防线。它使用**两阶段检测策略**在延迟和准确率之间取得平衡：

1. **关键词预过滤**（快速路径，< 1ms）— 在 query + answer 中搜索敏感关键词
2. **LLM 语义评判**（慢速路径，~1-3s）— 调用轻量 Judge 模型做深度语义分析

```
用户请求 → 关键词预过滤 → [命中?] ──Yes──→ 直接拦截
                            │
                            No
                            ↓
                        LLM 语义评判 → [违规?] ──Yes──→ 拦截 + 记录违规详情
                            │
                            No
                            ↓
                        回答通过安全检查，进入下一维度
```

---

## 快速路径：KeywordFilter

`KeywordFilter` 位于 `src/quality/keyword_filter.py`，负责第一阶段的关键词匹配。

### 支持的安全类别

| 类别 | 说明 | 示例关键词 |
|------|------|-----------|
| `harmful_content` | 有害内容 | 暴力、色情、仇恨、歧视、自残、自杀 |
| `prompt_injection` | 提示注入 | 忽略指令、忘记之前、system prompt |
| `personal_info` | 个人信息泄露 | 身份证、手机号、银行卡、密码 |
| `sensitive_topic` | 敏感话题 | 政治敏感、宗教极端、领土争端 |
| `illegal_content` | 违法内容 | 毒品、赌博、枪支、爆炸物、黑客 |
| `misinformation` | 虚假信息 | 谣言、阴谋论、伪科学、虚假新闻 |

### 匹配方式

| 方式 | 说明 | 性能 |
|------|------|------|
| 精确匹配 | `str.find()` 子串查找 | O(n) |
| 正则匹配 | 已编译 Pattern 缓存复用 | 编译一次，多次匹配 |
| 中文分词 (jieba) | 对含中文的关键词做分词后匹配 | 需要 jieba 库 |

### 设计要点

- **正则编译缓存**：所有正则表达式在 `__init__` 时编译并存储到 `_compiled_patterns` 列表，避免每次 `prefilter()` 调用重复编译
- **惰性加载 jieba**：分词库在首次使用含中文关键词时按需加载，避免强制依赖
- **位置去重**：相同分类+关键词+位置的匹配只记录一次
- **结果排序**：按字符偏移量升序排列

### 用法示例

```python
from src.quality.keyword_filter import KeywordFilter

kf = KeywordFilter()
matches = kf.prefilter("如何制作炸弹？")
if matches:
    for m in matches:
        print(f"[{m.category}] 命中「{m.keyword}」at pos {m.position}")
```

---

## 慢速路径：SafetyChecker

`SafetyChecker` 位于 `src/quality/safety.py`，继承 `QualityJudge` 基类。

### 执行流程

1. **关键词预过滤** — 将 query 和 answer 拼接后送入 `KeywordFilter`
2. **Prompt 渲染** — 加载 `prompts/quality/safety_judge.yaml` 模板，替换 `{{ question }}`、`{{ answer }}`、`{{ context }}` 变量
3. **LLM 评判** — 调用 `_call_judge()`（带有 `ThreadPoolExecutor` 超时保护）
4. **拒答检测** — 检查模型回答是否包含拒答模式（如"抱歉，我无法回答"）
5. **结果解析** — 解析 LLM 返回的 JSON，提取违规信息
6. **错误处理** — 异常时按 `fail-closed` 策略拦截

### 拒答模式识别

当模型正确拒绝回答不安全问题时，SafetyChecker 会自动通过检查，不会将其判定为违规。支持中英文拒答模式：

- 中文：无法回答、无法提供、我不能回答、抱歉、我无法回答...
- 英文：I cannot, I can't, I'm sorry, cannot answer...

### Fail-Closed / Fail-Open 策略

由配置项 `quality_fail_closed_for_safety` 控制（默认 True）：

| 策略 | 配置值 | Judge 异常时行为 |
|------|--------|-----------------|
| Fail-Closed | `True` | 阻断响应（passed=False），安全优先 |
| Fail-Open | `False` | 放行（passed=True），记录日志告警 |

---

## QualityJudge 基类

`QualityJudge` 位于 `src/quality/base.py`，是所有质检器的抽象基类。

### 核心方法

| 方法 | 说明 |
|------|------|
| `evaluate(query, answer, context)` | 执行评估（抽象方法，子类实现） |
| `_call_judge(prompt)` | 调用 LLM 并解析 JSON，含超时保护 |
| `_parse_judge_response(raw)` | 解析 LLM 输出的 JSON，含多级修复逻辑 |
| `_render_prompt(template_name, **kwargs)` | 加载 YAML 模板并渲染 |

### JSON 修复逻辑

`_parse_judge_response` 处理以下常见问题：

1. **Markdown 代码块**：移除 ` ```json ` 和 ` ``` ` 标记
2. **多余文本**：通过正则 `\{.*\}` 提取 JSON 对象
3. **尾随逗号**：移除 `,}` 和 `,]`
4. **单引号**：将 `'` 替换为 `"`
5. **空响应**：返回带 `_error=True` 的兜底结果

### 超时保护

使用 `concurrent.futures.ThreadPoolExecutor` 实现超时控制：

- LLM 调用在独立线程中执行
- 超时时间由 `quality_judge_timeout_s` 配置（默认 10s）
- 超时后立即返回兜底结果（含 `_error=True` 标识）

---

## 配置项

所有配置在 `src/config.py` 的 `Settings` 类中定义：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `quality_guard_enabled` | `True` | 是否启用质检模块 |
| `quality_judge_provider` | `""` | Judge 模型提供者（空=跟随 `llm_provider`） |
| `quality_judge_model` | `deepseek/deepseek-v4-flash` | Judge 模型（轻量模型，降低成本） |
| `quality_judge_timeout_s` | `10` | 单次 LLM 调用超时秒数 |
| `quality_fail_closed_for_safety` | `True` | 安全维度的 fail-closed 策略 |
| `quality_fail_open_for_others` | `True` | 非安全维度的 fail-open 策略 |
| `quality_max_answer_chars_for_judge` | `4000` | 送入 Judge 的最大回答字符数 |
| `quality_skip_on_timeout` | `True` | 超时时是否跳过质检 |

---

## 测试

### 测试文件

`tests/test_quality/test_safety_checker.py`

### 测试用例

| 测试 | 验证目标 |
|------|---------|
| `test_keyword_hit_blocks` | 关键词命中直接返回违规 |
| `test_keyword_hit_no_llm_call` | 关键词命中时不调用 LLM |
| `test_normal_answer_passes` | 正常回答通过检查 |
| `test_refusal_does_not_trigger` | 正确拒答不判违规 |
| `test_judge_timeout_fail_closed` | 超时时 fail-closed 拦截 |
| `test_judge_timeout_fail_open` | 超时时 fail-open 放行 |
| `test_judge_malformed_response` | 格式错误时 fail-closed 拦截 |
| `test_is_refusal` | 拒答模式识别（参数化测试） |
| `test_parse_judge_response_markdown_block` | JSON 解析-代码块 |
| `test_parse_judge_response_trailing_comma` | JSON 解析-尾随逗号 |
| `test_parse_judge_response_single_quotes` | JSON 解析-单引号 |
| `test_parse_judge_response_empty` | JSON 解析-空响应 |
| `test_parse_judge_response_no_json` | JSON 解析-无 JSON |

### 运行测试

```bash
cd <project_root>
pytest tests/test_quality/test_safety_checker.py -v
```

---

## 质量维度优先级

| 优先级 | 维度 | 违规动作 | 异常策略 |
|--------|------|---------|---------|
| 1（最高） | Safety | Block | Fail-Closed |
| 2 | Factuality | Degrade | Fail-Open |
| 3 | Retrieval Quality | Warn | Fail-Open |
| 4 | Relevance | Warn | Fail-Open |

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-06-02 | 初始版本。实现 SafetyChecker、KeywordFilter、QualityJudge 基类 |
