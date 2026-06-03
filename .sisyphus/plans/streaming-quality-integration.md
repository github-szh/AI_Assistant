# 流式接口 + 质检联动

## TL;DR

> **Quick Summary**: 在现有流式接口 (`POST /query/stream`) 中追加质检机制，LLM 生成完成后自动运行 QualityGuard，通过 SSE 事件将质检结果推送到前端展示，实现流式对话中的实时质检可见性。

> **Deliverables**:
> - 后端: `query_stream()` 追加质检钩子 + 质检 SSE 事件
> - API: 新增 `quality` SSE 事件类型格式
> - 前端: 处理 `quality` SSE 事件，展示质检结果（拦截/警告/通过）
> - 测试: 流式质检集成测试

> **Estimated Effort**: **Small** (~15-25 任务小时)
> **Parallel Execution**: NO — 后端 → 前端 串行
> **Critical Path**: 后端 SSE 事件 → 前端事件处理 → UI 组件 → 集成测试

---

## Context

### 背景
上一阶段已完成同步接口 (`POST /query`) 的质检集成，流式接口 (`POST /query/stream`) 按原计划未覆盖。现用户要求在流式对话中也能看到质检效果。

### 流式质检的架构矛盾
- 质检需要**完整回答**才能评估（安全检查、事实校验）
- 流式是逐字推送的，评估完成时回答已全部发出
- **解决方案**: 后置质检 + SSE 事件通知（不阻塞流式，推送到前端展示）

---

## Work Objectives

### Core Objective
流式对话中，用户能看到每次回答的质检结果（通过/警告/拦截）。

### Concrete Deliverables
- `src/knowledge/query_engine.py` — `query_stream()` 追加质检钩子
- `src/api/schemas.py` — 流式质检事件模型
- 前端项目 — SSE 事件处理 + 质检展示 UI 组件
- 测试 — 流式质检端到端测试

### Must Have
- 流式回答生成完毕后自动运行 QualityGuard
- 质检结果通过 SSE 事件推送到前端
- 前端展示 3 种状态：✅ 通过 / ⚠️ 警告 / 🔴 拦截
- 拦截场景：前端自动覆盖已显示的回答为安全消息
- 警告场景：前端在回答下方显示警告框
- 正常场景：前端显示可收起的质检通过标记
- **代码中文注释**: 所有新增和修改的代码必须添加中文注释，说明"这段代码干什么"和"为什么这么设计"
- **更新优化清单**: 所有代码完成后，更新 `docs/RAG优化清单.md`，维持原文件格式和标记

### Must NOT Have
- **不阻塞流式输出** — 回答照常显示，质检结果后到
- **不改动同步接口的质检逻辑** — 已完成的保持不变
- **不改动现有 SSE 事件格式** — 仅新增 quality 类型事件
- **不修改现有测试** — 仅新增流式相关测试

---

## 定义
- 所有代码必须有中文注释
- 完成后更新 `docs/RAG优化清单.md`

## Execution Strategy

```
Wave 1 (后端 — 串行):
├── Task 1: Schema 扩展 — 新增流式质检事件模型
├── Task 2: query_stream() 质检钩子 — LLM 生成完毕后运行 QualityGuard
├── Task 3: 流式质检测试 — 覆盖安全/事实/正常三种场景

Wave 2 (前端 — 串行，依赖 Wave 1):
├── Task 4: SSE 事件处理器扩展 — 支持 quality 类型事件
├── Task 5: 质检展示 UI 组件 — 通过/警告/拦截三种状态
└── Task 6: 前端集成测试 — 验证 UI 正确响应质检事件

Wave FINAL:
├── F1: 端到端验证 — 真实流式查询全流程
└── F2: 代码质量 + 范围审查
```

---

## TODOs

