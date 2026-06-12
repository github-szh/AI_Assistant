# 安全关键词预筛模块（KeywordFilter）

## 概述

`KeywordFilter` 是 RAG 质量保障体系中安全评估的**第一阶段（快速预筛）**。它在不调用 LLM 的情况下，通过关键词和正则表达式对输入文本进行快速扫描，命中关键词时直接判定违规，避免不必要的 LLM 调用开销。

### 工作流程

```
输入文本
    │
    ▼
┌─────────────────────┐
│  KeywordFilter      │  ← 第一阶段：快速预筛（本模块）
│  关键词/正则匹配     │
└─────────┬───────────┘
          │
     ┌────┴────┐
     ▼         ▼
   命中       不命中
     │         │
     │         ▼
     │   ┌─────────────────┐
     │   │  SafetyChecker  │  ← 第二阶段：LLM 深度评估
     │   │  (LLM Judge)    │
     │   └────────┬────────┘
     │            │
     ▼            ▼
  违规判定      合规放行
```

### 设计原则

- **Fail-closed**：预筛阶段不放行疑似违规内容，宁可误报不可漏报
- **高性能**：纯字符串匹配 + 正则，单次扫描 O(n × k)，< 100 关键词无需 Aho-Corasick
- **无外部依赖**：jieba 为可选依赖，不安装时退化为纯子串匹配
- **热加载**：关键词列表支持运行时 reload，无需重启服务

---

## 类与方法

### `KeywordFilter`

#### `__init__(categories=None, yaml_path=None)`

初始化加载顺序：

1. 内置默认安全类别（`get_default_safety_categories()`）
2. 合并自定义类别（`categories` 参数）
3. 合并 YAML 文件中定义的类别（`yaml_path` 参数）

```python
from src.quality import KeywordFilter

# 仅使用内置类别
filter = KeywordFilter()

# 合并自定义类别
custom_cats = [SafetyCategory(name="custom", keywords=["kw1"], description="")]
filter = KeywordFilter(categories=custom_cats)

# 从 YAML 加载
filter = KeywordFilter(yaml_path="config/safety_categories.yaml")
```

#### `prefilter(text) -> list[KeywordMatch]`

对输入文本执行关键词预筛，返回所有匹配结果。

**匹配方式：**

| 方式 | 实现 | 说明 |
|------|------|------|
| 精确匹配 | `str.find()` | 子串匹配，找到所有出现位置 |
| 正则匹配 | `re.finditer()` | 从 `SafetyCategory.regex_patterns` 获取模式 |
| 中文分词兼容 | jieba 分词后 token 级连续匹配 | 仅对含中文的关键词生效 |

**返回字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `category` | str | 匹配到的安全分类名称 |
| `keyword` | str | 匹配到的具体关键词或正则模式字符串 |
| `position` | int | 在文本中的字符偏移量 |
| `match_type` | Literal["exact", "regex"] | 匹配方式 |

```python
filter = KeywordFilter()
matches = filter.prefilter("这个视频包含暴力内容")

for m in matches:
    print(f"[{m.category}] '{m.keyword}' @ {m.position} ({m.match_type})")
# 输出: [harmful_content] '暴力' @ 6 (exact)
```

#### `add_category(category: SafetyCategory)`

动态添加自定义安全类别。同名类别已存在时跳过（记录 warning）。

```python
custom = SafetyCategory(
    name="custom_pii",
    keywords=["公司机密", "内部文件"],
    regex_patterns=[r"\bconfidential\b"],
    description="自定义 PII 类别",
)
filter.add_category(custom)
```

#### `get_categories() -> list[SafetyCategory]`

返回当前加载的所有安全类别（防御性拷贝）。

#### `reload()`

从内置默认配置重新加载类别列表。通过 `add_category` 添加的自定义类别会丢失。

---

## 内置类别与关键词

| 类别名 | 关键词数 | 正则数 | 说明 |
|--------|----------|--------|------|
| `harmful_content` | 10 | 0 | 暴力、色情、仇恨言论、恐怖主义等 |
| `prompt_injection` | 7 | 2 | 提示注入攻击，中英文模式 |
| `personal_info` | 8 | 3 | 身份证号、手机号、邮箱等 |
| `sensitive_topic` | 7 | 0 | 政治敏感、宗教极端、领土争端等 |
| `illegal_content` | 8 | 0 | 毒品、赌博、枪支、洗钱等 |
| `misinformation` | 7 | 0 | 谣言、阴谋论、深度伪造等 |

### 正则模式详情

**个人信息（personal_info）：**

| 模式 | 说明 |
|------|------|
| `(?<!\d)\d{17}[\dXx](?!\d)` | 18 位大陆身份证号 |
| `(?<!\d)1[3-9]\d{9}(?!\d)` | 11 位手机号 |
| `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}` | 邮箱地址 |

> **注意**：使用 `(?<!\d)/(?!\d)` 替代 `\b`，因为 Python 3 中 `re` 模块的 `\b`/`\w` 是 Unicode 感知的，会错误地将中文汉字视为单词字符。

**提示注入（prompt_injection）：**

| 模式 | 说明 |
|------|------|
| `(?i)(ignore\|forget\|disregard)\s+(all\s+)?(above\|previous\|instructions)` | 英文注入模式 |
| `(?i)(不要\|无需\|不必)(遵循\|遵守\|理会)(.*?)(规则\|指令\|限制)` | 中文注入模式 |

---

## 中文兼容性

对于包含中文字符的关键词，`KeywordFilter` 在子串匹配之外额外执行 jieba 分词匹配：

1. 将关键词和文本分别进行 jieba 分词
2. 在文本的 token 序列中查找与关键词 token 序列连续匹配的位置
3. 记录第一个匹配 token 的起始位置

**示例：**

```python
# 关键词 "恐怖袭击"
# jieba 分词 → ["恐怖", "袭击"]
# 文本 "他们策划了一起恐怖袭击活动"
# jieba 分词 → ["他们", "策划", "了", "一起", "恐怖", "袭击", "活动"]
# token 序列 ["恐怖", "袭击"] 在位置 4（字符偏移）处连续匹配
```

> **注意**：jieba 为可选依赖。未安装时，中文关键词仅使用子串匹配（`str.find()`），绝大多数场景下已足够。

---

## 性能说明

- **关键词总量 < 100 条**：使用线性扫描（`str.find()` + `re.finditer()`），无需 Aho-Corasick 等高级算法
- **每次 prefilter 扫描**：O(n × k)，其中 n = 文本长度，k = 关键词总数
- **正则表达式**：每次调用时即时编译（关键词总量少，编译开销可忽略）
- **jieba 分词**：仅在关键词含中文且 jieba 可用时触发，单次分词耗时约 1-5ms

---

## 配置示例（YAML）

```yaml
safety_categories:
  - name: custom_pii
    keywords:
      - 公司机密
      - 内部文件
    regex_patterns:
      - "(?i)(confidential|proprietary)"
    description: 自定义公司敏感信息类别

  - name: custom_policy
    keywords:
      - 政策违规
      - 合规风险
    regex_patterns: []
    description: 自定义合规风控类别
```

---

## 与 SafetyChecker 的关系

```
KeywordFilter (预筛)       SafetyChecker (深度评估)
─────────────────          ─────────────────────
规则驱动                     LLM 驱动
快速（毫秒级）                慢速（秒级）
无幻觉风险                    有幻觉风险
误报率较高                    误报率较低
100% 覆盖关键词               语义理解
```

两阶段互补：KeywordFilter 拦截已知的明确违规内容，SafetyChecker 处理需要语义理解的模糊场景。
