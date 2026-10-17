"""Bounded local schema validation shared by tool and capability discovery."""

import json

from jsonschema.validators import validator_for

from .models import BrokerError


def check_schema(schema: dict) -> None:
    if len(json.dumps(schema).encode("utf-8")) > 128 * 1024:
        raise BrokerError("tool schema exceeds 128 KiB")

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise BrokerError("tool schemas cannot fetch external references")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    validator = validator_for(schema, default=None)
    if validator is None:
        if "$schema" in schema:
            raise BrokerError("unsupported JSON Schema dialect")
        validator = validator_for(schema)
    validator.check_schema(schema)
