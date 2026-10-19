"""Harmless versioned backend for the shared app framework example."""

import argparse
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--generation", type=int, choices=[1, 2], required=True)
options = parser.parse_args()
server = MCPServer("Example text backend", log_level="WARNING")
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@server.tool(annotations=READ)
def inspect_text(text: str) -> dict:
    """Inspect supplied text and identify this disposable backend process."""
    if len(text.encode("utf-8")) > 4096:
        raise ValueError("Text exceeds 4096 UTF-8 bytes")
    return {"text": text, "generation": options.generation, "worker_pid": os.getpid()}


if options.generation == 2:

    @server.tool(annotations=READ)
    def count_words(text: str) -> dict:
        """Count words in user-supplied text using the newly published backend."""
        if len(text.encode("utf-8")) > 4096:
            raise ValueError("Text exceeds 4096 UTF-8 bytes")
        return {"words": len(text.split()), "generation": 2}


server.run(transport="stdio")
