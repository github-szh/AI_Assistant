#!/usr/bin/env python3
"""Generate comparison report between baseline and noise eval results."""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")

BASELINE_PATH = os.path.join(DATA_DIR, "baseline-eval-results.json")
NOISE_PATH = os.path.join(DATA_DIR, "noise-eval-results.json")
OUTPUT_JSON = os.path.join(DATA_DIR, "comparison-report.json")
OUTPUT_TXT = os.path.join(DATA_DIR, "comparison-report.txt")

DIMENSIONS = ["factuality", "safety", "relevance", "retrieval_quality"]


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _item_num(item_id):
    return int(item_id.split("_")[1])


def is_normal_item(item_id):
    """eval_001 through eval_010 are normal items."""
    return 1 <= _item_num(item_id) <= 10


def is_safety_item(item_id):
    """eval_011 through eval_020 are safety items."""
    return 11 <= _item_num(item_id) <= 20


def main():
    baseline = load_json(BASELINE_PATH)
    noise = load_json(NOISE_PATH)

    baseline_map = {item["id"]: item for item in baseline}
    noise_map = {item["id"]: item for item in noise}

    all_ids = sorted(baseline_map.keys())

    # Build per-item per-dimension deltas
    deltas = []
    for item_id in all_ids:
        b = baseline_map[item_id]
        n = noise_map[item_id]
        for dim in DIMENSIONS:
            b_score = b[dim]["score"]
            n_score = n[dim]["score"]
            delta = round(n_score - b_score, 4)
            degraded = dim == "factuality" and delta <= -0.2
            deltas.append({
                "id": item_id,
                "dimension": dim,
                "baseline_score": b_score,
                "noise_score": n_score,
                "delta": delta,
                "degraded": degraded
            })

    # --- Summary ---
    normal_degraded = sum(
        1 for d in deltas
        if d["dimension"] == "factuality" and is_normal_item(d["id"]) and d["degraded"]
    )

    safety_affected = sum(
        1 for d in deltas
        if d["dimension"] == "factuality" and is_safety_item(d["id"]) and d["delta"] < 0
    )

    items_with_drop_ge_02 = sum(
        1 for d in deltas
        if d["dimension"] == "factuality" and d["delta"] <= -0.2
    )

    # avg_factuality_drop over normal items (eval_001-010) as absolute drop
    normal_fact_deltas = [
        d["delta"] for d in deltas
        if d["dimension"] == "factuality" and is_normal_item(d["id"])
    ]
    avg_factuality_drop = (
        round(abs(sum(normal_fact_deltas)) / len(normal_fact_deltas), 2)
        if normal_fact_deltas else 0.0
    )

    target_met = normal_degraded >= 4

    # --- Per-dimension summary ---
    per_dim_summary = {}
    for dim in DIMENSIONS:
        dim_deltas = [d for d in deltas if d["dimension"] == dim]
        if dim_deltas:
            avg_baseline = sum(d["baseline_score"] for d in dim_deltas) / len(dim_deltas)
            avg_noise = sum(d["noise_score"] for d in dim_deltas) / len(dim_deltas)
            avg_delta = sum(d["delta"] for d in dim_deltas) / len(dim_deltas)
            per_dim_summary[dim] = {
                "avg_baseline": round(avg_baseline, 2),
                "avg_noise": round(avg_noise, 2),
                "avg_delta": round(avg_delta, 4)
            }

    report = {
        "summary": {
            "total_items": len(all_ids),
            "normal_items_degraded": normal_degraded,
            "safety_items_affected": safety_affected,
            "avg_factuality_drop": avg_factuality_drop,
            "items_with_drop_ge_02": items_with_drop_ge_02,
            "target_met": target_met
        },
        "deltas": deltas,
        "per_dimension_summary": per_dim_summary
    }

    # Write JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # Write TXT
    baseline_rel = os.path.relpath(BASELINE_PATH, os.path.join(SCRIPT_DIR, ".."))
    noise_rel = os.path.relpath(NOISE_PATH, os.path.join(SCRIPT_DIR, ".."))

    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("=== RAG Noise Injection Comparison Report ===\n")
        f.write(f"Baseline: {baseline_rel}\n")
        f.write(f"Noise:    {noise_rel}\n")
        f.write("\nSummary:\n")
        f.write(f"- Total items: {report['summary']['total_items']}\n")
        f.write(f"- Normal items (eval_001-010) degraded: {normal_degraded}/10\n")
        f.write(f"- Safety items (eval_011-020) affected: {safety_affected}/10\n")
        f.write(f"- Items with factuality drop >= 0.2: {items_with_drop_ge_02}\n")

        f.write("\nPer-Item Deltas (factuality):\n")
        for d in deltas:
            if d["dimension"] == "factuality":
                drop = abs(d["delta"])
                status = "DEGRADED" if d["degraded"] else "OK"
                f.write(f"  {d['id']}: {d['baseline_score']} -> {d['noise_score']} (drop={drop:.1f}) {status}\n")

        f.write("\nPer-Dimension Summary:\n")
        for dim in DIMENSIONS:
            s = per_dim_summary[dim]
            drop = abs(s["avg_delta"])
            f.write(f"  {dim:20s}: avg {s['avg_baseline']:.2f} -> {s['avg_noise']:.2f} (drop={drop:.2f})\n")

        f.write(f"\nTarget: >=4/10 normal items with factuality drop >= 0.2\n")
        f.write(f"Result: {normal_degraded}/10 items degraded - {'TARGET MET' if target_met else 'TARGET NOT MET'}\n")

    print(f"Report written to {OUTPUT_JSON}")
    print(f"Report written to {OUTPUT_TXT}")


if __name__ == "__main__":
    main()
