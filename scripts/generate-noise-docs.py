"""
Generate noise documents in the style of existing test docs.

Provides helper functions for creating formatted noise documents
that follow the conventions of data/test_docs/ files.
"""

import os

CHINESE_NUMERALS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def generate_doc(filename: str, title: str, sections: list[dict]) -> str:
    """
    Generate a noise document in the style of existing test docs.

    Args:
        filename: output filename (e.g. "04-AI-Assistant-system-arch.txt")
        title: document title
        sections: list of {"heading": str, "content": str}

    Returns:
        The full document content as a string
    """
    lines = [f"\ufeff{title}", ""]

    for i, section in enumerate(sections):
        numeral = CHINESE_NUMERALS[i] if i < len(CHINESE_NUMERALS) else str(i + 1)
        lines.append(f"{numeral}、{section['heading']}")
        lines.append("")
        lines.append(section["content"])
        lines.append("")

    return "\n".join(lines)


def write_doc(filepath: str, content: str) -> bool:
    """
    Write document content to a file, creating parent directories if needed.

    Args:
        filepath: path to the output file
        content: document content to write

    Returns:
        True on success, False on failure
    """
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Written: {filepath}")
        return True
    except OSError as e:
        print(f"Error writing {filepath}: {e}")
        return False


if __name__ == "__main__":
    example_sections = [
        {
            "heading": "系统架构总览",
            "content": "示例架构说明内容。",
        },
        {
            "heading": "部署环境要求",
            "content": "示例部署要求内容。",
        },
    ]
    doc = generate_doc(
        filename="04-Example.txt",
        title="示例文档",
        sections=example_sections,
    )
    # Print to console safely (strip BOM for Windows GBK console)
    try:
        print(doc)
    except UnicodeEncodeError:
        print(doc.lstrip("\ufeff"), end="")
