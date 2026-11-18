"""Sign the MCP server manifests. Run after any change to mcp_servers/ (design.md D32).

Usage: uv run python scripts/sign_manifests.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.manifests import sign_all  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(sign_all(), indent=2))
