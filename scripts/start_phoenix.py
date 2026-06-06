"""Start Arize Phoenix for local LLM observability.

Usage:
    python scripts/start_phoenix.py

Opens the Phoenix UI at http://localhost:6006.
Your AI Assistant app auto-sends traces to this address via OTLP.
"""

import subprocess
import sys


def main():
    try:
        import phoenix  # noqa: F401
    except ImportError:
        print(
            "Phoenix is not installed.\n"
            "Install it with:\n"
            "  pip install arize-phoenix openinference-instrumentation-openai"
        )
        sys.exit(1)

    print("Starting Arize Phoenix at http://localhost:6006 ...")
    subprocess.run(
        [sys.executable, "-m", "phoenix.server.main", "serve"],
        check=True,
    )


if __name__ == "__main__":
    main()
