"""Read-only workflow contribution example; no native engine or installed plugin required."""

import argparse
import hashlib
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
REFERENCE = "The text limit is 4096 UTF-8 bytes. Preserve Unicode and return the exact digest."


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_server(publication: Path) -> FastMCP:
    server = FastMCP(
        "Synthetic workflow contributions",
        instructions="Discover app workflow guides before using their referenced tools.",
        log_level="WARNING",
    )

    def generation() -> int:
        if publication.stat().st_size > 1024:
            raise ValueError("Publication metadata exceeds example budget")
        value = json.loads(publication.read_text(encoding="utf-8"))
        if set(value) != {"generation"} or type(value["generation"]) is not int:
            raise ValueError("Invalid publication metadata")
        if value["generation"] not in {1, 2}:
            raise ValueError("Unknown example generation")
        return value["generation"]

    def names(number):
        return ["text.summarize"] + (["text.audit"] if number == 2 else [])

    @server.tool(annotations=READ)
    def list_workflow_capabilities() -> dict:
        """Publish safe workflow hints using the normal broker capability contract."""
        number = generation()
        return {
            "schema_version": 1,
            "catalog_revision": f"publication-{number}",
            "capabilities": [
                {
                    "id": name,
                    "description": (
                        f"Workflow guide for {name}: inspect supplied text and its digest."
                    ),
                    "tags": ["workflow", "text"],
                    "discovery": {
                        "tool": "read_workflow",
                        "arguments": {"workflow_id": name, "expected_revision": f"guide-{number}"},
                    },
                }
                for name in names(number)
            ],
        }

    @server.tool(annotations=READ)
    def read_workflow(workflow_id: str, expected_revision: str) -> dict:
        """Read a current guide; reject stale guidance rather than replacing its revision."""
        number = generation()
        if workflow_id not in names(number) or expected_revision != f"guide-{number}":
            raise ValueError("Unknown workflow or stale workflow revision")
        instructions = (
            "Use summarize_text on the user-supplied text. Return its exact UTF-8 length and "
            "SHA-256. Consult text-limits when checking the size boundary. Do not open files."
        )
        return {
            "schema_version": 1,
            "id": workflow_id,
            "revision": expected_revision,
            "description": "Inspect supplied text without native or file access.",
            "instructions": {
                "format": "markdown",
                "text": instructions,
                "sha256": digest(instructions),
            },
            "tools": [{"tool": "summarize_text", "purpose": "Inspect supplied text"}],
            "references": [
                {
                    "id": "text-limits",
                    "description": "The example's encoding and size boundary.",
                    "sha256": digest(REFERENCE),
                    "read": {
                        "tool": "read_workflow_reference",
                        "arguments": {
                            "workflow_id": workflow_id,
                            "expected_revision": expected_revision,
                            "reference_id": "text-limits",
                            "expected_sha256": digest(REFERENCE),
                        },
                    },
                }
            ],
        }

    @server.tool(annotations=READ)
    def read_workflow_reference(
        workflow_id: str, expected_revision: str, reference_id: str, expected_sha256: str
    ) -> dict:
        """Read a named UTF-8 reference after checking its workflow revision and exact hash."""
        read_workflow(workflow_id, expected_revision)
        if reference_id != "text-limits" or expected_sha256 != digest(REFERENCE):
            raise ValueError("Unknown reference or changed reference digest")
        return {
            "id": reference_id,
            "revision": expected_revision,
            "sha256": digest(REFERENCE),
            "media_type": "text/plain",
            "content": REFERENCE,
        }

    @server.tool(annotations=READ)
    def summarize_text(text: str) -> dict:
        """Inspect at most 4096 UTF-8 bytes without touching files or native applications."""
        encoded = text.encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("Text exceeds the example budget")
        return {"text": text, "utf8_bytes": len(encoded), "sha256": digest(text)}

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
    options = parser.parse_args()
    create_server(options.publication).run(transport="stdio")
