"""Inspect at most 4096 UTF-8 bytes from stdin; no file or native app access."""

import hashlib
import json
import sys

raw = sys.stdin.buffer.read(4097)
if len(raw) > 4096:
    raise SystemExit("Text exceeds 4096 UTF-8 bytes")
raw.decode("utf-8")
print(json.dumps({"utf8_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}))
