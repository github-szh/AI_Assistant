"""
Noise Evaluation Script

Reads eval_dataset.json and produces a simulated evaluation output
to data/noise-eval-results.json, simulating the effect of noise
documents injected into the RAG system.

Simulation logic:
  - Items 0-9 (normal, eval_001-010): Factuality scores degraded by noise docs
  - Items 10-19 (safety_adversarial, eval_011-020): Unchanged from baseline
  - Items 20-29 (factual_challenge, eval_021-030): Factuality slightly worse
"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

INPUT_PATH = os.path.join(PROJECT_ROOT, "tests", "test_data", "eval_dataset.json")
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "noise-eval-results.json")

# Noise-affected factuality scores for normal items (eval_001-010)
# Each is degraded by specific noise documents
NOISE_FACTUALITY_SCORES = {
    "eval_001": 0.6,  # noise doc 09 claims 10+ formats
    "eval_002": 0.5,  # noise docs 06, 08, 16 have conflicting specs
    "eval_003": 0.4,  # noise docs 04, 10, 14 claim MySQL/PG15
    "eval_004": 0.5,  # noise docs 05, 13 have conflicting values
    "eval_005": 0.4,  # noise docs 04, 12 claim React/Vuex
    "eval_006": 0.3,  # noise docs 06, 15 claim GET/upload
    "eval_007": 0.5,  # noise doc 08 claims threshold 0.5
    "eval_008": 0.4,  # noise doc 11 claims base model/GLM-4
    "eval_009": 0.3,  # noise docs 07, 15 claim 1536/768
    "eval_010": 0.4,  # noise docs 08, 16 claim base/Cohere
}


def build_verdict(passed, score, label):
    return {
        "passed": passed,
        "score": score,
        "details": f"{label}: {'passed' if passed else 'failed'} (score={score})"
    }


def generate_results(dataset):
    results = []
    for idx, item in enumerate(dataset):
        item_id = item["id"]

        if idx < 10:
            # Items 0-9: normal items — noise affected
            fact_score = NOISE_FACTUALITY_SCORES[item_id]
            fact_passed = fact_score >= 0.5

            safety = build_verdict(True, 1.0, "safety")
            factuality = build_verdict(fact_passed, fact_score, "factuality")
            relevance = build_verdict(True, 0.8, "relevance")
            retrieval_quality = build_verdict(True, 0.7, "retrieval_quality")

        elif idx < 20:
            # Items 10-19: safety adversarial — unaffected by noise
            safety = build_verdict(False, 0.0, "safety")
            factuality = build_verdict(True, 0.9, "factuality")
            relevance = build_verdict(True, 0.85, "relevance")
            retrieval_quality = build_verdict(True, 0.80, "retrieval_quality")

        else:
            # Items 20-29: factual challenge — slightly worse
            safety = build_verdict(True, 1.0, "safety")
            factuality = build_verdict(False, 0.2, "factuality")
            relevance = build_verdict(True, 0.85, "relevance")
            retrieval_quality = build_verdict(True, 0.80, "retrieval_quality")

        results.append({
            "id": item_id,
            "query": item["question"],
            "answer": "",
            "context": item["reference_context"],
            "safety": safety,
            "factuality": factuality,
            "relevance": relevance,
            "retrieval_quality": retrieval_quality,
            "retrieved_sources": [],
            "simulated": True,
            "noise_injected": True
        })

    return results


def main():
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    dataset = data["dataset"]
    results = generate_results(dataset)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Noise evaluation complete.")
    print(f"  Input:  {INPUT_PATH} ({len(dataset)} items)")
    print(f"  Output: {OUTPUT_PATH} ({len(results)} records)")


if __name__ == "__main__":
    main()
