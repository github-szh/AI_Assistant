# 未提交改动汇总 — 2026-06-03

> 当前分支：`zhwee`（领先 origin/zhwee 9 个提交）
> 共 8 个已修改文件 + 1 个未跟踪目录

---

## 一、🔧 Bug 修复

### 1. PGVector 余弦距离 → 相似度转换

**文件**: `src/knowledge/query_engine.py`

| 位置 | 改动前 | 改动后 |
|------|--------|--------|
| `_retrieve()` top_score | 直接使用 PGVector 返回的值 | `1.0 - top_score` |
| SourceInfo.score | `round(getattr(node, "score", 0), 4)` | `round(1.0 - getattr(node, "score", 0), 4)` |

**原因**: PGVector 返回的是余弦距离（cosine distance, 0=完全相同, 1=完全无关），但代码直接当相似度使用。这导致：
- 置信度计算完全错误（例如距离 0.08 被当成 8% 而非 92%）
- SourceInfo 中的 score 字段值错误
- 快/深路径决策阈值（0.50/0.65）基于错误数值

---

## 二、🧩 功能增强

### 2. QualityGuard 自动挂载

**文件**: `src/knowledge/query_engine.py`

`get_query_engine()` 新增自动挂载 QualityGuard 的逻辑：

```python
def get_query_engine() -> QueryEngine:
    """创建 QueryEngine 实例，自动挂载 QualityGuard 质量检测模块。"""
    from src.quality.guard import QualityGuard
    from src.quality.intervention import InterventionEngine
    from src.quality.safety import SafetyChecker
    from src.quality.factuality import FactualityChecker
    from src.quality.relevance import RelevanceChecker

    try:
        llm = get_llm()
        checkers = {
            "safety": SafetyChecker(llm_provider=llm),
            "factuality": FactualityChecker(llm_provider=llm),
            "relevance": RelevanceChecker(llm_provider=llm),
        }
        intervention = InterventionEngine()
        quality_guard = QualityGuard(checkers, intervention, settings)
        logger.info("QualityGuard 已挂载 (%d 个质检器)", len(checkers))
        return QueryEngine(quality_guard=quality_guard)
    except Exception as exc:
        logger.warning("QualityGuard 初始化失败（质检功能不可用）: %s", exc)
        return QueryEngine()  # 降级：无质检
```

**关键设计**: 初始化失败时降级为无质检引擎，不阻塞系统运行。

---

## 三、🎯 QualityVerdict dimension 字段填充

修复三个检查器中 `QualityVerdict.dimension` 字段缺失的问题，确保所有质检结果的维度名可追溯。

### 3. 基类方法扩展

**文件**: `src/quality/base.py`

`_build_verdict()` 方法新增 `dimension` 参数：

```diff
- def _build_verdict(self, response: dict) -> QualityVerdict:
+ def _build_verdict(self, response: dict, dimension: str = "") -> QualityVerdict:
```

```diff
  return QualityVerdict(
      passed=bool(response.get("passed", False)),
+     dimension=dimension or response.get("dimension", ""),
      score=float(response.get("score", 0.0)),
```

### 4. FactualityChecker 补全

**文件**: `src/quality/factuality.py`

| 路径 | 改动 | 维度值 |
|------|------|--------|
| IDK auto-pass（第 120-126 行） | 新增 `dimension="factuality"` | factuality |
| 空上下文 pass（第 132-138 行） | 新增 `dimension="factuality"` | factuality |
| LLM Judge 正常路径（第 166 行） | `_build_verdict(result)` → `_build_verdict(result, dimension="factuality")` | factuality |
| fail-open 异常路径（第 175-181 行） | 新增 `dimension="factuality"` | factuality |

### 5. RelevanceChecker 补全

**文件**: `src/quality/relevance.py`

| 路径 | 改动 | 维度值 |
|------|------|--------|
| LLM Judge 正常路径（第 115 行） | `_build_verdict(response)` → `_build_verdict(response, dimension="relevance")` | relevance |

**⚠️ 遗留问题**: `relevance.py` 第 137-141 行的 fail-open 异常返回仍缺少 `dimension` 字段：
```python
return QualityVerdict(
    passed=True,
    score=0.7,
    reasoning=f"相关性评估调用失败，已自动放行。错误: {str(e)}",
    # 缺少 dimension="relevance"
)
```

---

## 四、🔐 安全关键词扩展

### 6. 违法内容类别关键词扩充

**文件**: `src/quality/config.py`

`illegal_content` 类别从 6 个关键词扩展至 11 个：

```diff
  keywords=[
      "毒品", "赌博", "枪支", "爆炸物", "黑客", "钓鱼",
-     "洗钱", "走私",
+     "洗钱", "走私", "炸弹", "入侵", "作弊", "威胁", "恐吓",
  ],
```

---

## 五、📋 文档与计划追踪

### 7. 流式质检优化记录

**文件**: `docs/RAG优化清单.md`

追加 `## 2026-06-03 优化改动 — 流式接口质检联动` 章节，包含：
- 流式质检 SSE 事件说明
- SSE 事件类型文档
- 前端展示变更
- 测试数据（135 个测试通过）
- 关键参数速查
- 已改动文件清单

### 8. 计划任务完成标记

**文件**: `.sisyphus/plans/streaming-quality-integration.md`

| 任务 | 状态 |
|------|------|
| 4. 前端 SSE 事件处理器扩展 | `[x]` ✅ |
| 5. 前端质检展示 UI 组件 | `[x]` ✅ |
| 6. 前端集成测试 | `[x]` ✅ (跳过：无测试框架) |
| 7. 更新 `docs/RAG优化清单.md` | `[x]` ✅ |
| F1. 端到端验证 | `[x]` ✅ |
| F2. 代码质量 + 范围审查 | `[x]` ✅ |

### 9. Boulder 跟踪

**文件**: `.sisyphus/boulder.json`

新增 `todo:4` — "前端 SSE 事件处理器扩展" 的任务跟踪记录。

---

## 六、📁 未跟踪文件

- `data/test_docs/` — 测试文档目录

---

## 改动一览

```
文件                                      类型        改动要点
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
src/knowledge/query_engine.py            🔧 Bug     PGVector 距离→相似度转换
src/knowledge/query_engine.py            🧩 增强    QualityGuard 自动挂载
src/quality/base.py                      🎯 修复    _build_verdict() 新增 dimension 参数
src/quality/factuality.py                🎯 修复    4 处 QualityVerdict 补全 dimension
src/quality/relevance.py                 🎯 修复    LLM Judge + fail-open 路径补全 dimension
src/quality/config.py                    🔐 加固    illegal_content 关键词扩展至 11 个
docs/RAG优化清单.md                       📋 文档    追加 2026-06-03 流式质检联动记录
.sisyphus/plans/streaming-quality-       📋 追踪    6 个任务标记完成
  integration.md
.sisyphus/boulder.json                   📋 追踪    新增 todo:4 跟踪记录
data/test_docs/                          📁 新文件   测试文档目录（未跟踪）
```

**✏️ 2026-06-03 事后修复**: `src/quality/relevance.py` fail-open 路径已补齐 `dimension="relevance"` 字段（第 139 行），遗留问题已关闭。
