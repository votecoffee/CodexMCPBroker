# Developing optional MCP extensions

Framework 0.16 adds opt-in MCP Tasks, verified downstream Skills, live skill publication and extension lifecycle hooks. The runtime remains pinned to MCP Python SDK 2.3.0; an isolated profile exercises the adapters on SDK 2.2.0. These are framework components for app authors. They do not migrate installed apps, discover arbitrary plugins, grant permissions or establish native host acceptance.

## Choose a supported profile

| Component | Supported behavior | Deliberate boundary |
| --- | --- | --- |
| `TaskExtension` | Released `io.modelcontextprotocol/tasks`, durable handles, polling, form input, cooperative cancellation | Explicit app-owned adapters; no automatic conversion of broker calls or native execution recovery |
| `TaskClient` | Explicit create/get/update/cancel on an SDK session, including HTTP task routing headers | Caller persists handles, polls and presents its approval UI |
| `SkillRegistry` | Origin-bound discovery/get, verified lazy reads, content-bound approval and bounded memory cache | One trusted host principal per registry; no script execution or plugin installation |
| `SkillCatalog` | Local byte snapshots, explicit approved federation, pagination, optional directory reads, atomic publication | Published skill content remains untrusted; native host loading is independent |
| `ExtensionServer` | Explicit SDK extension composition, lifecycle cleanup, live resource namespaces | Extension identifiers and contribution kinds stay fixed until a server lifecycle change |

The ordinary framework/broker surface retains its fourteen baseline tools. Task routes must name app tools that the app explicitly registers and authorizes. Generic `call_read_tool`/`call_write_tool` routing does not automatically relay downstream task results, arbitrary resources, prompts, sampling or permission-bearing extensions. Do not opt a generic invocation tool into Tasks without implementing the full app policy, review, receipt and reconciliation contract for that route.

