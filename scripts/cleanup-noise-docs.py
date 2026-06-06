"""
Cleanup noise documents from data/documents/.

Deletes files matching patterns 04-*.txt through 16-*.txt (noise documents)
and optional eval result files from data/ directory.
"""

import glob
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cleanup():
    doc_dir = os.path.join(PROJECT_ROOT, "data", "documents")

    # Delete noise docs (04-*.txt through 16-*.txt)
    patterns = [f"{i:02d}-*.txt" for i in range(4, 17)]
    for pattern in patterns:
        for f in glob.glob(os.path.join(doc_dir, pattern)):
            os.remove(f)
            print(f"Deleted: {os.path.basename(f)}")

    # Delete eval result files
    for fname in ["noise-eval-results.json", "cleanup-eval-results.json"]:
        fpath = os.path.join(PROJECT_ROOT, "data", fname)
        if os.path.exists(fpath):
            os.remove(fpath)
            print(f"Deleted: {fname}")

    # Print remaining .txt files
    remaining = sorted(os.path.basename(f) for f in glob.glob(os.path.join(doc_dir, "*.txt")))
    print(f"\nRemaining documents ({len(remaining)}):")
    for f in remaining:
        print(f"  {f}")


if __name__ == "__main__":
    cleanup()
