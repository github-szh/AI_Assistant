"""事实准确性校验脚本 — 模拟真实场景中的Ground Truth检查"""
import json, os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_json(path):
    with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(os.path.join(PROJECT_ROOT, path), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def accuracy_check(answer, expected_keywords):
    """检查回答是否包含了预期关键词（即是否答对了）"""
    if not expected_keywords:
        return {"expected_keywords": [], "matched_keywords": [], "accuracy_ratio": 1.0, "verdict": "PASS: no keywords to check", "passed": True}
    matched = [kw for kw in expected_keywords if kw in answer]
    ratio = len(matched) / len(expected_keywords)
    if ratio < 0.3:
        verdict = "FAIL: 回答与事实不符，关键信息缺失或错误"
        passed = False
    elif ratio < 0.7:
        verdict = "PARTIAL: 部分关键词匹配，但核心信息不完整"
        passed = False
    else:
        verdict = "PASS: 回答与事实一致"
        passed = True
    return {
        "expected_keywords": expected_keywords,
        "matched_keywords": matched,
        "accuracy_ratio": round(ratio, 2),
        "verdict": verdict,
        "passed": passed
    }

# 加载评估数据集
data = load_json("tests/test_data/eval_dataset.json")
dataset = data["dataset"]

# 模拟噪声文档导致的错误回答
WRONG_ANSWERS = {
    "eval_001": "平台支持10多种文档格式，包括PDF、DOCX、PPTX、EPUB、MOBI、TXT等格式，图片OCR使用OCRmyPDF引擎",
    "eval_002": "粗筛top_k=50，精排保留top_k=3，使用RRF参数k=40进行融合",
    "eval_003": "系统使用PostgreSQL 15作为主数据库，Redis 6用于缓存，MinIO RELEASE.2023-01存储",
    "eval_004": "最低配置：Python 3.8，4GB内存，20GB磁盘，Docker Compose v1",
    "eval_005": "前端使用React 17 + Webpack 5 + Redux，后端使用Flask框架",
    "eval_006": "上传接口GET /upload，查询接口POST /api/v1/ask，返回XML格式",
    "eval_007": "当置信度低于0.5时触发HyDE策略，生成假设性回答辅助检索",
    "eval_008": "系统支持DeepSeek-V4-Base、GLM-4和GPT-3.5-turbo，建议优先使用OpenAI",
    "eval_009": "向量维度为1536维，使用OpenAI text-embedding-ada-002嵌入模型",
    "eval_010": "重排序使用BAAI/bge-reranker-base，Cohere Rerank API作为备选",
}

results = []
for idx, item in enumerate(dataset):
    item_id = item["id"]
    expected_kw = item.get("expected_answer_keywords", [])

    if idx < 10:
        # 正常项：模拟噪声引发的错误回答
        answer = WRONG_ANSWERS.get(item_id, "")
        acc = accuracy_check(answer, expected_kw)
        factuality = {"passed": acc["passed"], "score": acc["accuracy_ratio"], "details": acc["verdict"]}
        safety = {"passed": True, "score": 1.0, "details": "safety: passed"}
        relevance = {"passed": True, "score": 0.85, "details": "relevance: passed"}
        retrieval_quality = {"passed": True, "score": 0.7, "details": "retrieval_quality: passed"}

    elif idx < 20:
        # 安全对抗项：不变
        answer = ""
        acc = accuracy_check(answer, expected_kw)
        factuality = {"passed": True, "score": 0.9, "details": "factuality: passed"}
        safety = {"passed": False, "score": 0.0, "details": "safety: failed (adversarial)"}
        relevance = {"passed": True, "score": 0.85, "details": "relevance: passed"}
        retrieval_quality = {"passed": True, "score": 0.8, "details": "retrieval_quality: passed"}

    else:
        # 事实挑战项
        answer = ""
        acc = accuracy_check(answer, expected_kw)
        factuality = {"passed": False, "score": 0.2, "details": "factuality: challenged"}
        safety = {"passed": True, "score": 1.0, "details": "safety: passed"}
        relevance = {"passed": True, "score": 0.85, "details": "relevance: passed"}
        retrieval_quality = {"passed": True, "score": 0.8, "details": "retrieval_quality: passed"}

    results.append({
        "id": item_id,
        "query": item["question"],
        "answer": answer,
        "context": item["reference_context"],
        "safety": safety,
        "factuality": factuality,
        "relevance": relevance,
        "retrieval_quality": retrieval_quality,
        "retrieved_sources": [],
        "noise_injected": True,
        "accuracy_check": acc
    })

save_json("data/accuracy-eval-results.json", results)

# ====== 打印对比报告 ======
print("=" * 70)
print("事实准确性校验结果 — 一致性 vs 准确性 对比")
print("=" * 70)

old = load_json("data/noise-eval-results.json")
print(f"\n{'评估项':<15} {'旧(一致性分)':<15} {'新(准确性分)':<15} {'匹配情况'}")
print("-" * 60)

for i in range(10):
    rid = results[i]["id"]
    old_score = old[i]["factuality"]["score"]
    new_score = results[i]["factuality"]["score"]
    acc = results[i]["accuracy_check"]
    matched_str = ",".join(acc["matched_keywords"]) if acc["matched_keywords"] else "(无)"
    print(f"{rid:<15} {old_score:<15} {new_score:<15} 匹配[{acc['accuracy_ratio']}] {matched_str}")

print(f"\n结论:")
print(f"  - 一致性检查平均分（旧）: {sum(old[i]['factuality']['score'] for i in range(10))/10:.2f}")
print(f"  - 准确性检查平均分（新）: {sum(results[i]['factuality']['score'] for i in range(10))/10:.2f}")
print(f"  - 平均分差: {sum(old[i]['factuality']['score']-results[i]['factuality']['score'] for i in range(10))/10:.2f}")
print(f"  - 安全项未受影响: {'通过' if all(results[10+i]['safety']==old[10+i]['safety'] for i in range(10)) else '失败'}")
print(f"\n结果已保存到: data/accuracy-eval-results.json")
