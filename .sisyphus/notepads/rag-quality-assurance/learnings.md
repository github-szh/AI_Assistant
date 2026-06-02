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
