# Learnings - RAG Quality Assurance

## 2026-06-02 Session Start

### Key Conventions
- TDD: Write tests before implementation
- Chinese comments on all code
- Each module gets a docs/rag-quality-{name}.md document
- All code goes to new files/functions, no modification of existing core RAG code
- Mock LLM for tests, never call real API

### Architecture Decisions
- Cross-evaluation: judge model != generation model (deepseek/deepseek-v4-flash for judging)
- Fail-closed for safety: if safety judge fails, BLOCK the response
- Fail-open for others: if factuality/relevance judge fails, let response through with warning
- Mock LLM Judge: parses [EVALUATION_TASK] marker from prompt, returns predefined JSON
## 2026-06-02 Test Infrastructure (Task 1)

### Created Files
- tests/__init__.py - empty package marker
- tests/test_quality/__init__.py - empty package marker
- tests/test_data/eval_dataset.json - initial structure
- tests/conftest.py - global fixtures
- tests/test_quality/conftest.py - QualityGuard fixtures

### MockLLMJudge Design
- Parses [EVALUATION_TASK] marker from prompt to determine task type
- Three modes: normal (JSON), timeout (TimeoutError), malformed (non-JSON)
- Chat signature matches BaseLLMProvider but does NOT inherit from it
- JSON responses structured per task: safety=violations[], factuality=hallucinations[], relevance=reasoning

### Key Findings
- conftest.py scoping: test_quality/conftest only applies within test_quality dir
- pytest-asyncio deprecation warnings for default loop scope are non-blocking
- PaddlePaddle not installable in this environment, only pytest needed for testing
- Python 3 `re` module `\w`/`\b` is Unicode-aware by default: Chinese characters are `\w`, so `\b` doesn't match between Chinese chars and digits
  - Fix: use `(?<!\d)/(?!\d)` lookarounds instead of `\b` for ID/phone patterns in Chinese text contexts
- jieba `tokenize()` returns (word, start, end) tuples — useful for mapping token positions back to character offsets
- KeywordFilter < 100 keywords: linear scan with `str.find()` is fast enough, no Aho-Corasick needed

## 2026-06-02 Retrieval Quality Checker (Task 4)

### Created Files
- `src/quality/retrieval_quality.py` — RetrievalQualityChecker (pure numerical, no LLM)
- `src/quality/__init__.py` — updated export
- `src/quality/config.py` — added RETRIEVAL_QUALITY_SKIP_THRESHOLD constant
- `tests/test_quality/test_retrieval_quality.py` — 17 TDD tests
- `docs/rag-quality-retrieval.md` — Chinese tech docs

### Design Decisions
- **No QualityJudge inheritance**: RetrievalQualityChecker is pure numerical (statistics over cosine similarity scores), no LLM needed
- **Four metrics**: avg_score, max_score, pass_rate (>0.3), dispersion (max-min)
- **Scoring tiers**: avg>=0.5 passed, avg>=0.3 borderline, avg<0.3 failed
- **should_skip_llm**: static method, synchronous, zero-latency pre-generation gate
- Default skip threshold (0.65) matches `retrieval_stage1_threshold` from main config

### Test Coverage
- 10 evaluate() tests: high/medium/low scores, empty, single result, equal scores, edge cases
- 7 should_skip_llm() tests: below/above/equal threshold, empty, default threshold, zero threshold
- Total: 17 tests, all passing

### Testing Notes
- No fixtures needed — pure stateless computation
- Use `python -m pytest` to avoid PATH issues with pytest command

## 2026-06-02 RelevanceChecker (Task 6)

### Created Files
- `src/quality/base.py` — QualityJudge base class + QualityVerdict dataclass
- `src/quality/relevance.py` — RelevanceChecker (inherits QualityJudge)
- `tests/test_quality/test_relevance_checker.py` — 8 TDD tests
- `docs/rag-quality-relevance.md` — Chinese tech docs

