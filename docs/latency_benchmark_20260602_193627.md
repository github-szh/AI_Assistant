# QualityGuard 质检延迟基准测试报告

- **测试时间**: 2026-06-02 19:36:27
- **模式**: Mock（模拟）
- **采样次数**: 5
- **总耗时**: 1.49s
- **并行评估**: True

## 系统环境

- **Python 版本**: 3.14.4
- **操作系统**: Windows 11

## 各阶段延迟统计

| 阶段 | 采样数 | min(ms) | max(ms) | avg(ms) | p50(ms) | p95(ms) | p99(ms) |
|------|--------|---------|---------|---------|---------|---------|---------|
| 关键词预筛 | 5 | 14.97 | 1198.74 | 255.12 | 20.74 | 963.47 | 1151.69 |
| Safety LLM Judge | 5 | 20.24 | 1201.29 | 259.04 | 24.61 | 966.26 | 1154.28 |
| Factuality LLM Judge | 5 | 3.92 | 5.05 | 4.25 | 4.12 | 4.88 | 5.02 |
| Relevance LLM Judge | 5 | 0.15 | 1.16 | 0.37 | 0.20 | 0.97 | 1.12 |
| 干预执行 | 5 | 0.04 | 0.12 | 0.06 | 0.05 | 0.11 | 0.12 |
| 质检总附加时间 | 5 | 21.64 | 1202.84 | 260.51 | 26.00 | 967.81 | 1155.84 |

## 分析说明

- **关键词预筛**: KeywordFilter.prefilter() 的耗时，纯文本匹配，极低延迟。
- **Safety LLM Judge**: SafetyChecker.evaluate() 总耗时，包含关键词预筛 + 可选的 LLM 调用。
- **Factuality LLM Judge**: FactualityChecker.evaluate() 总耗时，包含 LLM 调用。
- **Relevance LLM Judge**: RelevanceChecker.evaluate() 总耗时，包含 LLM 调用。
- **干预执行**: InterventionEngine.run_all() 的耗时，纯逻辑计算。
- **质检总附加时间**: QualityGuard.run() 的总耗时，是衡量质检对响应时间影响的关键指标。

### Mock 模式说明

Mock 模式使用 MockLLMJudge 替代真实 LLM，测量的是编排开销而非 LLM 延迟。
各 Judge 的延迟反映的是 Mock 调用的响应速度（通常 < 1ms），
在真实场景中每次 LLM Judge 调用约 1-3 秒。

### 并行 vs 串行

并行模式下，质检总附加时间 ≈ 关键词预筛 + max(Safety, Factuality, Relevance) + 干预执行。
串行模式下，质检总附加时间 ≈ 各阶段耗时之和。