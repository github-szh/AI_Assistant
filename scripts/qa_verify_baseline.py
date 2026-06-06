"""QA Verification: Baseline evaluation JSON integrity."""
import json
import sys

d = json.load(open("data/baseline-eval-results.json", encoding="utf-8"))
errors = []

# 1. Record count
if len(d) != 30:
    errors.append(f"Expected 30 records, got {len(d)}")
else:
    print(f"[PASS] Record count: 30/30")

# 2. Required fields
required = ["id","query","answer","context","safety","factuality","relevance","retrieval_quality","retrieved_sources","simulated"]
for i, r in enumerate(d):
    for k in required:
        if k not in r:
            errors.append(f"Item {i} ({r.get('id','?')}) missing field: {k}")
if not any("missing field" in e for e in errors):
    print(f"[PASS] All required fields present in all 30 items")

# 3. Normal items (0-9): all pass
for i in range(10):
    item_id = d[i]["id"]
    if d[i]["safety"]["passed"] != True:
        errors.append(f"{item_id}: safety should pass")
    if d[i]["factuality"]["score"] != 0.9:
        errors.append(f"{item_id}: factuality score should be 0.9")
    if d[i]["factuality"]["passed"] != True:
        errors.append(f"{item_id}: factuality should pass")
if not any("should" in e for e in errors if "index 0-9" not in e):
    print(f"[PASS] Normal items (eval_001-010): all dimensions pass")

# 4. Safety adversarial (10-19): safety=fail
for i in range(10, 20):
    item_id = d[i]["id"]
    if d[i]["safety"]["passed"] != False:
        errors.append(f"{item_id}: safety should fail")
    if d[i]["safety"]["score"] != 0.0:
        errors.append(f"{item_id}: safety score should be 0.0")
    if d[i]["factuality"]["passed"] != True:
        errors.append(f"{item_id}: factuality should pass")
if not any("should" in e for e in errors if "index 10-19" not in e):
    print(f"[PASS] Safety adversarial (eval_011-020): safety=fail")

# 5. Factual challenge (20-29): factuality=fail
for i in range(20, 30):
    item_id = d[i]["id"]
    if d[i]["factuality"]["passed"] != False:
        errors.append(f"{item_id}: factuality should fail")
    if d[i]["factuality"]["score"] != 0.3:
        errors.append(f"{item_id}: factuality score should be 0.3")
    if d[i]["safety"]["passed"] != True:
        errors.append(f"{item_id}: safety should pass")
if not any("should" in e for e in errors if "index 20-29" not in e):
    print(f"[PASS] Factual challenge (eval_021-030): factuality=fail")

# 6. All simulated
if all(r.get("simulated") == True for r in d):
    print(f"[PASS] All 30 items marked simulated=true")
else:
    errors.append("Not all items marked simulated=true")

# 7. All retrieved_sources empty
if all(r.get("retrieved_sources") == [] for r in d):
    print(f"[PASS] All retrieved_sources=[]")
else:
    errors.append("Not all items have empty retrieved_sources")

if errors:
    print(f"\n[FAIL] {len(errors)} error(s):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print(f"\n=== ALL QA CHECKS PASSED ===")
