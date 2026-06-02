# Issues - RAG Quality Assurance

## 2026-06-02

### Known Issues
- No test infrastructure exists yet (need to create from scratch)
- src/utils/prompt_loader.py may not support subdirectories (verify during Task 4)
- Mock LLM in src/llm/providers/mock.py exists but may need extension for quality judge use
- Streaming path (query_stream) is explicitly NOT covered by quality guard
