# Learnings - Quality Stream Integration (Task 2)

## query_stream() 质检集成要点

- **chunk 收集方式**: 在流式循环前初始化 `_stream_chunks: list[str]`，在循环中 `append(chunk)` 收集每个 token，质检时 `"".join(_stream_chunks)` 得到完整回答。
- **quality 事件位置**: 必须在 `{"done": True}` 之前 yield，因为前端收到 done 事件后停止读取 SSE。
- **fail-open 策略**: 质检异常时用 `try/except` 捕获，记录 `logger.warning`，不 yield quality 事件，然后继续 yield `{"done": True}`。
- **QualityGuard 不可用**: 通过 `self.quality_guard is not None and settings.quality_guard_enabled` 双重检查跳过。
- **原有事件流不变**: steps → status → sources → c chunks → (quality) → done — 不改变已有 SSE 事件格式和顺序。

## Schema 结构

- `InterventionInfo`: intervened(bool), action(str: none/block/warn/degrade), reason(str), violations(list[QualityVerdict])
- `QualityVerdict`: dimension(str), passed(bool), score(float 0-1), details(str)
