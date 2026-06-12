# RAG Quality Schema

> 质检模块的 Pydantic 数据模型文档。
> 定义位置：`src/api/schemas.py`
> 版本：v1.0

---

## 模型一览

```
QualityVerdict        ← 单个质检维度的判定结果
    ↓ 被组合为 list
InterventionInfo      ← 干预引擎的决策信息
    ↓ 被嵌入为可选字段
QueryResponse         ← RAG 查询响应（向后兼容）
```

---

## QualityVerdict

单个质检维度的判定结果。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `dimension` | `str` | — | 质检维度。可选值：`safety` / `factuality` / `relevance` / `retrieval_quality` |
| `passed` | `bool` | — | 是否通过该维度检查 |
| `score` | `float` | `ge=0.0, le=1.0` | 得分，0.0 ~ 1.0 |
| `details` | `str` | `default=""` | 详细说明 / 违规原因 |

### 维度说明

| 维度 | 含义 | 判定标准 |
|------|------|----------|
| `safety` | 内容安全 | 是否包含有害、偏见、违规内容 |
| `factuality` | 事实性 | 是否与检索到的文档事实一致 |
| `relevance` | 相关性 | 回答是否对用户问题有帮助 |
| `retrieval_quality` | 检索质量 | 检索到的文档是否与问题相关 |

---

## InterventionInfo

干预引擎的决策信息。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `intervened` | `bool` | — | 是否被干预（`false` = 正常通过） |
| `action` | `str` | `pattern="^(none\|block\|rewrite\|warn\|degrade)$"` | 执行的动作 |
| `reason` | `str` | `default=""` | 干预原因 |
| `violations` | `list[QualityVerdict]` | `default_factory=list` | 所有维度的判定详情 |

### Action 说明

| 动作 | 含义 | 行为 |
|------|------|------|
| `none` | 不干预 | 正常返回回答 |
| `block` | 阻断 | 不返回回答内容，仅展示安全提示 |
| `rewrite` | 重写 | AI 重写回答以符合规范 |
| `warn` | 警告 | 返回回答并附带警告信息 |
| `degrade` | 降级 | 返回简短/空回答，但保留来源 |

---

## QueryResponse（扩展后）

RAG 查询响应。`quality` 为新增可选字段。

| 字段 | 类型 | 说明 |
|------|------|------|
| `answer` | `str` | 回答文本 |
| `sources` | `list[SourceInfo]` | 引用来源列表 |
| `quality` | `InterventionInfo \| None` | **新增**。质检结果，`None` = 未启用质检（向后兼容） |

---

## 响应形状示例

### 正常（无质检 / 质检通过）

```json
{
  "answer": "根据文档，Python 是一种解释型语言…",
  "sources": [
    {"doc_id": "doc1", "filename": "python_intro.pdf", "chunk_index": 3, "score": 0.92, "snippet": "Python is an interpreted..."}
  ],
  "quality": {
    "intervened": false,
    "action": "none",
    "reason": "",
    "violations": []
  }
}
```

### BLOCK — 安全性阻断

```json
{
  "answer": "抱歉，根据内容安全策略，无法展示此回答。",
  "sources": [],
  "quality": {
    "intervened": true,
    "action": "block",
    "reason": "检测到内容安全违规",
    "violations": [
      {"dimension": "safety", "passed": false, "score": 0.0, "details": "检测到有害内容"}
    ]
  }
}
```

### WARN — 伴随警告返回

```json
{
  "answer": "根据文档内容…",
  "sources": [
    {"doc_id": "doc1", "filename": "report.pdf", "chunk_index": 5, "score": 0.65, "snippet": "..."}
  ],
  "quality": {
    "intervened": true,
    "action": "warn",
    "reason": "部分事实无法验证，请核实",
    "violations": [
      {"dimension": "factuality", "passed": false, "score": 0.4, "details": "回答中的部分数据与检索文档不一致"}
    ]
  }
}
```

### DEGRADE — 降级（空回答，保留来源）

```json
{
  "answer": "",
  "sources": [
    {"doc_id": "doc2", "filename": "data.pdf", "chunk_index": 1, "score": 0.35, "snippet": "..."}
  ],
  "quality": {
    "intervened": true,
    "action": "degrade",
    "reason": "检索质量不足，无法生成可靠回答",
    "violations": [
      {"dimension": "retrieval_quality", "passed": false, "score": 0.3, "details": "检索结果相关性低，最高分仅 0.35"}
    ]
  }
}
```

### 向后兼容（旧版本客户端）

```json
{
  "answer": "根据文档...",
  "sources": [...]
  // quality 字段不存在（None 在序列化时被 exclude_none 排除）
}
```

---

## 开发指南

### 创建质检结果

```python
from src.api.schemas import QualityVerdict, InterventionInfo, QueryResponse

# 1. 构造各维度判定
verdicts = [
    QualityVerdict(dimension="safety", passed=True, score=0.98, details="无安全问题"),
    QualityVerdict(dimension="factuality", passed=False, score=0.45, details="部分事实不一致"),
]

# 2. 构造干预决策
intervention = InterventionInfo(
    intervened=True,
    action="warn",
    reason="事实性存疑",
    violations=verdicts,
)

# 3. 嵌入响应
response = QueryResponse(
    answer="一些回答...",
    sources=[...],
    quality=intervention,
)
```

### 序列化说明

- 使用 `model_dump()` 而非 `dict()`
- 使用 `model_dump_json()` 而非 `json()`
- 向后兼容场景建议使用 `model_dump(exclude_none=True)` 以省略 `quality=None`

---

## 变更记录

| 日期 | 变更内容 |
|------|----------|
| 2026-06-02 | 初始版本。新增 QualityVerdict、InterventionInfo，扩展 QueryResponse |
