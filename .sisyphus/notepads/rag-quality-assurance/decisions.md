# Decisions - RAG Quality Assurance

## 2026-06-02

| Decision | Option Chosen | Rationale |
|----------|--------------|-----------|
| Judge model | Separate from generation (cross-evaluation) | Avoids self-enhancement bias |
| Safety fail behavior | Fail-closed (block if unsure) | Safety is top priority |
| Others fail behavior | Fail-open (allow with warning) | Better UX for non-safety issues |
| Eval dimensions | safety, factuality, relevance, retrieval_quality | Comprehensive coverage |
| Custom categories | Keywords + semantic description (both) | Two-stage: fast pre-filter + deep LLM |
| Intervention priority | Safety > Factuality > Retrieval > Relevance | Risk-based ordering |
| QualityVerdict.score range | `ge=0.0, le=1.0` via Pydantic Field | Enforce bounds at schema level |
| InterventionInfo.action | Regex `pattern` constraint: `^(none\|block\|rewrite\|warn\|degrade)$` | Prevent invalid action values at serialization |
| quality field | Optional (`None` = no QC) | Backward compatible with existing clients |
| Response shape comment | Documented in `docs/rag-quality-schema.md` with JSON examples | Team reference for frontend integration |
| Email regex pattern | Added to personal_info category | Extends regex coverage beyond ID/phone |
| Regex \b replacement | `(?<!\d)/(?!\d)` instead of `\b` | Python 3 `\w` is Unicode-aware, Chinese chars treated as word chars so `\b` doesn't work |
| Chinese keyword matching | Substring + jieba token-level (dual path) | Substring catches most cases; jieba adds token-aware matching for edge cases |
| Rule matching strategy | `verdict.dimension` → `_DIMENSION_TO_PREFIX` → `rule.violation_type.startswith(prefix)` | Decouples schema dimension names from rule naming conventions (e.g., "retrieval_quality" → "retrieval_*") |
| Dimension priority | Defined in `_DIMENSION_PRIORITY` dict, not inferred from rule priority | Rules can have different priorities than dimension ordering; verdicts sorted by dimension priority first, then matched against sorted rules |
| execute() response shape | Always returns `{answer, sources, quality}` dict with `quality` from `InterventionInfo.model_dump()` | Ensures consistent response structure regardless of action; QueryEngine can flatten or pass through |
| `rules` default | `None` → uses `get_default_intervention_rules()`, empty list `[]` → no rules | `None` vs `[]` distinction allows explicit disabling of all rules |

## F4: Scope Fidelity Check (2026-06-02)

**Verdict: APPROVE ? �� Zero scope creep detected**

Key findings:
- All 33 files changed in quality commits match plan deliverables exactly
- 6 false positives in HEAD~10..HEAD diff were pre-quality modifications (not scope creep)
- Must NOT do rules all pass: no frontend, no streaming, no existing code modification, no dashboard
- query_engine.py changes limited to __init__ param + query() hook only
- Test files exist on disk (tests/test_quality/) but are gitignored (tests/* in .gitignore)

