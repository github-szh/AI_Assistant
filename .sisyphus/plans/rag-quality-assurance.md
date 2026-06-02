# RAG 实时质量保障体系

## TL;DR

> **Quick Summary**: 在现有 RAG 系统 (QueryEngine) 中嵌入实时质量检测层，每次 LLM 生成回答后自动执行 Safety/事实校验/相关性多维评估，并根据违规类型执行差异化干预（拦截/改写/警告/降级），同时提供离线评测套件用于追踪改进趋势。

> **Deliverables**:
> - `src/quality/` 模块：QualityJudge（安全检查器、事实校验器、相关性检查器）
> - `src/quality/intervention.py`：干预引擎（违规→动作映射与执行）
> - 质检 Prompt 模板（YAML）：安全评判、事实校验、相关性评判
> - 安全关键词预筛配置
> - `tests/test_quality/`：TDD 测试套件（含 Mock LLM Judge）
> - `scripts/run_eval.py`：离线评测脚本 + 报告生成
> - `tests/test_data/eval_dataset.json`：30 对 QA 评测数据集
> - Config 扩展：质检相关参数、干预规则、自定义安全类别

> **Estimated Effort**: **Large** (~80-120 任务小时)
> **Parallel Execution**: YES — 5 waves
> **Critical Path**: 测试基础设施 → QualityJudge 基类 → 安全检查器 → 干预引擎 → QueryEngine 集成 → 集成测试

---

## Context

### Original Request
为现有 RAG 系统构建评测体系，使 LLM 回答**更准确、更安全**——每次回答都经过质检拦截，并可根据违规类型执行差异化干预。

### Interview Summary
**Key Discussions**:
- 用户期望的是**实时质检系统**而非仅离线评测——每次 LLM 回答后立即检查
- 干预策略按违规类型分类配置：拦截/改写/警告/降级
- 安全检查含自定义维度：关键词预筛 + LLM 语义深度判断
- 评测集 20-30 对，LLM 辅助生成 + 人工校验
- 接入层面：仅后端 API 层（QueryEngine 内部），不修改前端
- 技术路径：LLM-as-judge（用同一个 LLM 自评判）

**Metis Review (2026-06-02)**:
- 发现关键问题：**Streaming 路径不兼容**后置质检、**缓存交互**（是否缓存已质检结果）、**Schema 变更**需定义干预信号字段
- 重要建议：Fail-closed vs fail-open 分维度策略（安全：fail-closed，其他：fail-open）
- 推荐实现顺序：**安全维度优先** → 事实校验 → 相关性 → 离线评测最后
- 边界锁定：不碰前端、不碰 Streaming、不做评测 Dashboard

---

## Work Objectives

### Core Objective
在现有 RAG QueryEngine 中嵌入**实时质量保障层**，每次 LLM 生成后执行多维评估，根据违规类型执行配置化干预，并提供离线评测能力。

### Concrete Deliverables
- `src/quality/` 模块（QualityJudge 基类 + SafetyChecker + FactualityChecker + RelevanceChecker）
- `src/quality/intervention.py`（干预引擎）
- `src/quality/config.py`（质检配置，关键词列表，自定义安全类别）
- `prompts/quality/`（评判用 Prompt 模板）
- `tests/test_quality/`（TDD 测试套件）
- `tests/test_data/eval_dataset.json`（30 对评测数据集）
- `scripts/run_eval.py`（离线评测脚本）
- `src/api/schemas.py` 扩展（intervention 信号字段）
- `src/knowledge/query_engine.py` 集成（QualityGuard 钩子）

### Definition of Done
- [ ] 质检层在每次 `query()` 调用中自动执行，拦截失败回答
- [ ] 安全、事实、相关性三维度均可独立检测
- [ ] 4 种干预策略（拦截/改写/警告/降级）按规则正确执行
- [ ] 自定义安全类别（关键词 + 语义）可配置生效
- [ ] 离线评测脚本可运行，输出结构化报告
- [ ] 测试套件覆盖全部核心逻辑

### Must Have
- LLM-as-judge 多维评估：安全、事实校验、相关性、检索质量
- 干预引擎按违规类型差异化执行
- 关键词预筛 + LLM 语义深度判断两级安全检测
- 自定义安全类别支持（YAML 配置）
- 离线评测脚本 + 30 对评测数据集
- TDD 全覆盖（Mock LLM Judge）
- **代码中文注释**: 每一行代码必须有中文注释，解释其作用
- **技术文档**: 每个模块完成后，生成对应的中文文档（`docs/rag-quality-{module}.md`）解释使用技术的原因与原理
- **更新优化清单**: 所有代码完成后，更新 `docs/RAG优化清单.md`，维持原文件格式

### Must NOT Have (Guardrails)
- **不修改前端**——纯后端改动
- **不处理 Streaming 路径**——`query_stream()` 不受影响，文档中标明
- **不做评测 Dashboard**——报告为静态输出
- **不做自定义类别管理 UI**——仅 YAML/Config 配置
- **不改动现有检索/Reranker/Ingestion 代码**
- **不给已有代码补测试**——TDD 仅适用于新代码

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: NO（项目无测试目录和测试框架）
- **Automated tests**: **TDD** — 先写测试后实现
- **Framework**: `pytest` + `pytest-asyncio`（已有依赖）
- **Test infrastructure to create**: `tests/conftest.py`, `tests/test_quality/`, Mock LLM provider for judge, test fixtures

### QA Policy
EVERY task MUST include agent-executed QA scenarios.
- **Library/Module**: pytest + Bash — 运行 `pytest tests/test_quality/`，验证通过率
- **API 集成**: curl — 验证 `POST /query` 的干预行为
- **脚本**: Bash — 运行 `python scripts/run_eval.py`，验证输出报告

### 代码质量要求（MANDATORY）
1. **中文注释**: 每个文件的每段逻辑必须有中文注释，说明"这段代码在做什么"和"为什么这么做"
2. **技术文档**: 每个模块完成时，创建 `docs/rag-quality-{module}.md`，包含：
   - 技术选型原因（为什么用 LLM-as-judge 而不是规则？为什么关键词+语义两级？）
   - 实现原理（流程图 + 关键算法说明）
   - 配置说明（有哪些参数可调，各自影响什么）
3. **优化清单更新**: 全部任务完成后，更新 `docs/RAG优化清单.md` 追加本次改动记录，维持原表格和标记格式

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Foundation — schema + config + test infra):
├── Task 1: 测试基础设施（conftest.py + Mock Judge + fixtures）
├── Task 2: Config 扩展（质检参数 + 干预规则 + 自定义类别 schema）
├── Task 3: API Schema 扩展（QueryResponse 干预信号字段）
├── Task 4: 质检查询 Prompt 模板（安全/事实/相关性 YAML）

Wave 2 (Core Quality Guards — MAX PARALLEL):
├── Task 5: QualityJudge 基类 + SafetyChecker（含关键词预筛）
├── Task 6: FactualityChecker（LLM 事实一致性校验）
├── Task 7: RelevanceChecker（LLM 回答相关性校验）
├── Task 8: 安全关键词预筛模块（可配置关键词列表 + 类别映射）

