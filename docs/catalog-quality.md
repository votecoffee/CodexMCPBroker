# Catalog quality development tools

The framework provides **offline** catalog linting, optional evaluator reviews, before/after definition reports and selection-only request fixtures. These tools do not connect to a server, start services, dispatch domain tools, change settings or upgrade an application. Existing MCP Servers keep their reviewed framework pins. This feature has not been rolled out to them.

The review rubric is inspired by [Glama's Tool Definition Quality Score](https://github.com/glama-ai/tool-definition-quality-score). This implementation is an adapted engineering development profile, not an official Glama score or certification. Review the actual emitted `tools/list` definitions for each selected deployment profile, including full input/output schemas and annotations. Do not substitute source decorators for captured wire definitions.

## Deterministic checks

Use the framework development environment or a separately verified framework wheel:

```powershell
python -m universal_tools.quality_cli lint examples/quality-catalog.json --profile "Synthetic supplied-content text analysis; no native or file operations"
```

The installed entry point is `universal-tools-quality`. The CLI reads UTF-8 JSON and prints JSON to stdout. Exit 0 means the requested deterministic checks passed, 1 means catalog errors or incorrect selection cases, and 2 means invalid input or review binding. Output files and any external evaluator submission are caller controlled. Keep real app captures and results outside public Git.

Supply an array of tool definitions or a complete `{"tools": [...]}` result. Explicitly collect all pagination pages through an independently authorized client first; a nonempty `nextCursor` is refused. The checker never starts a server to collect metadata. Provide the selected profile and its limits in `--profile`; offline and native catalogs are separate review inputs.

Checks include unique stable tool names, nonempty descriptions, valid object-root JSON Schema 2020-12, locally resolvable references and boolean effect hints. Missing optional hints and undocumented nested input/output fields produce warnings. A description that exactly repeats the name/title is flagged. These are development gates: a missing description is a quality error here even though MCP makes that metadata optional. Hints do not establish actual effects or authorize a call. For example, MCP ignores `destructiveHint` when `readOnlyHint` is true; the checker does not invent a contradiction from that combination alone.

Only existing local JSON Pointer schema references are supported. External references and dynamic anchors are outside this profile; no remote schema resolution occurs. Recursive local references terminate because checks walk the bounded schema document rather than expanding references. Examples and defaults are instance data, not executable schemas. Each input is limited to 8 MiB, 100,000 JSON nodes, depth 64 and 256 tools. Reports include definition bytes and top-level schema documentation counts, not a fabricated static quality grade.

## Optional review

```powershell
python -m universal_tools.quality_cli review-request examples/quality-catalog.json --profile "Synthetic supplied-content text analysis; no native or file operations" --evaluator "reviewer-model/version; decoding-and-prompt-settings/1" > .local/quality-request.json
# Submit that request to a reviewer/model chosen by the developer; no automatic submission occurs.
python -m universal_tools.quality_cli review-result examples/quality-catalog.json --profile "Synthetic supplied-content text analysis; no native or file operations" --evaluator "reviewer-model/version; decoding-and-prompt-settings/1" --response .local/quality-response.json
```

The exported request contains an original instruction, the complete catalog, deterministic findings/signals, rubric weights, response JSON Schema and a `request_hash`. The response must echo that hash, provide exactly one review for every tool, and score each dimension from 1 to 5 with evidence. It also supplies four catalog coherence ratings. Unknown, duplicate, missing or stale entries, noninteger scores and extra response fields are refused. A human reviewer can use the same contract. `evaluate_catalog(catalog, callback, profile=..., evaluator=...)` supports a developer-supplied provider adapter without adding a credential/network dependency or automatic retries.

The six tool dimensions retain the TDQS weights: purpose clarity 25%, usage guidance 20%, behavioral transparency 20%, parameter semantics 15%, conciseness/structure 10%, and contextual completeness 10%. Deterministic post-processing caps a tautological purpose score at 2 and sets behavioral transparency to 1 for an evaluator-reported annotation contradiction. Original evaluator evidence remains in the imported response; the report records its digest and the adjusted dimensions.

Integer half-up arithmetic produces one-decimal scores. Description quality combines 60% mean and 40% minimum tool score. Coherence averages disambiguation, naming consistency, scope fit and workflow completeness. Overall uses 70% description quality and 30% coherence; A starts at 3.5, B at 3, C at 2 and D at 1. This profile requires **100% review coverage** before publishing a composite. Coherence is assessed against the selected workflow, without requiring CRUD/native operations or an arbitrary preferred tool count. Structural invocation-cost/shadowing heuristics are not implemented or presented as measured selection failures.

Every request binds the **whole catalog**, selected profile, rubric/instructions, evaluator identity and settings revision. Sibling changes therefore invalidate an older review, even when an individual tool did not change. No cache or live catalog is rewritten. Record the evaluator's actual version/settings accurately; a caller-supplied label is provenance, not proof of what ran. Scores are advisory, can vary by evaluator and are independent of deterministic CI gates. Definition quality does not prove authorization, native correctness, lifecycle cleanup or desktop-chat availability.

## Before/after and tool selection

```powershell
python -m universal_tools.quality_cli compare .local/new-catalog.json --before .local/old-catalog.json --profile "Same selected profile"
python -m universal_tools.quality_cli choices-request examples/quality-catalog.json --cases examples/quality-cases.json --profile "Synthetic supplied-content text analysis; no native or file operations" --evaluator "selector-model/version; settings/1" > .local/selection-request.json
python -m universal_tools.quality_cli choices-result examples/quality-catalog.json --cases examples/quality-cases.json --profile "Synthetic supplied-content text analysis; no native or file operations" --evaluator "selector-model/version; settings/1" --response .local/selection-response.json
```

Definition comparisons report added, removed and changed tools plus deterministic findings. Run separate optional reviews for each version with the same profile/rubric/evaluator if comparing advisory grades; do not compare stale or differently configured model scores as a regression measure.

Each selection fixture specifies a unique `id`, representative user `request` and `acceptable_tools`, including `null` for requests outside the profile. Expected choices are withheld from the exported selector request. The response is `{"request_hash": "...", "choices": [{"id": "...", "tool": "tool_name_or_null"}]}` with JSON `null` for no tool, not the text `"null"`. The checker requires exact case coverage and current catalog binding. Wrong choices fail; invented tools and stale responses are refused. The result separately hashes expectations so an expectation change cannot masquerade as the original test suite.

`evaluate_choices(catalog, cases, callback, profile=..., evaluator=...)` invokes only the caller's selection adapter, then validates and grades its response. It never dispatches the chosen tool. Synthetic callback tests prove the harness contract, not real model accuracy. Retain actual model responses to claim observed selection accuracy, and run separately authorized invocation/host/native tests for downstream correctness. Treat model-facing catalog/request text as untrusted and never give this evaluator an engineering tool executor.

## Verification and adoption

See [verification](../VERIFICATION.md#catalog-quality-profile) and [tool design](server-development/tools-and-workflows.md). App adoption is optional and separately reviewed: capture a complete selected-profile catalog, fix deterministic errors, review warning applicability, retain bound evaluator/choice evidence if used, and keep runtime/host/native acceptance separate. Updating this framework does not alter installed app packages, plugins, skills, settings or tools.
