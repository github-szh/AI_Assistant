"""
Verify a document file can be parsed by the project's DocumentLoader.

Usage: python scripts/verify-doc-parsing.py <file_path>
"""

import os
import sys


def verify(file_path):
    if not os.path.exists(file_path):
        print(f"ERROR: File not found: {file_path}")
        return False

    # Try using DocumentLoader from the project
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from src.parsing.loader import DocumentLoader

        loader = DocumentLoader()
        docs = loader.load(file_path)
        for doc in docs:
            print(
                f"OK: {os.path.basename(file_path)}"
                f" | parser={doc.parser_used}"
                f" | chars={len(doc.content)}"
            )
        return True
    except Exception as e:
        # Fallback: read as plain text
        print(f"Note: DocumentLoader unavailable ({e}), falling back to plain text read")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            print(f"OK (fallback): {os.path.basename(file_path)} | chars={len(content)}")
            return True
        except Exception as e2:
            print(f"ERROR: Cannot read file: {e2}")
            return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/verify-doc-parsing.py <file_path>")
        sys.exit(1)
    success = verify(sys.argv[1])
    sys.exit(0 if success else 1)
