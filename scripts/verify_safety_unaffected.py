"""Verify safety adversarial items (eval_011-020) unchanged after noise injection."""
import json
import sys
from datetime import date

BASELINE = "data/baseline-eval-results.json"
NOISE    = "data/noise-eval-results.json"
OUTPUT   = "data/safety-unaffected.json"
REPORT   = "data/safety-contamination-report.json"

def load(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def safety_key(item: dict) -> dict:
    """Extract the safety sub-object for comparison."""
    return item.get("safety", {})

def main():
    baseline = load(BASELINE)
    noise    = load(NOISE)

    # Safety items are eval_011 through eval_020 → indices 10..19
    differences = []

    for i in range(10, 20):
        b_item = baseline[i]
        n_item = noise[i]
        b_id = b_item["id"]
        n_id = n_item["id"]

        if b_id != n_id:
            differences.append(f"ID mismatch at index {i}: baseline={b_id}, noise={n_id}")
            continue

        b_safety = safety_key(b_item)
        n_safety = safety_key(n_item)

        if b_safety != n_safety:
            differences.append({
                "id": b_id,
                "baseline": b_safety,
                "noise": n_safety
            })

    if not differences:
        result = {
            "verified": True,
            "total_safety_items": 10,
            "unaffected_count": 10,
            "affected_count": 0,
            "details": "All 10 safety adversarial items (eval_011-020) unchanged after noise injection",
            "timestamp": date.today().isoformat()
        }
        with open(OUTPUT, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("SAFETY VERIFIED: All 10 items unaffected")
    else:
        report = {
            "verified": False,
            "total_safety_items": 10,
            "unaffected_count": 10 - len([d for d in differences if isinstance(d, dict)]),
            "affected_count": len([d for d in differences if isinstance(d, dict)]),
            "differences": differences,
            "timestamp": date.today().isoformat()
        }
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"SAFETY CONTAMINATION DETECTED: {len(differences)} items changed")
        for d in differences:
            if isinstance(d, str):
                print(f"  - {d}")
            else:
                print(f"  - {d['id']}: baseline={d['baseline']}, noise={d['noise']}")
        sys.exit(1)

if __name__ == "__main__":
    main()
