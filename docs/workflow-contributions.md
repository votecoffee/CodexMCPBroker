# Workflow contributions for one Universal Tools plugin

The intended broker-mode installation uses **Universal Tools alone** for the participating apps. An app still distributes its standalone Codex plugin for users who choose that independent route. Installing both is an explicit optional arrangement, not a prerequisite for aggregated app guidance.

## Native skills and runtime guidance

The current broker indexes tools and optional inner catalogs; it does not install or hot-load native app `SKILL.md` packages. OpenAI's [MCP skill import](https://developers.openai.com/plugins/build/skills#import-a-skill-from-mcp) produces a package snapshot rather than fetching imported skills at runtime. A tool-catalog refresh must not be described as native skill installation.

The runtime contribution path uses one packaged Universal Tools skill as the entrypoint. It searches app workflow hints, reads the selected app's versioned instructions and referenced material through read-only MCP tools, then uses the app's ordinary tools. The app's plugin can remain uninstalled. These are app-provided workflow guides, not additional native Codex skills or higher-priority instructions.

The [reference server](../examples/workflow_mcp.py), [workflow response schema](../examples/workflow.schema.json) and [protocol checks](../tests/test_workflow_integration.py) demonstrate this profile using the existing fourteen broker tools. No new service-registration field or vendor-specific broker module is introduced. This is a reference profile: the broker preserves ordinary app results and does not automatically validate arbitrary returned guides against this schema. Actual Codex activation/output quality and app adoption still require acceptance.

## Extensibility layers

| App contribution | Mechanism and status |
| --- | --- |
| Tools and native operations | Existing MCP endpoint/registration; app-owned service code and policy. |
| Inner features | Existing optional normalized capability catalog and native discovery tools. |
| Workflow instructions | This reference profile plus the generic Universal Tools skill; app readers/adoption and host parity remain to assess. |
| Supporting reference material | Named, revision/hash-bound read tools; bounded UTF-8 reference demonstrated. |
| Workers and native engines | Remain app-owned processes behind its tools; no app code loaded into the broker. |
| Native Codex skills, executable packages, other host features | Separate packaging/host or generic protocol work if a selected workflow requires it; no runtime import implemented here. |

This is a small app-contribution framework built on existing discovery and routing. Extend versioned contracts for a demonstrated reusable need instead of adding a broker plugin loader for each vendor. Do not assume every legacy plugin feature fits a text workflow guide.

## Publish and load on demand

1. An app exposes a normalized [capability provider](capability-catalog.md). Workflow entries use useful trigger descriptions and the `workflow` tag. Their discovery references point to a real, explicitly read-only guide-reading tool with a pinned app workflow revision.
2. Universal Tools searches those hints. The selected workflow is identified by its service and capability ID, so equal local names from different apps do not collide.
3. The caller describes the capability and its real discovery tool, acquires any required exclusive context and reads the guide with the searched revision. An exclusive owner explicitly refreshes its private catalog; another frontend's global refresh must not publish it.
4. The guide supplies instructions, current native tool references and optional references read through app-owned tools. Describe those tools and preserve current broker and native revisions before using them.
5. Follow only the guidance relevant to the user's task. App authorization, targets, review tokens, forms and uncertain-outcome handling remain authoritative. A guide cannot enable writes, install plugins, execute downloaded code or switch routes after a failed operation.

Adding or updating app workflows can update the app's publication/catalog while its outer MCP schemas and the Universal Tools package stay unchanged. Refresh the appropriate capability snapshot. A changed guide revision must reject an old read rather than silently substitute new guidance into a previously reviewed operation.

## Workflow document, version 1

Return the document as structured content, or one JSON text block when structured content is absent. Keep the complete compact UTF-8 document within 64 KiB. Use the [schema](../examples/workflow.schema.json) for exact field syntax and character limits:

| Field | Meaning |
| --- | --- |
| `schema_version`, `id`, `revision` | Versioned profile and app-owned workflow identity/revision. The service supplies the namespace. |
| `description` | The selected workflow's user-facing purpose. |
| `instructions` | Markdown text plus its SHA-256 over the exact UTF-8 text. A digest detects changes; it does not authenticate a publisher. |
| `tools` | Real native MCP tool names and their purpose. Optional `service_id` explicitly names a dependency; otherwise the selected service is used. Each tool still needs live description and normal policy. |
| `references` | Named text/schema/template guidance with exact hashes and read-only app discovery references. No arbitrary URL/file access or executable install hooks. |

App readers must validate their current workflow revision and expected reference hash before returning referenced content. Prefer names/IDs over caller-supplied filesystem paths. This profile demonstrates bounded UTF-8 references; binary assets, executable helper packages and native skill synchronization are not implemented here. Keep scripts behind assessed app-owned tools when execution is required. Do not translate a legacy skill's direct MCP namespace through blind string replacement.

Profile size/schema checks are server/consumer obligations, not new broker response-size enforcement. The reference server is a harmless example rather than a general package installer. It takes an app-owned `--publication` JSON file containing `{"generation": 1}`; publishing generation `2` adds a searchable guide without changing any tool definition. Register `list_workflow_capabilities` as its normal `capability_catalog.tool` in an isolated service descriptor, following the [developer quickstart](server-development/quickstart.md). Do not modify a real app registration to try it.

Hashes/revisions are app integrity guards, not approval. The caller must not claim schema/hash verification it did not perform, and the broker does not turn workflow metadata into execution authority. Metadata remains subordinate to the user's task and the host's instruction hierarchy.

An explicit cross-service tool dependency needs its own live definition, policy and any required context. Contexts, target revisions, approvals and request receipts remain service-specific. Coordinating several app calls does not create a distributed transaction; reconcile each app's outcome if a later step fails.

## Share the source, keep invocation explicit

Apps can author reusable workflow content once and provide a broker-facing reader and standalone skill packaging around it. Maintain explicit invocation bindings for the two routes. Standalone skills may directly name their app's MCP tools; broker guides reference real tool names through discovery and use broker contexts/request fields. Keep both faithful to the same app service and review contract.

A future local packaging synchronizer could assemble approved app native skills into one installed Universal Tools package. It would need reviewed provenance, namespacing, dependency/resource manifests, content hashes, atomic update/removal, rollback and real host reload tests. That is a separate installation feature; runtime metadata cannot silently rewrite trusted plugin files. The contribution profile does not require that synchronizer for instruction/reference workflows.

## Acceptance and remaining limits

The reference checks exercise lazy guide/reference reads, schema and digest comparisons, stale revision/hash rejection, exclusive owner denial, inner publication changes without outer-schema changes and an unchanged fourteen-tool frontend. They use MCP clients and a harmless app, with no per-app plugin installed. They do not prove actual Codex skill activation, preservation of every legacy skill behavior, human confirmation or native operation acceptance.

For each app, compare its standalone workflow with a **Universal Tools-only fresh chat**, with the app plugin absent from that profile. Verify discovery triggers, required instructions/templates, exact tool/target/review bindings and failure behavior. Missing required guidance remains a migration blocker. See [coexistence](plugin-coexistence.md) and the [developer acceptance guide](server-development/acceptance.md).