- [x] 1. Schema + SSE 事件格式定义

  **What to do**:
  - 在 `src/api/schemas.py` 中新增（或确认）流式质检事件模型：
  - 确定 SSE `quality` 事件的 JSON 格式：
    ```json
    {
      "type": "quality",
      "intervened": false,
      "action": "none",
      "reason": "",
      "violations": [
        {"dimension": "safety", "passed": true, "score": 0.95, "details": ""},
        {"dimension": "factuality", "passed": true, "score": 0.90, "details": ""},
        {"dimension": "relevance", "passed": true, "score": 0.85, "details": ""},
        {"dimension": "retrieval_quality", "passed": true, "score": 0.75, "details": ""}
      ]
    }
    ```
  - 定义不同 action 下的附加字段：
    - `action: "block"` → 附加 `override_answer: str`（前端用此替换已显示的回答）
    - `action: "warn"` → 附加 `warning_text: str`（前端在回答下方显示）
    - `action: "degrade"` → 附加 `degrade_reason: str`（前端清空回答，保留来源）
  - 文档化所有 SSE 事件类型（step/sources/c/done/quality）

  **Parallelization**: NO (Wave 1, sequential with 2, 3)
  **Blocked By**: None (参考已有 SSE 格式)
  **Commit**: YES

---

- [x] 2. query_stream() 质检钩子

  **What to do**:
  - 修改 `src/knowledge/query_engine.py` 中的 `query_stream()` 方法：
  - **所有新增代码必须有中文注释**
  - 在 LLM 生成循环后、`{"done": True}` 之前，插入质检流程：
    ```python
    # 收集完整的 answer 文本
    full_answer = "".join(collected_chunks)
    
    # LLM 生成完毕后运行质检
    if self.quality_guard is not None and settings.quality_guard_enabled:
        try:
            context = result.get("context", "")
            sources = result.get("sources", [])
            _, intervention = self.quality_guard.run(
                query=question, answer=full_answer,
                context=context, sources=sources
            )
            
            # 根据干预动作决定 SSE 事件内容
            quality_event = {"type": "quality", ...intervention...}
            if intervention.action == "block":
                quality_event["override_answer"] = settings.quality_block_message or "抱歉，根据内容安全策略，无法展示此回答。"
            elif intervention.action == "warn":
                quality_event["warning_text"] = "此回答部分内容可能存在问题，请谨慎参考。"
            elif intervention.action == "degrade":
                quality_event["degrade_reason"] = "回答内容与检索来源不一致，已自动降级。"
                
            yield f"data: {json.dumps(quality_event)}\n\n"
        except Exception as e:
            logger.warning("流式质检异常: %s", e)
            # fail-open: 不推送质检事件
    ```
  - 异常保护：质检失败时不阻塞流式，跳过质检事件
  - 注意：`query_stream()` 是生成器方法，yield 模式需要正确处理

  **Parallelization**: NO (Wave 1, after Task 1)
  **Blocked By**: Task 1
  **Commit**: YES

---

- [x] 3. 流式质检测试

  **What to do**:
  - 创建 `tests/test_quality/test_streaming_quality.py`（或追加到 test_integration.py）
  - 覆盖场景：
    - `test_streaming_quality_passed`: 正常回答 → quality 事件 action="none"
    - `test_streaming_safety_blocked`: 安全违规 → quality 事件 action="block" + override_answer
    - `test_streaming_factuality_degraded`: 事实违规 → action="degrade"
    - `test_streaming_guard_disabled`: quality_guard_enabled=False → 无 quality 事件
  - 使用 MockLLMJudge，不调用真实 LLM
  - 模拟 `query_stream()` 生成器，验证 yield 的事件序列

  **Parallelization**: NO (Wave 1, after Task 2)
  **Blocked By**: Task 2
  **Commit**: YES

---

