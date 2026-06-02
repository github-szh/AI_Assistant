"""Compare retrieval with/without Sentence Window expansion."""
import sys
sys.path.insert(0, ".")

from src.knowledge.retrieval import HybridRetriever, _expand_to_parents, _get_original_text

r = HybridRetriever()

queries = [
    "劳动合同的解除条件有哪些",
    "经济补偿金怎么计算",
    "什么情况下用人单位不能解除劳动合同",
]

print("=" * 70)
print("  Sentence Window 对比测试 — 劳动法.pdf")
print("=" * 70)

for q in queries:
    print(f"\n查询: {q}")
    print("-" * 50)

    # Without expansion (child chunks only — simulating old behavior)
    children = r._coarse_retrieve(q)
    if not children:
        print("  (无结果)")
        continue

    # Check if any have parent_id
    has_parent = any(n.metadata.get("parent_id") for n in children)

    if has_parent:
        # With expansion
        expanded = _expand_to_parents(children)

        print(f"  未展开: {len(children)} 个子 chunk ({sum(len(_get_original_text(n)) for n in children[:5])} 字/前5个)")
        for i, n in enumerate(children[:3]):
            ot = _get_original_text(n)
            print(f"    [{i+1}] {len(ot)}字: {ot[:80]}...")

        print(f"  展开后: {len(expanded)} 个父 chunk ({sum(len(_get_original_text(n)) for n in expanded[:5])} 字/前5个)")
        for i, n in enumerate(expanded[:3]):
            ot = _get_original_text(n)
            print(f"    [{i+1}] {len(ot)}字: {ot[:80]}...")
    else:
        # No sentence window — normal chunks
        print(f"  普通 chunk ({len(children)} 个):")
        for i, n in enumerate(children[:3]):
            ot = _get_original_text(n)
            print(f"    [{i+1}] {len(ot)}字: {ot[:80]}...")

print("\n" + "=" * 70)
print("  测试完成")
print("=" * 70)
