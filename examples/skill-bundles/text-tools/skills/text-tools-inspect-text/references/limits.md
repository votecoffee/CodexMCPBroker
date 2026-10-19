# Encoding limit

The example tool accepts at most 4096 UTF-8 bytes. Preserve Unicode, whitespace and newlines exactly when comparing digests. A character count is not a UTF-8 byte count. The tool rejects oversize input without opening files or running a native engine.
