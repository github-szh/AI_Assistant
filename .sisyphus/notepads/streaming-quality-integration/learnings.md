# Learnings - Streaming Quality Integration

## 2026-06-03 Session Start

### Context
- Frontend project: `../ai-assistant-web/` (Vite + Vue 3)
- Key files: ChatView.vue (SSE handling), api/index.ts (API calls)
- Need to understand current SSE event processing before implementing

### Execution Plan
Wave 1: Tasks 1→2→3 (backend, serial)
Wave 2: Tasks 4→5→6 (frontend, serial, after Wave 1)
Final: Task 7 + F1 + F2

### Task 3 Done - Streaming Quality Tests

**File created**: `tests/test_quality/test_streaming_quality.py`

**4 tests implemented**:
1. `test_streaming_quality_passed` — mock guard returns `action="none"`, verify quality event fields
2. `test_streaming_safety_blocked` — mock guard returns `action="block"`, verify `override_answer` present
3. `test_streaming_factuality_degraded` — mock guard returns `action="degrade"`, verify `degrade_reason` present
4. `test_streaming_guard_disabled` — set `quality_guard_enabled=False`, verify no quality event + guard.run() not called

**Key patterns**:
- Mock `quality_guard.run()` to return `(dict, InterventionInfo)` tuple
- Mock `get_llm().chat_stream` to return `iter(chunks)` (iterator over strings)
- Use `patch.object(engine, "_retrieve")` + `patch("src.knowledge.query_engine.get_llm")` + `patch("src.knowledge.query_engine.settings")` context managers
- Parse SSE `data: {json}\n\n` format via `_parse_sse_events()` helper
- Verify event ordering: chunks → quality → done

**All 4 tests pass** (verified with pytest).**
