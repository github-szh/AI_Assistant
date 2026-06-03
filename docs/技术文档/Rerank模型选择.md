# Rerank 模型选择记录

> 2026-05-30，替换 Reranker 模型的调研、对比与决策过程。

---

## 背景

项目当前使用 **BGE-Reranker-large**（BAAI，0.33B 参数，~1.3GB）作为 Stage 2 精排器。模型需从 HuggingFace 下载，国内网络不可达。同时该模型是 BAAI 第一代 reranker，性能已被后续版本超越。

触发替换的几个原因：
1. HuggingFace 被墙，`data/models/BAAI/bge-reranker-large/` 目录不存在，模型无法加载
2. 存在更新、更强、ModelScope 可直连的替代方案
3. 同属 BAAI 系列，替换成本极低

---

## 候选模型全量对比

### 国内模型（ModelScope 可直连）

| 模型 | 出品方 | 参数 | 大小 | MTEB-R 中文 | 获取 |
|------|--------|------|------|------------|------|
| BGE-Reranker-large（当前） | 智源 BAAI | 0.33B | 1.3GB | 60~67 | ModelScope |
| BGE-Reranker-v2-m3 | 智源 BAAI | 0.6B | 1.2GB | 72.16 | ModelScope |
| GTE-multilingual-reranker | 阿里达摩院 | 0.3B | 1.2GB | 74.08 | ModelScope |
| BCE-Reranker-base | 网易有道 | 0.28B | 304MB | 61.29 | ModelScope |
| Qwen3-Reranker-0.6B | 阿里 | 0.6B | 1.2GB | 71.31 | ModelScope |
| Qwen3-Reranker-4B | 阿里 | 4B | 8GB | 75.94 | ModelScope |
| Qwen3-Reranker-8B | 阿里 | 8B | 16GB | 77.45 | ModelScope |
| 360Zhinao-Reranking | 奇虎360 | 1.8B | 3.6GB | 70.13 | HuggingFace |
| Piccolo-large-zh-v2 | — | 0.65B | 1.3GB | 70.00 | HuggingFace |

### 海外模型（API 服务）

| 模型 | 类型 | 中文精度 | 网络 | 价格 |
|------|------|---------|------|------|
| Cohere Rerank 4 | API | 高 | ❌ 被墙 | $0.05/M tokens |
| Jina Reranker v2 | API/本地 | 中等 | ❌ 不稳定 | 免费额度 |

---

## 关键维度分析

### 精度（中文 MTEB-R）

```
BCE (有道)        ████████░░░░  61
BGE-large (当前)  ████████░░░░  67
Piccolo           ████████░░░░  70
360Zhinao         ████████░░░░  70
Qwen3-0.6B        █████████░░░  71
BGE-v2-m3         █████████░░░  72
GTE (阿里达摩院)    ██████████░░  74
Qwen3-4B          ██████████░░  76
Qwen3-8B          ███████████░  77
```

### 替换成本（代码改动量）

| 模型 | 改动量 | 说明 |
|------|--------|------|
| BGE-v2-m3 | **1 行** | 同属 FlagEmbedding 生态，改模型名即可 |
| GTE | 半文件 | 需切到 `transformers.AutoModelForSequenceClassification` |
| Qwen3-0.6B | 半文件 | 同 GTE，需改推理代码 |
| 360Zhinao | 半文件 | 生成式模型，推理逻辑完全不同 |

### 许可证

全部为 **Apache 2.0**，免费商用无限制。360Zhinao 需额外邮件申请。

---

## 决策：BGE-Reranker-v2-m3

### 选型理由

1. **替换成本最低**：项目已使用 `FlagEmbedding` 库 + BGE 生态，只改一个模型名字符串，零代码逻辑变更
2. **精度提升显著**：MTEB-R 从 ~67 → 72.16，+5 分
3. **ModelScope 可直连**：国内下载无墙，已下载至 `data/models/BAAI/bge-reranker-v2-m3/`
4. **参数量合理**：0.6B / 1.2GB，CPU 可跑
5. **M3 架构**：支持多语言 + 混合检索，与项目已有的 BM25 + 向量混合检索配套

### 为什么没选 GTE

精度略高（74.08 vs 72.16，差 1.92 分），但需要重写加载逻辑并引入新的推理代码。不到 2 分的 MTEB-R 差距在实际 RAG 回答中感知不到，不值得为此增加代码复杂度。

### 为什么没选 Qwen3-4B

绝对 SOTA（75.94），但 8GB 模型需要 GPU，不合本项目部署环境。且 4B 参数的推理延迟对实时查询不够友好。

### 为什么没选 Cohere

精度最高 + 零运维是好，但 `api.cohere.ai` 在国内被墙，每次调用都不可达。

---

## 智谱 AI 调研结论

**智谱 AI 目前没有独立的 Reranker 模型。** 其产品线为：
- GLM-4 / GLM-5.1（LLM 对话）
- embedding-3（向量化，Bi-Encoder，不能替代 Cross-Encoder Reranker）

曾被认为是智谱的 **bce-reranker-base_v1**，经核实由网易有道开发，非智谱产品。

---

## 实际替换

### 模型下载

```bash
# 从 ModelScope 下载
python -c "
from modelscope import snapshot_download
snapshot_download('BAAI/bge-reranker-v2-m3', cache_dir='data/models/BAAI/bge-reranker-v2-m3')
"
```

模型大小：2.12GB（safetensors），下载至 `data/models/BAAI/bge-reranker-v2-m3/`

### 代码修改

`src/knowledge/reranker.py` 第 17-18 行：

```python
# 改前
_RERANKER_LOCAL = os.path.join(_PROJECT_ROOT, "data", "models", "BAAI", "bge-reranker-large")
_RERANKER_DEFAULT = "BAAI/bge-reranker-large"

# 改后
_RERANKER_LOCAL = os.path.join(_PROJECT_ROOT, "data", "models", "BAAI", "bge-reranker-v2-m3")
_RERANKER_DEFAULT = "BAAI/bge-reranker-v2-m3"
```

仅模型名变更，推理代码（`FlagEmbedding.FlagReranker`）完全兼容。

### 验证

```
查询："年假有几天？"
候选 1："公司规定员工每年享有5天带薪年假" → 0.723 ✅
候选 2："年假期间工资正常发放"            → 相关
候选 3："财务报表显示Q1营收增长15%"       → 不相关（被过滤）
```

模型正常加载，精排结果正确。