- [ ] 4. 前端 SSE 事件处理器扩展

  **What to do**:
  - 定位前端项目中处理 SSE 事件的代码（搜索 `EventSource` 或 `fetch` + `stream`）
  - 在事件分发器中新增 `case "quality":` 分支：
    ```
    收到 quality 事件 →
      根据 action 字段分发：
        "none"   → 显示 ✅ 质检通过标记
        "block"  → 替换当前回答为 override_answer，显示 🔴 拦截标识
        "warn"   → 在回答下方追加 ⚠️ 警告框
        "degrade" → 清空回答文本，仅保留来源，显示 🟡 降级标识
    ```
  - 状态管理：质检结果应作为独立的状态字段存储（与对话消息关联）
  - 国际化：所有 UI 文本使用中文

  **Parallelization**: NO (Wave 2, after Wave 1)
  **Blocked By**: Tasks 1, 2, 3
  **Commit**: YES

---

- [ ] 5. 前端质检展示 UI 组件

  **What to do**:
  - 创建质检展示组件（如 `QualityBadge.vue` 或集成到现有消息组件中）：
  - **拦截状态 🔴**：
    ```
    ┌─────────────────────────────────────┐
    │ 🔴 该回答已被安全策略自动拦截        │
    │    原因: 检测到违禁内容              │
    └─────────────────────────────────────┘
    ```
  - **警告状态 ⚠️**：
    ```
    ┌─────────────────────────────────────┐
    │ ⚠️ 事实校验提醒                     │
    │ 回答中部分信息与原文不一致           │
    └─────────────────────────────────────┘
    ```
  - **通过状态 ✅**（可收起）：
    ```
    ▶ ✅ 质检通过 (点击展开详情)
    │  安全 ✓ | 事实 ✓ | 检索 0.82 ✓ | 相关 ✓
    ```
  - 样式要求：不破坏现有对话布局，醒目但不刺眼
  - 状态切换：组件应能根据最新收到的 quality 事件切换状态

  **Parallelization**: NO (Wave 2, after Task 4)
  **Blocked By**: Task 4
  **Commit**: YES

---

- [ ] 6. 前端集成测试

  **What to do**:
  - 添加前端测试（根据前端项目使用的测试框架）
  - 覆盖：
    - 收到 quality pass 事件 → 显示 ✅ 标记
    - 收到 quality block 事件 → 回答被替换，显示 🔴
    - 收到 quality warn 事件 → 显示 ⚠️ 警告框
    - 未收到 quality 事件 → 不显示质检标记
    - 多次消息的质检状态独立

  **Parallelization**: NO (Wave 2, after Task 5)
  **Blocked By**: Task 5
  **Commit**: YES

---

- [ ] 7. 更新 `docs/RAG优化清单.md`

  **What to do**:
  - 追加 2026-06-02 流式质检联动优化记录，维持原文件格式
  - 记录改动的文件、新增的参数、SSE 事件格式变更
  - 更新关键参数速查表

  **Parallelization**: NO (Final, after all tasks)
  **Blocked By**: All Wave 1 + Wave 2 tasks
  **Commit**: YES (groups with Final Verification)

---

## Final Verification Wave

- [ ] F1. **端到端验证**
  - 启动后端 + 前端
  - 发送正常问题 → 验证 quality pass 事件
  - 发送安全问题 → 验证 quality block + 回答被替换
  - 发送事实挑战问题 → 验证 quality warn
  - 检查：不阻塞流式输出，回答先显示，质检结果后到达

- [ ] F2. **代码质量 + 范围审查**
  - 后端：Ruff + pytest 通过
  - 前端：构建无报错
  - 范围：无改动超出计划范围
  - 文档：更新 `docs/RAG优化清单.md`

---

## Commit Strategy

| Task | Message | Files |
|------|---------|-------|
| 1-2 | `feat(quality): add quality guard SSE event to streaming endpoint` | query_engine.py, schemas.py |
| 3 | `test(quality): add streaming quality integration tests` | test files |
| 4-5 | `feat(frontend): handle quality SSE event and display UI` | frontend files |
| 6 | `test(frontend): add quality UI component tests` | frontend tests |
| 7 | `docs: update RAG优化清单.md with streaming quality integration` | RAG优化清单.md |