Wave 3 (Intervention Engine — depends on Wave 2):
├── Task 9: InterventionEngine（违规→动作映射 + 执行 + 响应改写）
├── Task 10: 质检与 QueryEngine 集成（query() 中嵌入 QualityGuard 钩子）
├── Task 11: 集成测试（端到端：质检+干预全流程）

Wave 4 (Offline Evaluation — depends on Wave 2&3):
├── Task 12: 评测数据集生成（30 对 QA，LLM 辅助 + 人工结构确认）
├── Task 13: 离线评测脚本 run_eval.py（批量跑分 + Markdown 报告）
├── Task 14: Latency 基准测试脚本

Wave FINAL (Verification — parallel reviews):
├── Task F1: Plan Compliance Audit (oracle)
├── Task F2: Code Quality Review
├── Task F3: Real Manual QA (全场景端到端测试)
└── Task F4: Scope Fidelity Check
```

---

## TODOs

> **通用要求（所有任务必须遵守）**：
> 
> **注释要求**: 每段代码必须有中文注释。注释要说明"这段代码干什么"和"为什么这么设计"。不要只写变量名翻译，要写设计意图。
> 
> **文档要求**: 每个模块完成后，创建对应的中文技术文档到 `docs/` 目录。文档需解释：技术选型依据、实现原理、关键配置参数说明。文件名格式: `docs/rag-quality-{module-name}.md`
> 
> **优化清单更新**: 全部任务完成后，统一更新 `docs/RAG优化清单.md`，遵循原文件格式（表格/标记/日期分段）。

- [x] 1. 测试基础设施搭建（conftest.py + Mock Judge + fixtures）

  **What to do**:
  - 创建 `tests/__init__.py`、`tests/test_quality/__init__.py`
  - 创建 `tests/conftest.py`：
    - pytest-asyncio 事件循环配置
    - `mock_llm_judge` fixture：一个模拟 LLM Judge 的 provider，根据输入 prompt 关键词返回预设评价 JSON
    - `sample_query_context` fixture：模拟查询 + 检索上下文 + LLM 回答
    - `eval_dataset` fixture：加载 `tests/test_data/eval_dataset.json`
  - 创建 `tests/test_quality/conftest.py`：QualityGuard 相关 fixtures
  - 创建 `tests/test_data/eval_dataset.json`：初始空结构（Task 12 填充完整数据）
  - 验证 `pytest tests/test_quality/ --asyncio-mode=auto` 可运行

  **Mock Judge 设计要点**：
  - 解析 Judge Prompt 中的 `[EVALUATION_TASK]` 标记，按任务类型返回预定义结果
  - 支持安全、事实、相关性三种评判模式
  - 支持正常返回 / 超时模拟 / 格式错误返回 三种模式（通过特殊标记触发）

  **Must NOT do**:
  - 不依赖真实 LLM 调用
  - 不修改任何现有测试文件（目前无测试文件）

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 基础设施搭建，模式固定，不需要复杂推理
  - **Skills**: `[]` — 不需要加载额外技能

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 2, 3, 4)
  - **Blocks**: Tasks 5, 6, 7, 8, 11
  - **Blocked By**: None

  **References**:
  - `src/llm/providers/mock.py` — 现有 Mock LLM Provider，参考其接口模式
  - `pyproject.toml:83-84` — pytest 配置（asyncio_mode = "auto", testpaths = ["tests"]）
  - `tests/` — 确认目前无测试目录，需要新建

  **Acceptance Criteria**:
  - [ ] `tests/conftest.py` 存在，包含 mock_llm_judge fixture
  - [ ] `tests/test_data/eval_dataset.json` 存在
  - [ ] `pytest tests/test_quality/ --asyncio-mode=auto --co` → 返回 `0 passed`（空测试套件可运行）

  **QA Scenarios**:
  ```
  Scenario: 测试框架可用
    Tool: Bash
    Preconditions: 测试基础设施文件已创建
    Steps:
      1. cd $PROJECT_ROOT
      2. pip install -e ".[dev]" (if needed)
      3. pytest tests/test_quality/ --asyncio-mode=auto --tb=short -q
    Expected Result: 测试运行成功，无报错（至少 0 passed）
    Evidence: .sisyphus/evidence/task-1-test-framework.txt

  Scenario: Mock Judge 可被调用
    Tool: Bash
    Preconditions: conftest.py 包含 mock_llm_judge fixture
    Steps:
      1. 创建一个临时测试文件 tests/test_quality/test_mock_judge_works.py
      2. 运行 pytest tests/test_quality/test_mock_judge_works.py -v
    Expected Result: Mock Judge fixture 可正常注入，返回预设 JSON
    Evidence: .sisyphus/evidence/task-1-mock-judge.txt
  ```

  **Commit**: YES
  - Message: `test(quality): add test infrastructure with Mock LLM Judge and fixtures`
  - Files: `tests/__init__.py`, `tests/conftest.py`, `tests/test_quality/__init__.py`, `tests/test_quality/conftest.py`, `tests/test_data/eval_dataset.json`

---

- [x] 2. Config 扩展（质检参数 + 干预规则 + 自定义类别）

  **What to do**:
  - 在 `src/config.py` 的 `Settings` 类中新增质量保障相关配置：
    ```python
    # Quality Guard
    quality_guard_enabled: bool = True
    
    # Judge 模型（交叉评判——与生成模型不同，更客观）
    # 生成用主 LLM provider，评判用专门的 judge 模型
    quality_judge_provider: str = ""   # 空=跟随 llm_provider
    quality_judge_model: str = "deepseek/deepseek-v4-flash"  # 轻量模型做评判，降低成本
    
    quality_eval_dimensions: list[str] = ["safety", "factuality", "relevance", "retrieval_quality"]
    quality_judge_timeout_s: int = 10
    quality_parallel_eval: bool = True  # 维度并行执行
    quality_fail_closed_for_safety: bool = True
    quality_fail_open_for_others: bool = True
    quality_max_answer_chars_for_judge: int = 4000
    quality_skip_on_timeout: bool = True  # 超时时跳过质检而非阻塞
    ```
  - 创建 `src/quality/` 模块目录和 `__init__.py`
  - 创建 `src/quality/config.py`：
    - 干预规则：`InterventionRule` Pydantic 模型（violation_type → action mapping）
    - 自定义安全类别：`SafetyCategory` Pydantic 模型（name, keywords, description）
    - 默认配置初始化函数，加载内置 + 自定义类别
    - 从 YAML 文件加载自定义安全类别的方法
  - 创建 `prompts/quality/` 目录
  - 示例 YAML 配置结构（用于用户参考）

  **Must NOT do**:
  - 不实现具体质检逻辑（仅配置模型和加载逻辑）
  - 不修改现有 config.py 中的检索参数

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 配置模型定义，Pydantic schema，模式明确
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 3, 4)
  - **Blocks**: Tasks 5, 8, 9
  - **Blocked By**: None

  **References**:
  - `src/config.py` — 现有 Settings 模式，遵循 Pydantic Settings 风格
  - `src/api/schemas.py` — Pydantic model 定义模式参考
  - `prompts/rag/query.yaml` — 现有 Prompt YAML 格式

  **Acceptance Criteria**:
  - [ ] `src/config.py` 包含 quality_guard 相关配置字段（至少 6 个）
  - [ ] `src/quality/__init__.py` 和 `src/quality/config.py` 存在
  - [ ] InterventionRule Pydantic model 正确定义
  - [ ] SafetyCategory Pydantic model 正确定义
  - [ ] 从 YAML 加载自定义安全类别的函数实现

  **QA Scenarios**:
  ```
  Scenario: Config 加载正常
    Tool: Bash
    Preconditions: 文件已创建
    Steps:
      1. cd $PROJECT_ROOT
      2. python -c "from src.config import settings; print(settings.quality_guard_enabled); print(settings.quality_eval_dimensions)"
    Expected Result: 输出 True 和 ['safety', 'factuality', 'relevance']
    Evidence: .sisyphus/evidence/task-2-config-load.txt

  Scenario: InterventionRule 模型可初始化
    Tool: Bash
    Preconditions: src/quality/config.py 存在
    Steps:
      1. python -c "
      from src.quality.config import InterventionRule, SafetyCategory
      rule = InterventionRule(violation_type='safety_hate_speech', action='block', message='抱歉，无法展示此内容')
      cat = SafetyCategory(name='custom_category', keywords=['badword'], description='描述')
      print(rule.action, cat.name)
      "
    Expected Result: 输出 "block custom_category"
    Evidence: .sisyphus/evidence/task-2-intervention-model.txt
  ```

  **Commit**: YES (groups with Task 3, 4)
  - Message: `feat(quality): add config schema for quality guard, intervention rules, and safety categories`
  - Files: `src/config.py`, `src/quality/__init__.py`, `src/quality/config.py`

---

- [x] 3. API Schema 扩展（QueryResponse 干预信号字段）

  **What to do**:
  - 在 `src/api/schemas.py` 中扩展：
    - 新增 `QualityVerdict` Pydantic model：`{dimension: str, passed: bool, score: float, details: str}`
    - 新增 `InterventionInfo` Pydantic model：`{intervened: bool, action: str, reason: str, violations: list[QualityVerdict]}`
    - `QueryResponse` 扩展：添加 `quality: InterventionInfo | None = None` 可选字段
  - 定义 `QueryResponse` 在各干预模式下的响应形状：
    - **正常**: `{"answer": "...", "sources": [...], "quality": {"intervened": false, ...}}`
    - **拦截(BLOCK)**: `{"answer": "抱歉，根据内容安全策略，无法展示此回答。", "sources": [], "quality": {"intervened": true, "action": "block", "reason": "safety_hate_speech: 内容包含不当言论", "violations": [...]}}`
    - **警告(WARN)**: `{"answer": "...", "sources": [...], "quality": {"intervened": true, "action": "warn", "reason": "factuality_low: 部分内容与来源不一致", "violations": [...]}}`
    - **降级(DEGRADE)**: `{"answer": "", "sources": [...], "quality": {"intervened": true, "action": "degrade", ...}}`

  **Must NOT do**:
  - 不改动现有字段（answer, sources — 保持向后兼容）
  - 不实现质检逻辑

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Pydantic schema 定义，模式固定
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 4)
  - **Blocks**: Tasks 9, 10, 11
  - **Blocked By**: None

  **References**:
  - `src/api/schemas.py:64-80` — 现有 QueryResponse 和 SourceInfo 定义

  **Acceptance Criteria**:
  - [ ] QualityVerdict, InterventionInfo 模型正确定义
  - [ ] QueryResponse 新增 `quality` 可选字段
  - [ ] 各干预模式的响应形状有明确注释

  **QA Scenarios**:
  ```
  Scenario: Schema 编译正确
    Tool: Bash
    Preconditions: src/api/schemas.py 已修改
    Steps:
      1. python -c "
      from src.api.schemas import QueryResponse, SourceInfo, QualityVerdict, InterventionInfo
      qv = QualityVerdict(dimension='safety', passed=True, score=0.95, details='内容安全')
      ii = InterventionInfo(intervened=False, action='none', reason='', violations=[qv])
      resp = QueryResponse(answer='test', sources=[], quality=ii)
      print(resp.model_dump_json(indent=2))
      "
    Expected Result: 成功输出 JSON，包含 quality 字段
    Evidence: .sisyphus/evidence/task-3-schema-valid.txt
  ```

  **Commit**: YES (groups with Task 2, 4)
  - Message: `feat(quality): extend API schema with quality verdict and intervention info`
  - Files: `src/api/schemas.py`

---

- [x] 4. 质检查询 Prompt 模板（安全/事实/相关性 YAML）

  **What to do**:
  - 创建 `prompts/quality/safety_judge.yaml`：安全评判 Prompt
    - 系统指令：明确角色为内容安全审查员
    - 输入：用户问题、LLM 回答、检索上下文
    - 输出要求：JSON 格式 `{"passed": bool, "score": float, "violations": [{"type": str, "detail": str}], "reasoning": str}`
    - 评判维度：仇恨言论、歧视、违法内容、敏感政治、其他自定义
    - 标注 refusals（合法拒绝）不应被判违规
  - 创建 `prompts/quality/factuality_judge.yaml`：事实一致性评判 Prompt
    - 系统指令：检查 LLM 回答是否与检索上下文一致
    - 输入：用户问题、LLM 回答、检索上下文原文
    - 输出：JSON `{"passed": bool, "score": float, "hallucinations": [{"claim": str, "supported": bool}], "reasoning": str}`
    - 明确 "我不知道" 类回答应被视为事实正确
  - 创建 `prompts/quality/relevance_judge.yaml`：相关性评判 Prompt
    - 系统指令：检查回答是否与用户问题相关
    - 输入：用户问题、LLM 回答
    - 输出：JSON `{"passed": bool, "score": float, "reasoning": str}`
  - 每个模板需包含明确的 JSON 输出格式说明
  - 修改 `src/utils/prompt_loader.py`（如果必要）以支持子目录

  **Must NOT do**:
  - 不包含评判逻辑实现（仅 Prompt 模板）
  - 不使用占位变量以外的动态内容

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: Prompt 工程，需精确的指令写作能力
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 3)
  - **Blocks**: Tasks 5, 6, 7
  - **Blocked By**: None

  **References**:
  - `prompts/rag/query.yaml` — 现有 RAG Prompt 格式和风格
  - `src/utils/prompt_loader.py` — Prompt 加载逻辑

  **Acceptance Criteria**:
  - [ ] 3 个 YAML 文件创建在 `prompts/quality/` 下
  - [ ] 每个 YAML 包含完整的系统指令和输出格式规范
  - [ ] `load_prompt("quality/safety_judge", question="...", answer="...", context="...")` 可加载
  - [ ] 输出格式显式定义为 JSON，含 passed/score/violations/reasoning 字段

  **QA Scenarios**:
  ```
  Scenario: Prompt 可加载
    Tool: Bash
    Preconditions: prompts/quality/ 下三个 YAML 文件存在
    Steps:
      1. python -c "
      from src.utils.prompt_loader import load_prompt
      prompt = load_prompt('quality/safety_judge', question='test', answer='test', context='test')
      print('Loaded:', prompt[:100])
      "
    Expected Result: Prompt 加载成功，内容包含 "safety" "judge" 等关键词
    Evidence: .sisyphus/evidence/task-4-prompt-load.txt
  ```

  **Commit**: YES (groups with Task 2, 3)
  - Message: `feat(quality): add judge prompt templates for safety, factuality, relevance`
  - Files: `prompts/quality/safety_judge.yaml`, `prompts/quality/factuality_judge.yaml`, `prompts/quality/relevance_judge.yaml`

---

- [x] 5. QualityJudge 基类 + SafetyChecker（含关键词预筛）

  **What to do**:
  - 创建 `src/quality/` 模块结构：
    - `src/quality/__init__.py` — 导出核心类
    - `src/quality/base.py` — `QualityJudge` 抽象基类
    - `src/quality/safety.py` — `SafetyChecker` 实现
    - `src/quality/keyword_filter.py` — 关键词预筛模块
  - `QualityJudge` 基类：
    - `__init__(self, llm_provider, prompt_template, config)` — 注入 LLM provider
    - `evaluate(self, query: str, answer: str, context: str | None) -> QualityVerdict` — 抽象方法
    - `_call_judge(prompt: str) -> dict` — 调用 LLM 并解析 JSON 返回
    - 错误处理：JSON 解析失败、LLM 超时、空响应 — 均有兜底
    - `_parse_judge_response(raw: str) -> dict` — 解析 LLM 输出的 JSON（含 JSON 修复逻辑）
  - `SafetyChecker(QualityJudge)`：
    - 实现 evaluate() 方法
    - 调用关键词预筛（快速失败：关键词命中→直接 BLOCK）
    - 未命中关键词→调用 LLM 语义评判
    - 使用 `prompts/quality/safety_judge.yaml` 加载模板
  - `KeywordFilter`：
    - 加载 `src/quality/config.py` 中的 SafetyCategory 列表
    - 按类别组织关键词（内置 + 自定义）
    - `prefilter(text: str) -> list[KeywordsMatch]` — 返回命中的类别列表
    - 支持正则模式（如 `make\s+a\s+bomb`）
    - 快速返回：命中任意类别即返回结果，不继续 LLM 判断
  - LLM 调用失败时按 `quality_fail_closed_for_safety` 配置决定行为

  **Must NOT do**:
  - 不实现 FactualityChecker 和 RelevanceChecker（将在 Task 6、7 实现）
  - 不修改 QueryEngine（集成在 Task 10）

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: 质检核心逻辑，需要周全设计错误处理、JSON 解析、兜底策略
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 6, 7, 8)
  - **Blocks**: Tasks 9, 10, 11
  - **Blocked By**: Task 1, 2, 4

  **References**:
  - `prompts/quality/safety_judge.yaml` — 安全评判 Prompt（Task 4）
  - `src/config.py` — quality_guard 配置（Task 2）
  - `src/quality/config.py` — InterventionRule, SafetyCategory（Task 2）
  - `src/llm/router.py` — LLM 路由，获取 provider 实例
  - `src/api/schemas.py` — QualityVerdict 模型（Task 3）

  **Acceptance Criteria**:
  - [ ] QualityJudge 基类定义 evaluate/ _call_judge/ _parse_judge_response
  - [ ] SafetyChecker.evaluate() 返回 QualityVerdict
  - [ ] 关键词预筛命中→快速返回违规判定，不调 LLM
  - [ ] 关键词未命中→调用 LLM 语义判断
  - [ ] LLM 超时/异常→按配置 fail-closed(拦截) 或 fail-open(放行)
  - [ ] 正确拒答（refusals）不被判违规

  **QA Scenarios**:
  ```
  Scenario: 关键词预筛命中→快速拦截
    Tool: Bash
    Preconditions: KeywordFilter 配置了预设关键词
    Steps:
      1. python -c "
      from src.quality.keyword_filter import KeywordFilter
      from src.quality.config import SafetyCategory
      cats = [SafetyCategory(name='test', keywords=['badword'], description='')]
      kf = KeywordFilter(categories=cats)
      result = kf.prefilter('this contains badword')
      print(result)
      "
    Expected Result: 返回 [KeywordMatch(category='test', keyword='badword')]
    Evidence: .sisyphus/evidence/task-5-keyword-hit.txt

  Scenario: 正常回答通过安全检查
    Tool: Bash (via pytest)
    Preconditions: Mock LLM Judge fixture 可用
    Steps:
      1. pytest tests/test_quality/test_safety_checker.py::test_normal_answer_passes -v
    Expected Result: 测试通过，安全回答返回 passed=True, score>=0.8
    Evidence: .sisyphus/evidence/task-5-safety-pass.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): implement QualityJudge base class and SafetyChecker with keyword filter`
  - Files: `src/quality/__init__.py`, `src/quality/base.py`, `src/quality/safety.py`, `src/quality/keyword_filter.py`

---

- [x] 6. FactualityChecker（LLM 事实一致性校验）

  **What to do**:
  - 创建 `src/quality/factuality.py`：
    - `FactualityChecker(QualityJudge)` 类
    - **交叉评判**：使用与 LLM 生成**不同的模型**（`quality_judge_model`）做评判，避免自我增强偏差
    - `evaluate(query, answer, context) -> QualityVerdict`
    - 使用 `prompts/quality/factuality_judge.yaml` Prompt
    - 输入：用户问题、LLM 回答、检索上下文（SourceInfo 中的原始内容）
    - 输出维度：
      - 回答中的 claims 是否有上下文支撑
      - 引用的来源是否对应实际内容
      - 是否有编造数据/引用
    - 特殊处理："我不知道" / "没有找到" 类回答自动 pass（LLM 正确拒答不应被判违规）
    - 空 context 时（无检索结果）：标记为 factuality 低分但有特殊标识
    - 调用失败时按 `quality_fail_open_for_others` 配置放行 + 记录警告
  - 创建 `tests/test_quality/test_factuality_checker.py`（TDD）

  **Must NOT do**:
  - 不修改 QueryEngine
  - 不实现安全检查逻辑（已在 Task 5）

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: 事实校验需要精准的 claim 提取与比对逻辑
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 5, 7, 8)
  - **Blocks**: Tasks 9, 10, 11
  - **Blocked By**: Task 1, 2, 4

  **References**:
  - `prompts/quality/factuality_judge.yaml` — 事实校验 Prompt（Task 4）
  - `src/quality/base.py` — QualityJudge 基类
  - `src/api/schemas.py:SourceInfo` — 检索来源格式

  **Acceptance Criteria**:
  - [ ] FactualityChecker.evaluate() 返回 QualityVerdict
  - [ ] 有上下文支撑的回答→passed=True
  - [ ] 包含编造内容的回答→passed=False
  - [ ] "我不知道" 回答→自动 passed
  - [ ] 空 context → 标记低分但非违规

  **QA Scenarios**:
  ```
  Scenario: 事实正确回答通过
    Tool: Bash (via pytest)
    Preconditions: Mock LLM Judge fixture
    Steps:
      1. pytest tests/test_quality/test_factuality_checker.py::test_answer_grounded_in_context -v
    Expected Result: 测试通过
    Evidence: .sisyphus/evidence/task-6-factuality-pass.txt

  Scenario: 编造内容被检测
    Tool: Bash (via pytest)
    Preconditions: Mock LLM Judge fixture 配置返回 hallucination
    Steps:
      1. pytest tests/test_quality/test_factuality_checker.py::test_hallucination_detected -v
    Expected Result: 测试通过，返回 passed=False, 包含 hallucination claims
    Evidence: .sisyphus/evidence/task-6-factuality-fail.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): implement FactualityChecker for hallucination and citation verification`
  - Files: `src/quality/factuality.py`, `tests/test_quality/test_factuality_checker.py`

---

- [x] 7. RelevanceChecker（LLM 回答相关性校验）

  **What to do**:
  - 创建 `src/quality/relevance.py`：
    - `RelevanceChecker(QualityJudge)` 类
    - `evaluate(query, answer, context=None) -> QualityVerdict`
    - 使用 `prompts/quality/relevance_judge.yaml` Prompt
    - 检查维度：
      - 回答是否直接回答了用户问题
      - 是否有离题/无关内容
      - 是否有未回答的问题部分
    - 不依赖 context（仅判断回答 vs 问题）
    - 调用失败时 fail-open
  - 创建 `tests/test_quality/test_relevance_checker.py`（TDD）

  **Must NOT do**:
  - 不修改 QueryEngine
  - 不依赖检索上下文

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: 逻辑较 Safety/Factuality 简单，但需确保 LLM 评判准确
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 5, 6, 8)
  - **Blocks**: Tasks 9, 10, 11
  - **Blocked By**: Task 1, 2, 4

  **References**:
  - `prompts/quality/relevance_judge.yaml` — 相关性 Prompt（Task 4）
  - `src/quality/base.py` — QualityJudge 基类

  **Acceptance Criteria**:
  - [ ] RelevanceChecker.evaluate() 返回 QualityVerdict
  - [ ] 相关回答→passed=True, score>=0.7
  - [ ] 离题回答→passed=False
  - [ ] 不依赖 context 参数

  **QA Scenarios**:
  ```
  Scenario: 相关回答通过
    Tool: Bash (via pytest)
    Preconditions: Mock LLM Judge fixture
    Steps:
      1. pytest tests/test_quality/test_relevance_checker.py::test_relevant_answer -v
    Expected Result: 测试通过
    Evidence: .sisyphus/evidence/task-7-relevance-pass.txt

  Scenario: 离题回答被标记
    Tool: Bash (via pytest)
    Preconditions: Mock LLM Judge fixture 返回低分
    Steps:
      1. pytest tests/test_quality/test_relevance_checker.py::test_off_topic_answer -v
    Expected Result: 测试通过，返回 passed=False
    Evidence: .sisyphus/evidence/task-7-relevance-fail.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): implement RelevanceChecker for answer relevance verification`
  - Files: `src/quality/relevance.py`, `tests/test_quality/test_relevance_checker.py`

---

- [x] 8. 安全关键词预筛模块（关键词列表 + 类别映射完善）

  **What to do**:
  - 在 `src/quality/keyword_filter.py`（已创建初始结构）中完善：
    - 内置安全类别列表（P0 级别）：
      - `sensitivity_political`: 敏感政治关键词
      - `hate_speech`: 仇恨/歧视言论关键词
      - `illegal_content`: 违法内容关键词
      - `violence`: 暴力内容关键词
    - 自定义类别加载：从 `quality_custom_categories.yaml` 或 `src/quality/config.py` 加载
    - 关键词匹配引擎：
      - 精确匹配（子串/全词）
      - 正则匹配（模式匹配）
      - 中文分词兼容（对中文关键词做 jieba 分词后再匹配）
    - 匹配结果包含：命中类别、命中关键词、位置、匹配模式
    - 性能考虑：关键词超过 100 条时使用 Aho-Corasick 算法（或类似多模式匹配）
    - 内置类别关键词配置在 `src/quality/config.py` 中
  - 创建 `tests/test_quality/test_keyword_filter.py`（TDD）

  **Must NOT do**:
  - 不做自定义类别管理 UI
  - 不依赖 LLM 调用

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: 关键词匹配引擎需处理中文/正则/性能优化
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 5, 6, 7)
  - **Blocks**: Task 9
  - **Blocked By**: Task 2

  **References**:
  - `src/knowledge/tokenizer.py` — 现有 jieba 分词逻辑，参考中文处理模式
  - `src/quality/config.py:SafetyCategory` — 类别模型定义

  **Acceptance Criteria**:
  - [ ] 内置至少 4 个安全类别，每个类别含至少 5 个关键词
  - [ ] 关键词支持精确/正则两种匹配模式
  - [ ] 匹配结果包含类别、关键词、位置信息
  - [ ] 测试覆盖：命中、不命中、正则匹配、边界情况

  **QA Scenarios**:
  ```
  Scenario: 内置类别命中
    Tool: Bash (via pytest)
    Preconditions: 关键词列表已配置
    Steps:
      1. pytest tests/test_quality/test_keyword_filter.py::test_builtin_categories_hit -v
    Expected Result: 测试通过，预置违规关键词能正确命中
    Evidence: .sisyphus/evidence/task-8-keyword-hit.txt

  Scenario: 正常文本不命中
    Tool: Bash (via pytest)
    Steps:
      1. pytest tests/test_quality/test_keyword_filter.py::test_normal_text_no_hit -v
    Expected Result: 测试通过，正常文本不命中任何类别
    Evidence: .sisyphus/evidence/task-8-keyword-clean.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): implement keyword pre-filter with built-in and custom safety categories`
  - Files: `src/quality/keyword_filter.py`, `src/quality/config.py`, `tests/test_quality/test_keyword_filter.py`

---

- [x] 8b. RetrievalQualityChecker（检索质量校验器）

  **What to do**:
  - 创建 `src/quality/retrieval_quality.py`：
    - `RetrievalQualityChecker` 类（注意：不继承 QualityJudge，因为不需要 LLM 评判）
    - `evaluate(query, retrieved_nodes, scores) -> QualityVerdict`
    - 检查维度：
      - **平均分**: retrieved chunks 的平均相似度分数
      - **最高分**: 最佳匹配 chunk 的分数
      - **阈值通过率**: 有多少 chunk 超过 `retrieval_stage1_threshold`（当前 0.35）
      - **分数离散度**: 高分和低分的差距（如果差距大说明只有部分相关）
    - 评分规则：
      - `avg_score >= 0.5` → passed (检索质量好)
      - `avg_score >= 0.3` → borderline (警告)
      - `avg_score < 0.3` → failed (检索质量差，知识库可能缺少相关内容)
    - **预生成检查**: 如果最高分 < threshold，跳过 LLM 生成，直接返回"未找到相关信息"
    - **后生成报告**: 在 quality 字段中报告检索质量指标
    - 无需调用 LLM（纯计算），零额外成本
  - 更新 `src/quality/config.py`：添加检索质量阈值配置
  - 创建 `tests/test_quality/test_retrieval_quality.py`（TDD）
  - 更新 `prompts/quality/`：不需要单独 Prompt（纯计算）

  **Must NOT do**:
  - 不调用 LLM（纯数值计算）
  - 不修改现有的检索逻辑（读取分数即可）

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 纯数值计算，无需 LLM，逻辑明确
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2 (with Tasks 5, 6, 7, 8)
  - **Blocks**: Task 9, 10
  - **Blocked By**: Task 2

  **References**:
  - `src/knowledge/query_engine.py:70-71` — 现有 stage1_threshold 分数判断逻辑
  - `src/config.py:65` — `retrieval_stage1_threshold: float = 0.35`

  **Acceptance Criteria**:
  - [ ] RetrievalQualityChecker 返回 QualityVerdict
  - [ ] avg_score>=0.5 → passed=True
  - [ ] avg_score <0.3 → passed=False
  - [ ] 最高分低于 threshold → 建议跳过 LLM 生成
  - [ ] 所有指标在 quality 字段中可查

  **QA Scenarios**:
  ```
  Scenario: 高分检索通过
    Tool: Bash (via pytest)
    Steps:
      1. pytest tests/test_quality/test_retrieval_quality.py::test_high_score_passes -v
    Expected Result: 测试通过，返回 passed=True
    Evidence: .sisyphus/evidence/task-8b-retrieval-pass.txt

  Scenario: 低分检索被标记
    Tool: Bash (via pytest)
    Steps:
      1. pytest tests/test_quality/test_retrieval_quality.py::test_low_score_fails -v
    Expected Result: 测试通过，返回 passed=False, score<0.3
    Evidence: .sisyphus/evidence/task-8b-retrieval-fail.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): implement RetrievalQualityChecker for retrieval score evaluation`
  - Files: `src/quality/retrieval_quality.py`, `tests/test_quality/test_retrieval_quality.py`

---

- [x] 9. InterventionEngine（违规→动作映射 + 执行 + 响应改写）

  **What to do**:
  - 创建 `src/quality/intervention.py`：
    - `InterventionEngine` 类
    - `__init__(self, rules: list[InterventionRule])` — 加载配置规则
    - `evaluate(verdicts: list[QualityVerdict]) -> InterventionInfo` — 核心方法
      - 遍历所有 verdicts
      - 按优先级处理：安全违规 > 事实违规 > 相关性违规
      - 第一个命中的最高优先级规则决定最终动作
    - `execute(intervention: InterventionInfo, original_response: dict) -> dict` — 执行动作
      - **BLOCK**: 返回 `{"answer": "抱歉，根据内容安全策略，无法展示此回答。", "sources": [], "quality": {...}}`
      - **REWRITE**: 将 answer 替换为 "由于内容不合规，无法展示"（用户要求）
      - **WARN**: 在 answer 末尾追加警告标记（如 `\n\n---\n⚠️ 此回答部分内容未经事实校验`）
      - **DEGRADE**: 将 answer 置空，保留 sources，标记 `quality.action: "degrade"`
    - `run_all(verdicts, response) -> (dict, InterventionInfo)` — 一次调用完成评估+执行
  - 优先级规则：
    1. **Safety BLOCK** → 任何安全违规 → BLOCK
    2. **Factuality FAIL** → 事实违规 → DEGRADE（只给来源）
    3. **Retrieval FAIL** → 检索质量差 → WARN（在回答追加"检索结果可能不完整"提示）
    4. **Relevance FAIL** → 相关性违规 → WARN（警告标记）
  - 规则配置化：可在 `src/quality/config.py` 中调整映射关系
  - 创建 `tests/test_quality/test_intervention.py`（TDD）

  **Must NOT do**:
  - 不实现质检逻辑（已由 Checker 完成）
  - 不修改 QueryEngine

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: 干预引擎是策略核心，需要精细处理优先级、组合、边界情况
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 3 (sequential with 10, 11)
  - **Blocks**: Tasks 10, 11
  - **Blocked By**: Tasks 5, 6, 7, 8 (Wave 2)

  **References**:
  - `src/quality/config.py:InterventionRule` — 规则模型（Task 2）
  - `src/api/schemas.py:InterventionInfo, QualityVerdict` — 响应模型（Task 3）

  **Acceptance Criteria**:
  - [ ] 安全违规→BLOCK：返回预设消息，不暴露原回答
  - [ ] 事实违规→DEGRADE：answer 置空，sources 保留
  - [ ] 相关性违规→WARN：追加警告标记
  - [ ] 多重违规→安全优先于其他
  - [ ] 无违规→quality.intervened=False

  **QA Scenarios**:
  ```
  Scenario: 安全违规→BLOCK
    Tool: Bash (via pytest)
    Steps:
      1. pytest tests/test_quality/test_intervention.py::test_safety_violation_blocks -v
    Expected Result: 返回 answer 为预设拦截消息, intervened=True, action='block'
    Evidence: .sisyphus/evidence/task-9-block.txt

  Scenario: 多重违规→安全优先
    Tool: Bash (via pytest)
    Steps:
      1. pytest tests/test_quality/test_intervention.py::test_safety_overrides_others -v
    Expected Result: 即使同时有安全+事实违规，最终动作为 block（安全优先）
    Evidence: .sisyphus/evidence/task-9-priority.txt

  Scenario: 无违规→正常放行
    Tool: Bash (via pytest)
    Steps:
      1. pytest tests/test_quality/test_intervention.py::test_no_violation_passes -v
    Expected Result: intervened=False, action='none', 原回答不变
    Evidence: .sisyphus/evidence/task-9-pass.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): implement InterventionEngine with configurable violation-to-action mapping`
  - Files: `src/quality/intervention.py`, `tests/test_quality/test_intervention.py`

---

- [x] 10. 质检与 QueryEngine 集成（QualityGuard 编排 + query() 钩子）

  **What to do**:
  - 创建 `src/quality/guard.py`：`QualityGuard` 编排类
    - `__init__(self, checkers: list[QualityJudge], intervention: InterventionEngine)`
    - `run(query, answer, context, sources) -> (dict, InterventionInfo)`
    - 并行/串行执行 checkers（按 `quality_parallel_eval` 配置）
    - 超时保护（按 `quality_judge_timeout_s`）
    - 异常保护：任何一个 checker 失败→记录日志→继续其他维度
  - 修改 `src/knowledge/query_engine.py`：
    - `QueryEngine.__init__()` 中新增 `quality_guard: QualityGuard | None` 参数
    - `query()` 方法中，在 LLM 生成 answer 之后、return 之前嵌入质检流程：
      ```python
      # After LLM generation
      if self.quality_guard and settings.quality_guard_enabled:
          checked_result, intervention = self.quality_guard.run(
              question=question, answer=answer, context=context, sources=sources
          )
          result = checked_result
          result["quality"] = intervention
      ```
    - 缓存策略修改：缓存**质检后的结果**（包含 quality 字段），避免重复质检
    - 异常保护：`quality_guard.run()` 抛出异常→记录日志→返回原始 answer（fail-open 兜底）
    - `query_stream()` 不做修改（已标明不覆盖）
  - 创建 `tests/test_quality/test_quality_guard.py`：QualityGuard 编排测试

  **Must NOT do**:
  - 不改动 streaming 路径
  - 不改动检索/reranker 流程

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: 集成是关键环节，需协调多个组件、缓存、异常处理
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 3 (sequential: 9 → 10 → 11)
  - **Blocks**: Task 11
  - **Blocked By**: Tasks 5, 6, 7, 9

  **References**:
  - `src/knowledge/query_engine.py:109-142` — 现有 query() 方法
  - `src/knowledge/query_engine.py:34-41` — 缓存键生成和缓存逻辑
  - `src/quality/base.py` — QualityJudge 基类
  - `src/quality/intervention.py` — InterventionEngine
  - `src/config.py` — quality_guard_enabled 等配置

  **Acceptance Criteria**:
  - [ ] query() 中 LLM 生成后自动触发质检
  - [ ] 质检结果写入 response 的 quality 字段
  - [ ] 缓存包含质检后结果（避免重复质检）
  - [ ] 质检异常→日志记录+原始 answer 放行
  - [ ] streaming 路径不受影响

  **QA Scenarios**:
  ```
  Scenario: 正常回答含 quality 字段
    Tool: curl
    Preconditions: 系统运行，有已上传文档
    Steps:
      1. curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -d '{"question": "测试", "top_k": 3}'
      2. 提取 .quality 字段
    Expected Result: quality 字段存在，intervened=false
    Evidence: .sisyphus/evidence/task-10-quality-field.txt

  Scenario: 质检异常→原始回答放行
    Tool: Bash (via pytest with mock)
    Steps:
      1. pytest tests/test_quality/test_quality_guard.py::test_guard_exception_fallback -v
    Expected Result: 异常时返回原始 answer，记录日志，不阻塞
    Evidence: .sisyphus/evidence/task-10-fallback.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): integrate QualityGuard into QueryEngine with caching and error handling`
  - Files: `src/quality/guard.py`, `src/knowledge/query_engine.py`, `tests/test_quality/test_quality_guard.py`

---

- [x] 11. 集成测试（端到端：质检+干预全流程）

  **What to do**:
  - 创建 `tests/test_quality/test_integration.py`：
    - `test_end_to_end_safety_block()`: Mock 全流程，安全违规→BLOCK
    - `test_end_to_end_factuality_degrade()`: 事实违规→DEGRADE
    - `test_end_to_end_relevance_warn()`: 相关性低→WARN
    - `test_end_to_end_all_pass()`: 全部通过→正常返回
    - `test_end_to_end_guard_disabled()`: quality_guard_enabled=False→不触发质检
    - `test_end_to_end_multi_violation_safety_first()`: 多重违规→安全优先
    - `test_end_to_end_no_context_handling()`: 空 context→正确处理
    - `test_cache_contains_quality_result()`: 缓存包含质检结果
  - 每个测试使用 Mock LLM，不调用真实 LLM
  - 验证 response 的各字段正确性（answer, sources, quality 所有子字段）
  - 运行 `pytest tests/test_quality/` 确认全部通过

  **Must NOT do**:
  - 不调用真实 LLM API
  - 不启动 FastAPI 服务器（纯单元级集成）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: 集成测试需覆盖全流程的多种场景
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 3 (final task)
  - **Blocks**: Wave 4
  - **Blocked By**: Tasks 9, 10

  **References**:
  - `src/quality/guard.py` — QualityGuard 编排
  - `src/quality/intervention.py` — InterventionEngine
  - `src/knowledge/query_engine.py` — 修改后的 QueryEngine
  - `tests/conftest.py:mock_llm_judge` — Mock LLM Judge fixture

  **Acceptance Criteria**:
  - [ ] 8 个集成测试全部通过
  - [ ] 覆盖安全/事实/相关性三种违规场景
  - [ ] 覆盖正常通过场景
  - [ ] 覆盖质检禁用场景
  - [ ] 覆盖优先级逻辑
  - [ ] 测试运行不调用任何真实 API

  **QA Scenarios**:
  ```
  Scenario: 全量集成测试通过
    Tool: Bash
    Preconditions: 所有 task 1-10 完成
    Steps:
      1. pip install -e ".[dev]"
      2. pytest tests/test_quality/ -v --tb=short --asyncio-mode=auto 2>&1
    Expected Result: 所有测试通过（至少 15+ passed）
    Evidence: .sisyphus/evidence/task-11-all-tests-pass.txt
  ```

  **Commit**: YES (groups with Task 10)
  - Message: `test(quality): add end-to-end integration tests for full quality guard pipeline`
  - Files: `tests/test_quality/test_integration.py`

---

- [ ] 12. 评测数据集生成（30 对 QA，LLM 辅助 + 人工校验）

  **What to do**:
  - 完善 `tests/test_data/eval_dataset.json`：
    - 30 条 QA 对，结构为：
    ```json
    {
      "id": "eval_001",
      "question": "公司年假政策是什么？",
      "reference_context": "根据公司规定，工作满1年可享受5天年假...",
      "expected_answer_keywords": ["5天", "年假"],
      "ground_truth": {
        "safety": "pass", "factuality": "pass", "relevance": "pass"
      },
      "adversarial": false,
      "dimension": "normal"
    }
    ```
  - 30 条分布：
    - **正常（10 条）**：基于现有文档的真实问题，全部应 pass
    - **安全对抗（10 条）**：含违规内容模拟（偏见/违法/政治/自定义各类别）
    - **事实挑战（10 条）**：回答含幻觉/编造，应被事实校验拦截
  - 每条含：question, reference_context, ground_truth 标签
  - 使用 LLM 辅助生成初始版本，添加注释说明如何人工校验和扩展

  **Must NOT do**:
  - 不在生产环境运行评测数据集
  - 不自动更新（人工校验后才可用）

  **Recommended Agent Profile**:
  - **Category**: `writing`
    - Reason: 高质量 QA 数据需要精确的问题设计和标注
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 (with Tasks 13, 14)
  - **Blocks**: Task 13
  - **Blocked By**: Task 1

  **Acceptance Criteria**:
  - [ ] eval_dataset.json 包含 30 条有效 QA
  - [ ] 每条包含 question, reference_context, ground_truth 字段
  - [ ] 含 10 条正常、10 条安全对抗、10 条事实挑战
  - [ ] JSON Schema 校验通过

  **QA Scenarios**:
  ```
  Scenario: 数据集格式校验
    Tool: Bash
    Steps:
      1. python -c "
      import json
      with open('tests/test_data/eval_dataset.json') as f:
          data = json.load(f)
      assert len(data) == 30
      print('30 items OK')
      "
    Expected Result: 输出 "30 items OK"
    Evidence: .sisyphus/evidence/task-12-dataset-valid.txt
  ```

  **Commit**: YES
  - Message: `test(quality): add 30-pair evaluation dataset for safety, factuality, and relevance dimensions`
  - Files: `tests/test_data/eval_dataset.json`

---

- [ ] 13. 离线评测脚本 run_eval.py（批量跑分 + Markdown 报告）

  **What to do**:
  - 创建 `scripts/run_eval.py`：
    - 加载 `tests/test_data/eval_dataset.json` 评测数据集
    - 对每条 QA 执行全流程：检索 → LLM 生成 → 质量检测
    - 收集每个维度的评测结果
    - 输出 Markdown 评测报告至 `docs/eval_report_{timestamp}.md`
  - 报告内容：
    - 总览：通过率、各维度平均分
    - 按维度展开：安全通过率 / 事实校验通过率 / 相关性通过率
    - 每个维度的详细结果（每条的 passed/score/details）
    - 失败案例分析（违规类型分布）
    - 对比基线（如果有上次报告）
  - 评分聚合：`macro_avg` 和 `micro_avg`
  - CLI 参数：`--dataset`, `--output`, `--compare`, `--dimensions`, `--verbose`

  **Must NOT do**:
  - 不做 Web Dashboard
  - 不自动触发

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: 脚本需协调多条 QA 执行、结果聚合、报告生成
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 (with Tasks 12, 14)
  - **Blocked By**: Tasks 5, 6, 7

  **Acceptance Criteria**:
  - [ ] scripts/run_eval.py 存在且可运行
  - [ ] 对 30 条 QA 执行全流程评测
  - [ ] 输出 Markdown 报告含总览、分维度结果、失败分析
  - [ ] 支持 CLI 参数 --dataset/--output/--dimensions/--verbose

  **QA Scenarios**:
  ```
  Scenario: 离线评测可运行
    Tool: Bash
    Steps:
      1. python scripts/run_eval.py --verbose --dataset tests/test_data/eval_dataset.json --output docs/eval_report_test.md
    Expected Result: 脚本成功运行，输出报告文件
    Evidence: .sisyphus/evidence/task-13-eval-run.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): add offline evaluation script with markdown report generation`
  - Files: `scripts/run_eval.py`

---

- [ ] 14. Latency 基准测试脚本

  **What to do**:
  - 创建 `scripts/benchmark_quality.py`：
    - 测量 QualityGuard 各阶段延迟
    - 对标准查询运行 N 次（默认 20 次）
    - 统计：min/max/avg/p50/p95/p99（毫秒）
    - 分阶段统计：关键词预筛、Safety LLM、Factuality LLM、Relevance LLM、干预执行
    - 报告**质检总附加时间**（最关键指标）
    - 支持 Mock 模式（不调真实 API）和真实模式
  - 输出：终端表格 + `docs/latency_benchmark_{timestamp}.md`

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 基准测试脚本，模式固定
  - **Skills**: `[]`

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4 (with Tasks 12, 13)
  - **Blocked By**: Tasks 5, 6, 7

  **Acceptance Criteria**:
  - [ ] 脚本可运行输出基准测试报告
  - [ ] 包含各阶段 p50/p95 延迟
  - [ ] 明确标注质检总附加延迟

  **QA Scenarios**:
  ```
  Scenario: 基准测试可运行（Mock模式）
    Tool: Bash
    Steps:
      1. python scripts/benchmark_quality.py --samples 5 --mock --output docs/latency_test.md
    Expected Result: 输出包含各阶段 p50/p95 延迟的报告
    Evidence: .sisyphus/evidence/task-14-benchmark.txt
  ```

  **Commit**: YES
  - Message: `feat(quality): add latency benchmark script for quality guard pipeline`
  - Files: `scripts/benchmark_quality.py`

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

- [ ] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. For each "Must Have": verify implementation exists. For each "Must NOT Have": search codebase for forbidden patterns. Check evidence files exist in .sisyphus/evidence/. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [ ] F2. **Code Quality Review** — `unspecified-high`
  Run `ruff check src/quality/` + `mypy src/quality/` + `pytest tests/test_quality/`. Review for: bare excepts, JSON parsing fragility, missing error handling, hallucination in judge prompt construction.
  **检查中文注释**: 抽查 `src/quality/` 下每个文件，确保关键逻辑有中文注释
  **检查技术文档**: 确认 `docs/rag-quality-*.md` 文件已创建且内容完整
  Output: `Ruff [PASS/FAIL] | Mypy [PASS/FAIL] | Tests [N pass/N fail] | Docs [N/N] | Comments [OK/NEEDED] | VERDICT`

- [ ] F3. **Real Manual QA** — `unspecified-high`
  From clean state: pytest everything passes. Check cross-task integration (all checkers work together). Test edge cases: empty answer, malicious answer, judge timeout, keyword-only safety (no LLM), all violations simultaneously.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [ ] F4. **Scope Fidelity Check** — `deep`
  Verify 1:1 — everything in spec was built (no missing), nothing beyond spec was built (no creep). Check "Must NOT do" compliance: no frontend changes, no streaming path modifications, no existing code modifications.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | VERDICT`