The [released Tasks specification](https://github.com/modelcontextprotocol/ext-tasks/blob/main/specification/2026-07-28/tasks.md) and [Skills specification](https://github.com/modelcontextprotocol/ext-skills/blob/main/specification/stable/skills.mdx) define the wire contracts. SDK package versions and negotiated wire revisions are separate choices.

## Compose extensions explicitly

`ApplicationHost.create_server(..., extensions=[extension])` and `create_surface(..., extensions=[extension])` accept selected SDK extensions. `skills=catalog` additionally wires the live resource provider. A standalone app can use `ExtensionServer` directly:

```python
from universal_tools.extensions import ExtensionServer
from universal_tools.skill_catalog import SkillCatalog

skills = SkillCatalog([reviewed_package], directory_read=True)
server = ExtensionServer(
    "Example app",
    extensions=[skills],
    resource_providers=[skills],
)
```

Duplicate extension identifiers or conflicting method registrations are rejected by the SDK. No remote declaration causes Python imports, extension installation or permission grants. An extension may expose an `@asynccontextmanager` method `lifespan(server)`; the framework enters selected extension lifespans in order, unwinds them in reverse order, and cleans up already-entered lifespans if a later one fails. These hooks belong to local trusted app code.

Live resource providers implement `claims_uri(uri)`, `resource_metadata()`, `read_bytes(uri)` and a `listeners` set of async change callbacks. Their claimed namespaces must be disjoint. `SkillCatalog` claims `skill://`; an app using it must not separately register competing resources in that namespace. A plain SDK `MCPServer(extensions=[skills])` serves only its initially registered local resources; use the framework server for live replacement and downstream federation.

Trusted deployment identity is bound around both extension methods and tool interception. For a single trusted frontend, supply an opaque deployment-selected `frontend_identity` to the framework surface. For multiple principals, use app-owned authenticated request identity; a shared fixed ID would merge ownership. The reference HTTP host provides authenticated local-user transport plus frontend affinity. Model arguments and `clientInfo` cannot establish task ownership.

## Adapt app-owned jobs to Tasks

The SDK does not implement this Tasks extension. The framework adapter uses its public extension hooks. The supported wire revision is `2026-07-28`, rather than the older experimental in-core task API.

```python
from universal_tools.tasks import TaskExtension, TaskRoute, TaskStore

tasks = TaskExtension(
    TaskStore(private_app_state / "tasks.sqlite3"),
    {"start_export": TaskRoute(export_jobs, "export-contract-v1", validate_export)},
)
server = host.create_server(extensions=[tasks])
# Register start_export on this server with its actual input schema.
# Its synchronous handler is not used when the nominated task route matches.
```

`export_jobs` implements `TaskAdapter`. `validate_export(arguments)` checks the complete request before a task is claimed. The route revision identifies the job adapter contract; old handles cannot silently bind to a different revision. Keep compatible adapters available while their tasks remain accessible.

| Adapter method | App responsibility |
| --- | --- |
| `authorize(context, handle)` | Recheck current app authorization, including reads of completed results. Called before creation with an empty task ID and no handle; called on every later task request with its actual identity. |
| `start(context, arguments)` | Durably associate `context.task_id` with one app job and return its opaque string handle. Enforce target selection, write policy, review revisions and app receipts. |
| `inspect(context, handle, pending)` | Read/reconcile the app's actual job, including lookup by task ID when `handle` is absent after a lost start response. Return `TaskObservation`; never start the job again. |
| `update(context, handle, responses)` | Validate and consume app review answers idempotently by task/input key. Recheck target and review revision before effects. |
| `cancel(context, handle)` | Record an idempotent cancellation request. Report actual eventual status through `inspect`; an acknowledgment does not prove stopping or rollback. |

`TaskContext.principal` comes from the trusted identity boundary. The store retains its SHA-256 rather than the raw principal. Each frontend request also checks that binding; unknown, expired and foreign task IDs share the same rejection. Principal hashes do not replace authenticated transport or app authorization.

An opaque `request_id` of 8–128 characters is required in tool arguments by default. A trusted embedding can supply `request_key(ctx, params)` instead, using an equivalent operation key. Never use the JSON-RPC request ID as a durable operation key. Reusing the same owner/key/route/revision/arguments returns the same task; changing its arguments is rejected. The initial intent is committed before `start`. A lost response, cancellation or restart never automatically redispatches start, input or cancellation.

`inspect` receives a persisted pending intent after an ambiguous operation. `TaskObservation(reconciled=True, ...)` explicitly confirms that the app reconciled it. Until then, further mutations are blocked. Return `handle=...` to bind a recovered app job. A database handle surviving restart does not prove that a native process, project or queued operation survived. Preserve the app's resource fencing and uncertain-write recovery barriers.

Return `TaskObservation("completed", result=call_tool_result_dict)` for successful **and app-error** results (`isError: true`). Use `failed` only with a JSON-RPC `error` object. Terminal states are immutable. `input_required` carries one to eight outstanding ordinary form requests; every key is unique for the lifetime of that task. Answered or superseded keys cannot be reused. Unknown/consumed response keys are ignored, partial answers are supported, and decline/cancel answers carry no approval content. URL elicitation, sampling and nested task inputs are rejected by this profile.

The store defaults to 10,000 records and 64 MiB of bounded record data including per-record overhead. Individual task data is limited to 4 MiB, and a task has at most 128 lifetime form keys. Default TTL is one day, configurable between one second and thirty days. Expiry denies further access and dispatch; it does not cancel a native job. Deduplication tombstones are retained, so archival is an explicit app-operator operation that must preserve its operation-key ledger. SQLite's page/free-list overhead is outside the logical data quota. Store form answers and results in private, access-controlled app state with appropriate retention.

App adapter methods have a default 30-second operation deadline, configurable up to 300 seconds. The durable job can run longer; polling reads its state. A timeout during mutation preserves the intent for reconciliation. The store uses transactional claims and optimistic updates to prevent concurrent claims from dispatching twice. This is a local SQLite deployment profile, not a replicated distributed queue.

Task routes require the client extension declaration on **every request**. Missing support is rejected before dispatch; there is no synchronous fallback. Legacy clients must use a separately selected app workflow. This profile uses `tasks/get` polling and does not publish optional `notifications/tasks` streams. It emits no task progress/log notifications.

```python
from mcp import Client
from universal_tools.tasks import TaskClient

tasks = TaskClient()
async with Client(target, mode="2026-07-28", extensions=[tasks]) as client:
    created = await tasks.create(client.session, "start_export", export_arguments)
    task_id = created["taskId"]  # Persist in caller-private state.
    current = await tasks.get(client.session, task_id)
    # Present inputRequests through the host's normal approval UI when required.
    # Send real responses with tasks.update(...); never fabricate approval.
```

`TaskClient` explicitly preserves extension result shapes and supplies `Mcp-Name: <taskId>` on HTTP task methods. It does not automatically poll, retry tools or accept forms. The [synthetic server](../tests/fixtures/extensions_mcp.py) is an executable reference with an independent durable fake-job database; it is not a native job implementation.

## Consume downstream Skills safely

Construct one registry per trusted host principal. Assign each source a stable origin identifier from local configuration, never from remote `serverInfo`. Each `SkillSource(origin, connect)` provides a fresh or appropriately scoped async session context manager; construct its SDK client with `extensions=[SkillClient()]`. Credential selection and session lifecycle remain app-owned.

```python
from universal_tools.skill_client import SkillRegistry, SkillSource

registry = SkillRegistry([SkillSource("example-app", connect_example)])
entries = await registry.discover("example-app")
description = registry.describe("example-app", entries[0]["uri"])
# After the host obtains explicit approval for this complete manifest:
registry.approve("example-app", entries[0]["uri"], description["approval_digest"])
raw = await registry.read("example-app", entries[0]["uri"], entries[0]["uri"])
```

The `approve` API records a decision made by the embedding host; it does not manufacture user consent. Discovery, direct `refresh(origin, uri)` and approval retrieve no files. Direct get supports unlisted skill URIs. File reads occur only on demand and are restricted to the approved complete manifest. Origin identity and skill URI form the key, so equal names/URIs from different servers do not share approvals or caches.

Reads verify one returned resource, exact URI, raw byte size, SHA-256 and complete `SKILL.md` frontmatter. The parser rejects duplicate YAML keys, aliases, unsafe paths, normalization conflicts and non-finite metadata. A changed manifest/header revokes approval; failed refreshes and verification failures fail closed. A cached file is hashed again on every use. The memory cache defaults to 32 MiB, is instance-local and is never stored in a host's native skill-discovery directory. The registry limits metadata to 512 entries/32 MiB; each descriptor is at most 4 MiB. Source sessions have a 30-second operation window.

This implementation deliberately rejects `resources: "dynamic"` because it cannot bind approval to complete content. It never executes scripts, follows cross-origin reads or grants frontmatter `allowed-tools`. An embedding must independently gate **all** execution prompted by skill instructions and obtain fresh consent to activate a nested skill. Reading a nested `SKILL.md` as a supporting file does not activate it. Hashes demonstrate consistency with the source's manifest, not trust in that source.

## Publish and aggregate skills without restarting the server

```python
from universal_tools.skill_catalog import SkillCatalog

catalog = SkillCatalog([reviewed_package], page_size=16, directory_read=True)
server = host.create_server(skills=catalog)

# Explicit local administrator action after preparing and reviewing new inputs:
await catalog.publish(
    [replacement_package],
    registry=registry,
    selections=[("example-app", selected_original_skill_uri)],
    expected_revision=catalog.revision,
)
```

Publication builds and validates a complete candidate, then atomically replaces the maps. Failed candidates leave the prior catalog intact. Local files are immutable byte snapshots; disk edits have no effect until publication. Downstream files stay lazy and must retain current registry approval when read. Federation namespaces origins and preserves the skill's original name/frontmatter. It attaches original origin/URI/manifest provenance under `org.codexmcpbroker/provenance`; absolute resource references in instructions are not rewritten or automatically resolved across origins.

`skills/list` and directory reads paginate with revision- and scope-bound cursors. Catalog replacement rejects old cursors so a consumer cannot accidentally mix generations. `resources/directory/read` is advertised only with `directory_read=True`, lists immediate children, and never extends an approved manifest. Live servers publish resource-list changes and the `broker://skills` generation resource. Notifications are best effort: reconnecting consumers refetch the catalog. The standard SDK extension identifiers stay fixed; hot loading arbitrary extension code is outside this API.

## Host publication profiles

The general `MCP_PROFILE` accepts up to 64 skills, 512 files and 16 MiB per skill, with a 256 KiB instruction limit. Local snapshots additionally cap combined file bytes at 64 MiB. Equal names from different origins are legal under this profile; identity is origin plus URI.

`OPENAI_IMPORT_PROFILE` selects the narrower documented importer limits: five uniquely named skills, ten catalog pages, 100 files per skill, 256 KiB of instruction Markdown, 1 MiB per supporting file and 5 MiB per skill. Publication also enforces a conservative 8 MiB combined archive estimate including ZIP overhead. This metadata estimate may reject highly compressible packages that an importer could accept. The actual host importer remains authoritative. Select a subset explicitly rather than silently omitting or renaming skills.

CLI `serve`, `app-serve` and `proxy` accept `--skill-package`, `--skill-profile mcp|openai-import`, `--skill-page-size` and `--skill-directory-read`. These options are local admin choices and are not automatically persisted to plugin configuration. Downstream selection/publication uses the embedding API.

OpenAI currently imports MCP skills as submission-time snapshots. Updating those imported instructions requires scanning and submitting a new plugin version; live catalog publication does not remove that host requirement. See [official OpenAI importer documentation](https://developers.openai.com/plugins/build/mcp-server#import-skills-from-the-mcp-server). Neither protocol tests nor the import profile establish Codex desktop skill activation. Keep [native bundle composition](native-skill-bundles.md) available when packaged skills and reload are needed.

## Structural budgets and acceptance

Service manifests expose `max_argument_depth` (default 64, maximum 128) and `max_argument_nodes` (default 100,000, maximum 1,000,000). Iterative checks run before schema validation, write claims and native dispatch. Object keys count as nodes. Byte limits, HTTP envelope limits and SSE event limits still apply independently. Full-width JSON integers are preserved; these structural limits do not clamp integers to JavaScript header ranges. Schemas, forms and extension metadata also receive bounded shape checks.

Run the [extension verification profile](../VERIFICATION.md#extension-profile) before changing these contracts. Tests cover protocol opt-in, real HTTP task headers, process restart, ambiguous mutations, duplicate suppression, owner denial, terminal immutability, lazy file retrieval, verification failures, origin isolation, live publication and SDK 2.2 execution. Separately assess native worker survival, actual host approval UI, skill selection, execution gates and per-app authorization before any migration.
