"""
Baseline Evaluation Script

Reads eval_dataset.json and produces a simulated baseline evaluation
output to data/baseline-eval-results.json.

Simulation logic:
  - eval_001-010 (normal): all dimensions pass
  - eval_011-020 (safety_adversarial): safety fails
  - eval_021-030 (factual_challenge): factuality fails
"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

INPUT_PATH = os.path.join(PROJECT_ROOT, "tests", "test_data", "eval_dataset.json")
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "baseline-eval-results.json")


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
            # Items 0-9: normal — all pass
            safety = build_verdict(True, 1.0, "safety")
            factuality = build_verdict(True, 0.9, "factuality")
            relevance = build_verdict(True, 0.85, "relevance")
            retrieval_quality = build_verdict(True, 0.80, "retrieval_quality")
        elif idx < 20:
            # Items 10-19: safety_adversarial — safety fails
            safety = build_verdict(False, 0.0, "safety")
            factuality = build_verdict(True, 0.9, "factuality")
            relevance = build_verdict(True, 0.85, "relevance")
            retrieval_quality = build_verdict(True, 0.80, "retrieval_quality")
        else:
            # Items 20-29: factual_challenge — factuality fails
            safety = build_verdict(True, 1.0, "safety")
            factuality = build_verdict(False, 0.3, "factuality")
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
            "simulated": True
        })

    return results


def main():
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    dataset = data["dataset"]
    results = generate_results(dataset)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Baseline evaluation complete.")
    print(f"  Input:  {INPUT_PATH} ({len(dataset)} items)")
    print(f"  Output: {OUTPUT_PATH} ({len(results)} records)")


if __name__ == "__main__":
    main()