---

## Commit Strategy

| Task | Message | Files |
|------|---------|-------|
| 1 | `test(quality): add test infrastructure with Mock LLM Judge and fixtures` | tests/ |
| 2-4 | `feat(quality): add config, schema, and prompt templates for quality guard` | src/config.py, src/quality/, prompts/quality/, src/api/schemas.py |
| 5 | `feat(quality): implement QualityJudge base class and SafetyChecker with keyword filter` | src/quality/base.py, safety.py, keyword_filter.py, docs/rag-quality-safety.md |
| 6 | `feat(quality): implement FactualityChecker for hallucination and citation verification` | src/quality/factuality.py + tests |
| 7 | `feat(quality): implement RelevanceChecker for answer relevance` | src/quality/relevance.py + tests |
| 8 | `feat(quality): enhance keyword pre-filter with categories and regex` | src/quality/keyword_filter.py, config.py |
| 9 | `feat(quality): implement InterventionEngine with configurable violation-to-action mapping` | src/quality/intervention.py + tests |
| 10-11 | `feat(quality): integrate QualityGuard into QueryEngine + integration tests` | src/quality/guard.py, query_engine.py, test_integration.py |
| 12 | `test(quality): add 30-pair evaluation dataset` | tests/test_data/eval_dataset.json |
| 13 | `feat(quality): add offline evaluation script with markdown report` | scripts/run_eval.py |
| 14 | `feat(quality): add latency benchmark script` | scripts/benchmark_quality.py |

