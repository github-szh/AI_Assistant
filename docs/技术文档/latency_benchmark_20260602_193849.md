# QualityGuard 质检延迟基准测试报告

- **测试时间**: 2026-06-02 19:38:49
- **模式**: Mock（模拟）
- **采样次数**: 10
- **总耗时**: 1.69s
- **并行评估**: True

## 系统环境

- **Python 版本**: 3.14.4
- **操作系统**: Windows 11

## 各阶段延迟统计

| 阶段 | 采样数 | min(ms) | max(ms) | avg(ms) | p50(ms) | p95(ms) | p99(ms) |
|------|--------|---------|---------|---------|---------|---------|---------|
| 关键词预筛 | 10 | 16.81 | 39.61 | 27.12 | 26.45 | 37.53 | 39.20 |
| Safety LLM Judge | 10 | 20.61 | 43.50 | 31.57 | 31.20 | 41.92 | 43.18 |
| Factuality LLM Judge | 10 | 3.47 | 14.30 | 9.88 | 10.29 | 13.82 | 14.20 |
| Relevance LLM Judge | 10 | 0.15 | 0.22 | 0.19 | 0.19 | 0.22 | 0.22 |
| 干预执行 | 10 | 0.04 | 0.07 | 0.05 | 0.05 | 0.07 | 0.07 |
| 质检总附加时间 | 10 | 22.06 | 45.68 | 33.04 | 32.76 | 43.73 | 45.29 |

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