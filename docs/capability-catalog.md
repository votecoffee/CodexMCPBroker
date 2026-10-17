# Inner capability catalog, version 1

Broker 0.8 can index inner app operations that sit behind stable outer MCP tools. This is an opt-in discovery contract, separate from MCP's standard tool list. It adds search metadata and references to read-only app discovery tools. It does not create executable routes or translate native write arguments.

## Register the provider

An administrator can add this optional field to a version-1 service registration:

```json
{
  "capability_catalog": {
    "schema_version": 1,
    "tool": "list_broker_capabilities",
    "arguments": {}
  }
}
```

See the complete [registration example](../examples/app-capabilities.service.json). The configured provider must exist in the live MCP tool list and explicitly declare `readOnlyHint: true`. Its configured arguments must pass the native input schema and the service's argument budget. Legacy read overrides are insufficient for automated catalog probes. Broker refresh never guesses a provider name or reloads app extensions.

Provider registration authorizes a metadata-only tool invocation during unowned discovery and explicit capability refresh. It must be safe without selecting or operating a native project/device. Initialization and shutdown still follow the service's transport/session contract. Existing apps need an app-owned adapter or tool that supplies this format; an existing `list_capabilities` name alone does not establish compatibility. Preserve existing native interfaces when adding that adapter.

## Provider result

Return the normalized object as MCP `structuredContent`, or as one JSON text content block when structured content is absent. If both exist, structured content is authoritative. App errors and malformed/unknown fields are rejected. See the [example](../examples/capability-catalog.json) and [JSON schema](../examples/capability-catalog.schema.json).

- `schema_version` is `1`.
- `catalog_revision` is an app-owned opaque string, preserved without interpretation.
- `capabilities` contains unique IDs, descriptions, optional search tags and a required `discovery` reference. IDs follow the broker's 128-character tool-name syntax, but are not native tool IDs.
- Each discovery reference names an existing outer MCP tool and complete schema-valid arguments for describing that capability. That tool must explicitly declare `readOnlyHint: true`. The broker validates the reference but does not invoke it during indexing. Do not embed executable commands, authorization or write instructions as routing fields.

The normalized payload is limited to 512 KiB of compact UTF-8 JSON and 1,000 entries. Each ID is at most 128 characters, description 4,096 characters, app revision 256 characters, and tags 32 strings of at most 64 characters each. Provider and individual discovery argument objects are limited to 16 KiB and must also fit the service budget. Unknown fields, external schema references, duplicate IDs and invalid discovery references reject the whole snapshot without partial indexing. These are retained-metadata limits after receipt, not a transport response-size guarantee.

## Search and use

`search_tools` accepts `kind: "all"` (default), `"tool"` or `"capability"`. Results carry a `kind` discriminator. Capability matches include `service_id`, `capability_id`, `capability_revision`, `app_catalog_revision` and `metadata_source`. They have `discovery_only: true` and `callable: false`; they cannot be passed to `call_read_tool` or `call_write_tool` as virtual native tools.

Call `describe_capability(service_id, capability_id, capability_revision)` with the searched broker snapshot revision. It returns attributed, untrusted metadata and a `discovery` object containing a namespaced native `tool_id`, its broker `catalog_revision` and native arguments. Inspect that native tool with `describe_tool`, then call it through the read channel in the appropriate context. Use its current inner schema, revision and app workflow before any native operation. Search, capability description and service description do not connect to an app or renew a lease.

Keep these scopes distinct:

| Revision | Meaning |
| --- | --- |
| Outer tool `catalog_revision` | Broker fingerprint of native tool definition and registration |
| `capability_revision` | Broker fingerprint of the indexed catalog, discovery bindings and registration |
| `app_catalog_revision` | Opaque revision supplied by the app for its inner catalog |
| Native target/review revision | The app's document, project, receipt or preview/apply guard |

An inner catalog update need not change the outer tool definition. A stale `capability_revision` fails description. Changed outer discovery definitions hide an old index until refreshed. App revisions remain app-enforced: the broker never substitutes its own revision for a native review token or builds a write operation from a search entry.

## Refresh and ownership

`refresh_services` queries configured providers only during unowned discovery. Public snapshots must contain installation-level metadata suitable for all authenticated frontends, without secrets or private project selections. While an exclusive context exists, global refresh preserves its earlier public snapshot and does not invoke its provider. Owner-specific catalog results never replace that shared snapshot.

After acquiring an exclusive context, call `refresh_capabilities(service_id, context_id)` to index its actual session. Only that frontend sees the resulting snapshot, even if another frontend knows its IDs or revision. Until this explicit probe, the owner has no owned capability index. Other frontends retain the earlier public snapshot. Closing, losing or expiring the context removes visibility of its private snapshot; a new owner must refresh its own context.

For transient services, `refresh_capabilities(service_id)` opens a temporary session and replaces the public snapshot. For exclusive services, the exact active context and frontend identity are required. A valid owned probe renews its lease. App extension installation/reload is still an app-owned action; refresh indexes metadata after that action and does not load code itself.

Invalid or app-error results evict the affected snapshot while preserving healthy native sessions and outer routes. Provider transport failure or deadline expiry follows the normal context-loss and durable recovery rules. Removed, changed or recovery-blocked registrations cannot expose capability snapshots. Automated metadata probes have no confirmation route; required elicitation is rejected, never answered on the user's behalf. Native calls remain subject to normal schema, target, write policy, approval, request-ID and uncertain-outcome rules.

This contract does not distribute app skills, prove native compatibility or authorize migration. Validate an adapter's real catalog and workflow through the staged [assessment guide](../MIGRATION.md) before relying on it.