---

## Success Criteria

### Verification Commands
```bash
# 1. All TDD tests pass
pytest tests/test_quality/ -v --tb=short --asyncio-mode=auto
# Expected: All tests pass (20+ tests)

# 2. Config loads correctly
python -c "from src.config import settings; print(settings.quality_guard_enabled)"
# Expected: True

# 3. Schema compiles
python -c "from src.api.schemas import QueryResponse, InterventionInfo, QualityVerdict; print('OK')"
# Expected: OK

# 4. Offline eval can run
python scripts/run_eval.py --dataset tests/test_data/eval_dataset.json --output /tmp/test_report.md
# Expected: Report generated with all metrics

# 5. Latency benchmark runs
python scripts/benchmark_quality.py --samples 5 --mock
# Expected: Latency breakdown printed

# 6. Keyword filter works
python -c "from src.quality.keyword_filter import KeywordFilter; print(KeywordFilter().prefilter('test badword'))"
# Expected: Returns match results
```

### Final Checklist
- [ ] All 14 implementation tasks completed（含中文注释）
- [ ] 技术文档已生成：docs/rag-quality-{safety,factuality,relevance,retrieval,intervention,guard}.md
- [ ] All TDD tests pass (20+ tests across safety/factuality/relevance/intervention/integration)
- [ ] QualityGuard integrated into QueryEngine, intercepts every `query()` call
- [ ] Safety violations → BLOCK with preset message
- [ ] Factuality violations → DEGRADE (sources only)
- [ ] Relevance violations → WARN with warning marker
- [ ] Custom safety categories (keyword + semantic) configurable
- [ ] Offline eval script produces structured Markdown report
- [ ] Latency benchmark established as baseline
- [ ] Streaming path untouched and documented
- [ ] No frontend changes, no dashboard, no existing code modifications

