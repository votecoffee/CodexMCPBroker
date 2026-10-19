---
name: text-tools-inspect-text
description: Inspect user-supplied text and its UTF-8 digest using the synthetic text workflow service through Universal Tools.
---

Use the registered `workflow-example` service to inspect text supplied by the user. This example does not open drawings, files or native applications.

Search Universal Tools for `summarize_text` in that service. Describe its service and tool, acquire a context if required, and call it through the read channel with the exact catalog revision. Preserve the supplied text and return its `utf8_bytes` and `sha256`. Read [the encoding limit](references/limits.md) when checking the size boundary. Release an acquired context when finished.

If an independent local digest is useful, `scripts/text_digest.py` accepts UTF-8 bytes on stdin and returns JSON; it makes no broker or app calls. `assets/sample.txt` is an optional Unicode sample. The synchronizer copies these resources without executing them.

This skill requires the example service registration. Missing registration, denied ownership or an unsupported request is a reported gap; do not switch to a direct app plugin. Follow the user's explicit instructions and the app's current tool policy.
