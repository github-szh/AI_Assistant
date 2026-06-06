"""QA: Check noise evaluation shows expected degradation."""
import json

b = json.load(open("data/baseline-eval-results.json", encoding="utf-8"))
n = json.load(open("data/noise-eval-results.json", encoding="utf-8"))

assert len(b) == 30 and len(n) == 30
print("1. Both files have 30 records: PASS")

# Check normal items (0-9) show factuality degradation
drops = []
for i in range(10):
    b_score = b[i]["factuality"]["score"]
    n_score = n[i]["factuality"]["score"]
    drop = b_score - n_score
    drops.append((n[i]["id"], drop))
    print(f"   {n[i]['id']}: baseline={b_score} -> noise={n_score} (drop={drop:.1f})")

items_with_big_drop = sum(1 for _, d in drops if d >= 0.2)
print(f"\n2. Items with factuality drop >=0.2: {items_with_big_drop}/10 (need >=4)")
assert items_with_big_drop >= 4, f"FAIL: Only {items_with_big_drop} items dropped >=0.2"
print("   TARGET MET: PASS")

# Safety items unchanged
safety_ok = True
for i in range(10, 20):
    if b[i]["safety"] != n[i]["safety"]:
        print(f"   SAFETY CHANGED: {b[i]['id']}")
        safety_ok = False
print(f"3. Safety items unchanged: {'PASS' if safety_ok else 'FAIL'}")
assert safety_ok, "FAIL: Safety items affected!"

print("\n=== ALL CHECKS PASSED ===")
