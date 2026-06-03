# 修复：RelevanceChecker fail-open 路径缺失 dimension 字段 ✅ 已完成

## TL;DR
> **目标**: 为 `src/quality/relevance.py` 第 137 行的 fail-open 返回补齐 `dimension="relevance"` 字段
> 
> **改动量**: 1 行，+1 个参数
> 
> **风险**: 极低（仅修改默认值，不影响逻辑）

---

## - [x] 改动内容 (完成)

### 改动

**文件**: `src/quality/relevance.py` 第 137-141 行

当前代码：
```python
return QualityVerdict(
    passed=True,
    score=0.7,  # 默认中等偏上分数（保守放行）
    reasoning=f"相关性评估调用失败，已自动放行。错误: {str(e)}",
)
```

改为：
```python
return QualityVerdict(
    passed=True,
    dimension="relevance",   # ← 新增这一行
    score=0.7,
    reasoning=f"相关性评估调用失败，已自动放行。错误: {str(e)}",
)
```

---

## 验证方法

修复后运行测试确认不破坏现有逻辑：

```bash
pytest tests/test_quality/test_relevance_checker.py -v
```

预期：全部测试通过（此修改不改变任何行为，仅补齐数据字段）。

---

## 事后验证

运行离线评测脚本，确认 fail-open 路径下的 verdict 不再出现空 dimension：

```bash
python scripts/run_eval.py --verbose
```

检查日志中不再有 `dimension=""` 的报警即可。
