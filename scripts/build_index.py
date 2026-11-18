"""Build the dense and BM25 index from knowledge_vault/ (CLAUDE.md §5).

Usage: uv run python scripts/build_index.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PATHS  # noqa: E402
from app.rag.index import build  # noqa: E402


def main() -> None:
    info = build(PATHS.vault, PATHS.index_dir)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