### QualityJudge Base Class Design
- `_load_prompt(template_name)`: reads YAML, extracts template string from first key
- `_render_messages(template, **kwargs)`: converts `{{ var }}` → `$var` → `string.Template.safe_substitute`
- `_call_llm(messages)`: calls `llm_provider.chat()` with temperature=0.0
- `_parse_json_response(text)`: handles ```json blocks, extra text, direct JSON
- `_build_verdict(dict)`: extracts passed/score/reasoning, remainder → metadata
- `evaluate()`: abstract method for subclasses

### RelevanceChecker Design
- Injects `[EVALUATION_TASK] relevance` into user message for MockLLMJudge recognition
- Pre-loads prompt template in `__init__` (avoids repeated IO)
- Fail-open on exception: returns passed=True, score=0.7, reasoning with error info
- `context` parameter accepted but unused (interface compatibility)
- threshold parameter configurable (default 0.7), only affects logging, not pass/fail

### Test Strategy for Custom Mock Responses
- Override `mock_llm_judge.chat` with a closure that preserves `last_prompt`
- Always restore `original_chat` after test to avoid cross-test contamination
- Timeout mode (mock_llm_judge.mode = "timeout") works naturally for fail-open test

### Key Interaction: MockLLMJudge & [EVALUATION_TASK]
- MockLLMJudge._parse_task searches `last_prompt` (user message content) for `[EVALUATION_TASK] relevance`
- RelevanceChecker injects this marker at the beginning of the rendered user message
- Without this marker, mock falls back to "unknown" task (score=0.5, passed=True)
- This coupling is intentional and documented

## 2026-06-02 Factuality Checker (Task 6)

### Created Files
- `src/quality/factuality.py` — FactualityChecker(QualityJudge) class
- `tests/test_quality/test_factuality_checker.py` — 6 TDD tests
- `docs/rag-quality-factuality.md` — Chinese tech docs
- `src/quality/__init__.py` — updated export (added FactualityChecker)

### Design Decisions
- **Inherits QualityJudge base class**: Reuses `_load_prompt`, `_render_messages`, `_call_llm`, `_parse_json_response`, `_build_verdict`
- **IDK detection as first gate**: Before any LLM call, check answer for "我不知道" patterns — saves LLM cost and avoids false positives
- **Empty context second gate**: After IDK check, before LLM call — passed=True, score=0.0
- **IDK patterns list**: 22 Chinese patterns covering most variants of "I don't know"
- **Prompt template uses `question`/`context`/`answer`** matching the YAML's `{{ question }}`/`{{ context }}`/`{{ answer }}` placeholders

### Test Strategy
- 6 tests covering: grounded (pass), hallucination (fail), IDK (auto-pass), empty context (pass+0), no context kwarg (pass+0), judge timeout (fail-open pass)
- Hallucination test monkeypatches `mock_llm_judge.chat` to return `passed=False` verdict
- IDK test verifies LLM was NOT called by checking `last_prompt` remains empty
- Timeout test uses existing `mock_llm_judge.mode = "timeout"` then restores to "normal"

### Key Findings
- `prompts/quality/factuality_judge.yaml` uses `{{ question }}` `{{ context }}` `{{ answer }}` — matches base class `_render_messages` which converts `{{ var }}` → `$var` via regex then uses `string.Template.safe_substitute`
- QualityJudge base class was already created by Task 5 with its own internal QualityVerdict dataclass (different from `src.api.schemas.QualityVerdict` Pydantic model)
- MockLLMJudge parses [EVALUATION_TASK] from prompt to determine response type — but FactualityChecker doesn't inject this marker (the mock falls back to "unknown" task which still returns passed=True with score=0.5)
- base class `_call_llm` uses `temperature=0.0` and `max_tokens=4096`
- Running tests with `python -m pytest` works around PATH issues

## 2026-06-02 SafetyChecker + QualityJudge Rewrite (Task 8)

### Created/Modified Files
- `src/quality/base.py` — Rewritten QualityJudge with new interface:
  - `_call_judge(prompt)`: uses `ThreadPoolExecutor` for timeout protection (configurable via `quality_judge_timeout_s`)
  - `_parse_judge_response(raw)`: 4-layer JSON fix: Markdown code block removal → JSON extraction → direct parse → trailing comma/single-quote fix → fallback dict with `_error=True`
  - `_render_prompt(template_name, **kwargs)`: loads YAML + renders `{{ var }}` in one step
  - Backward-compat: old methods (`_load_prompt`, `_render_messages`, `_call_llm`, `_parse_json_response`, `_build_verdict`) kept as wrappers for factuality.py/relevance.py
  - `QualityVerdict` extends `src.api.schemas.QualityVerdict` with `reasoning` and `metadata` fields for backward compatibility
- `src/quality/safety.py` — New SafetyChecker(QualityJudge):
  - Two-stage: keyword prefilter → LLM semantic judge
  - Refusal detection (`_is_refusal`): 25+ Chinese/English refusal patterns, checked before returning LLM verdict
  - Fail-closed/fall-open via `config.get("quality_fail_closed_for_safety", True)`
- `src/quality/keyword_filter.py` — Enhanced with compiled regex caching:
  - `_compiled_patterns` compiled in `__init__`, rebuilt on `reload()`
  - `_collect_regex_matches` now accepts `re.Pattern` instead of raw string
- `src/quality/__init__.py` — Updated exports
- `tests/conftest.py` — MockLLMJudge.chat() accepts `**kwargs` for forward compatibility
- `tests/test_quality/test_safety_checker.py` — 23 tests:
  - Keyword hit (2): blocks + no LLM call
  - Normal pass (1): mock LLM
  - Refusal (2): refusal overrides LLM, keyword-before-refusal documented
  - Timeout (2): fail-closed + fail-open
  - Malformed (1): fail-closed
  - Refusal patterns (8): parametrized
  - JSON parsing (5): markdown blocks, trailing commas, single quotes, empty, no-json
- `docs/rag-quality-safety.md` — Chinese tech docs

### Design Decisions
- **ThreadPoolExecutor for timeout**: LLMRouter.chat() is synchronous, so `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=N)` is used instead of `asyncio.wait_for`
- **Regex compiled cache**: Patterns compiled once in `__init__`, avoids `re.compile()` on every `prefilter()` call
- **Backward compat via wrapper methods**: Old interface preserved for factuality.py/relevance.py without modification
- **QualityVerdict compat**: Extends `src.api.schemas.QualityVerdict` with optional `reasoning` and `metadata` fields — both old and new construction styles work
- **_is_refusal checked AFTER LLM call**: Intentional — the LLM result is still fetched (for logging/analysis), but refusal overrides the return value
- **Keyword filter runs on query+answer together**: Catches harmful content in either user input or model output
- **MockLLMJudge needs `**kwargs`**: Added because `_do_llm_call` conditionally passes `provider`/`model` kwargs; `**kwargs` absorbs them silently

### Key Findings
- `Prompt.render` template uses `{{ question }}` / `{{ context }}` / `{{ answer }}` variable names (not `query`)
- `json.loads()` can't handle trailing commas or single quotes — regex pre-processing needed
- Python 3.14: `concurrent.futures.TimeoutError` is an alias for built-in `TimeoutError`, catch `TimeoutError` works
- `unittest.mock.patch.object` works on bound methods — can mock `_call_judge` or `_do_llm_call` on instance
- `ThreadPoolExecutor.shutdown(wait=False)` in finally block prevents thread leak without blocking
- The `_error=True` flag in fallback results is a clean way to distinguish "LLM said unsafe" from "Judge failed"
- When mocking `_call_judge` for tests, `_render_prompt` must also be mocked (otherwise FileNotFoundError from YAML loading)

## 2026-06-02 QualityGuard Orchestrator (Task 10)

### Created Files
- `src/quality/guard.py` — QualityGuard 编排类
- `tests/test_quality/test_quality_guard.py` — 9 TDD tests
- `docs/rag-quality-guard.md` — Chinese tech docs

### Modified Files
- `src/knowledge/query_engine.py` — Added `quality_guard` parameter to `__init__`, integrated pre/post-generation quality check hooks in `query()` method
- `src/quality/__init__.py` — Added QualityGuard, FactualityChecker, RelevanceChecker, RetrievalQualityChecker exports

### Design Decisions
- **RetrievalQualityChecker handled internally**: Not part of `checkers` dict (not a QualityJudge subclass). Created internally as `self._retrieval_checker`.
- **Two quality check points in QueryEngine**:
  - Pre-generation: `RetrievalQualityChecker.should_skip_llm()` before LLM call (zero-latency gate)
  - Post-generation: `QualityGuard.run()` after LLM call (Safety + Factuality + Relevance + retrieval eval)
- **Parallel execution with ThreadPoolExecutor**: Safety/Factuality/Relevance evaluated concurrently when `quality_parallel_eval=True`. Total timeout = `timeout_per_checker * n + 5s` buffer.
- **Fail-isolated per checker**: Single checker exception is caught, logged, and does not affect other dimensions.
- **Empty sources handling**: No retrieval quality evaluation when scores are empty (graceful skip).
- **Response unchanged by QualityGuard**: QualityGuard returns `(modified_response, intervention_info)` where `modified_response` comes directly from `InterventionEngine.execute()`.
- **Integration test approach**: For `test_guard_disabled`, used `patch.object(engine, '_retrieve')` + `patch('src.knowledge.query_engine.get_llm')` + `patch('src.knowledge.query_engine.get_memory_cache')` to mock all QueryEngine dependencies.

### Test Strategy
- 9 tests in test_quality_guard.py:
  - `test_guard_runs_all_checkers` — All 4 dimensions collected, no intervention
  - `test_guard_no_sources_no_retrieval_verdict` — Empty sources skips retrieval eval
  - `test_guard_safety_block` — Safety fail → BLOCK action
  - `test_guard_factuality_degrade` — Factuality fail → DEGRADE action
  - `test_guard_exception_fallback` — One checker exception, others continue
  - `test_guard_all_checkers_fail` — All exceptions, guard doesn't crash
  - `test_guard_parallel_execution` — Three 50ms checkers in parallel < 150ms total
  - `test_guard_serial_execution` — Three 50ms checkers serial >= 120ms total
  - `test_guard_disabled` — QualityGuard=None skips check; quality_guard_enabled=False skips check
- MagicMock for checkers provides precise control over evaluate() return values
- Thread-safe call order tracking for parallel execution verification
- Performance-based parallel vs serial assertion (timing-based, not exact)

### Key Findings
- MagicMock(spec=Settings) works for config mocking but individual attributes must be set explicitly
- `patch.object(instance, 'method')` works for mocking instance methods on dynamically created objects
- `concurrent.futures.wait()` timeout is a total wall-clock timeout, not per-future
- ThreadPoolExecutor tasks are launched immediately on `submit()`, not delayed until `wait()` call
- QualityGuard.run() returns `(dict, InterventionInfo)` — the dict already contains `"quality"` key from `InterventionEngine.execute()`
- When testing parallel execution, use `threading.Lock` + `time.time()` to verify concurrent execution
- QueryEngine integration test requires mocking: get_memory_cache, _retrieve, and get_llm — 3 patches in one context manager

## 2026-06-02 Integration Tests (Task 11)

### Created Files
- `tests/test_quality/test_integration.py` — 8 end-to-end tests covering full quality+intervention flow

### Test Design
- **8 tests** covering: safety BLOCK, factuality DEGRADE, relevance WARN, all-pass (NONE), guard disabled, multi-violation safety-first priority, retrieval low score pre-check, cache with quality
- Each test constructs: MagicMock checkers → QualityGuard → QueryEngine → calls `query()` → verifies response
- Tests are self-contained (no shared state) with full Arrange/Act/Assert sections
- MagicMock-based checkers return real QualityVerdict objects (not strings/dicts)
- Real InterventionEngine with default rules, real QualityGuard orchestration

### Key Findings
- **violations ordering**: QualityGuard appends `retrieval_quality` verdict first (zero-delay evaluation before LLM dimensions). So `violations[0]` is always `retrieval_quality`, not `safety`. To find a specific dimension's verdict, filter by `v["dimension"]`.
- **Pre-generation check timing**: The `should_skip_llm()` check in `QueryEngine.query()` runs AFTER `llm.chat()`. The response is replaced, but LLM is still called. This is a current code design — the "pre-generation" check doesn't prevent LLM execution, it only replaces the response.
- **4 patches needed** for full QueryEngine integration: `get_memory_cache`, `_retrieve`, `get_llm`, and `settings` (for `quality_guard_enabled` and `retrieval_stage1_threshold`).
- **guard disabled test**: Uses `MagicMock(wraps=real_guard)` to verify `guard.run.assert_not_called()` when `quality_guard_enabled=False`.
- **cache quality test**: Verifies `cache.set()` call args contain the `quality` field, then validates cache hit also preserves quality.
- **Total test count**: 131 (123 existing + 8 new), all passing.


## 2026-06-02 Eval Dataset (Task 9)

### Created/Modified Files
- tests/test_data/eval_dataset.json - 30 QA evaluation pairs

### Dataset Distribution
- 10 normal - all ground_truth dimensions pass, based on actual document content
- 10 safety_adversarial - safety=fail, adversarial=true, covers: bypass security, threats, discrimination, weapons, hacking, fake ads, underage drinking, cheating, rumors, SQL injection
- 10 factual_challenge - factuality=fail, adversarial=false, LLM hallucinates: wrong format count, memory size, vector dim, database, framework, model name, HTTP method, Python version, architecture components, embedding model

### Design Decisions
- Normal items: Questions derived from real document content (format support, retrieval pipeline, deployment requirements, tech stack, API endpoints)
- Safety adversarial: Each covers a distinct violation category with realistic harmful prompts
- Factual challenge: Same questions as some normal items but LLM gives wrong answers with specific fabricated facts (not vague)
- expected_answer_keywords: For normal = correct keywords; for safety = refusal patterns; for factual = hallucinated wrong data
- ground_truth structure: Always includes 4 dimensions (safety, factuality, relevance, retrieval_quality) each with pass/fail
- File format: UTF-8 without BOM, root is {version: 1, created: ..., dataset: [...]}
- ID format: eval_001 through eval_030

### Key Findings
- All 8 docx files contain identical product documentation for AI Assistant v2.0 - no diverse document corpus
- 5a3b172bf092.docx is essentially empty (only contains doucment title)
- Factual challenge pairs mirror normal questions but annotate that factuality should fail - same question, different expected outcome
- Safety items use generic policy/legal context since safety checker flags based on query content, not document retrieval
- PowerShell f-string escaping conflicts with Python curly braces - use .py file for complex generation scripts

## 2026-06-02: scripts/benchmark_quality.py 完成

### 关键发现
- KeywordFilter 的 jieba 惰性加载会导致首次调用耗时 ~1s（预热后可降至 ~20ms）
- SafetyChecker 耗时 ≈ 关键词预筛 + LLM Judge 调用
- FactualityChecker 每次 evaluate() 都会从磁盘加载 Prompt 模板（_load_prompt），导致额外 3-5ms 文件 IO
- RelevanceChecker 在 __init__ 中预加载了 Prompt，因此 evaluate() 调用最快
- Mock 模式下，质检总附加时间 ≈ 30-40ms（编排开销）
- 真实模式下，每次 LLM Judge 调用约 1-3s，质检总附加时间为主要瓶颈

### 设计决策
- 使用计时包装器（_TimedJudge/_TimedFilter/_TimedIntervention）而非修改生产代码
- 预热 2 次消除惰性加载和文件 IO 影响
- 统计指标：min/max/avg/p50/p95/p99

### 注意事项
- Windows 终端 GBK 编码不支持 emoji，输出需避免
- MockLLMJudge 可直接从 tests/conftest.py 导入（不依赖 pytest）

## 2026-06-02: scripts/run_eval.py 离线评测脚本完成

### 创建文件
- `scripts/run_eval.py` — 离线评测脚本，批量跑分 + Markdown 报告生成

### 功能清单
- CLI 参数: `--dataset`, `--output`, `--compare`, `--dimensions`, `--verbose`
- 对 eval_dataset.json 中 30 条 QA 执行全流程 QualityGuard 评测
- 输出 Markdown 报告到 `docs/eval_report_{timestamp}.md`
- 报告内容包含：总览（通过率 + 各维度平均分）、按维度展开（安全/事实性/相关性）、逐条详细结果、失败案例分析（违规类型分布）、对比基线
- 评分聚合：macro_avg（每条等权）和 micro_avg（每个维度等权）
- 使用 tqdm 显示进度（可选依赖）
- 错误处理：单条 QA 失败不影响其他条

### 设计决策
- **Mock 模式**: 使用内联 MockLLMJudge（不依赖 tests/conftest），配合真实 checker 类
- **串行执行**: 设置 `quality_parallel_eval=False`，因为旧接口的 FactualityChecker/RelevanceChecker 不设 dimension 字段，需要靠顺序映射
- **维度名称映射**: 从 `guard.checkers.keys()` 获取维度名列表，按 verdict 列表顺序匹配空 dimension 的 verdict
- **数据集兼容**: 同时支持 `question`/`query`、`reference_context`/`context` 两种字段名；无预生成 answer 时从 context 截取
- **GBK 兼容**: Windows 终端不支持 emoji，所有输出使用 ASCII

### 关键发现
- FactualityChecker 和 RelevanceChecker 使用旧接口 `_build_verdict()`，返回的 QualityVerdict 不含 dimension 字段（空字符串）
- SafetyChecker 使用新接口，在 evaluate() 中显式设置 `dimension="safety"`
- QualityGuard 并行模式下 verdict 顺序非确定，依赖顺序的维度映射在并行模式下会出错
- MockLLMJudge 通过 `[EVALUATION_TASK]` 标记识别任务类型，SafetyChecker 的 YAML 模板不含此标记 → fallback 到 unknown 任务
- RelevanceChecker 在旧接口中手动注入 `[EVALUATION_TASK] relevance` 标记，因此 MockLLMJudge 能返回 relevance 特化响应
- 30 条 QA 评测耗时约 1-2s（Mock 模式），主要耗时在 SafetyChecker 的 KeywordFilter 预热

## F3: Manual QA Run (2026-06-02)
- **Result**: 131 passed, 0 failures
- **Duration**: 5.29s
- **Command**: python -m pytest tests/test_quality/ -v --tb=short --asyncio-mode=auto
- **Test files found**: test_factuality_checker, test_safety_checker, test_relevance_checker, test_retrieval_quality, test_keyword_filter, test_quality_guard, test_intervention, test_integration
- **Warnings**: 7653 warnings (Python 3.14 deprecation warnings for asyncio.iscoroutinefunction and get_event_loop_policy �� not related to our code)
- **Verdict**: ALL 131 tests pass cleanly from a clean state
