"""MCP server for the deforestation detection project.

Exposes the results in outputs/ as tools a language model can call.
Run by Claude Desktop over stdio: nothing may be printed to stdout.
"""

import json
from pathlib import Path

from fastmcp import FastMCP

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "outputs"

mcp = FastMCP(name="deforestation")


@mcp.tool
def get_run_info() -> dict:
    """Describe how the detection was run: study area, dates, scenes and thresholds.

    Use this when the user asks where or when the analysis was done, which
    satellite images were used, or which settings produced the alerts.
    """
    with open(OUTPUT_DIR / "run_info.json") as f:
        return json.load(f)


if __name__ == "__main__":
    mcp.run()