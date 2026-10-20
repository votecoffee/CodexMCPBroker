"""Synthetic durable Tasks and Skills server. Never opens a native application."""

import argparse
import json
import secrets
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--state", type=Path, required=True)
parser.add_argument("--framework-root")
parser.add_argument("--sdk-root")
parser.add_argument("--stdio", action="store_true")
args = parser.parse_args()
if args.framework_root:
    sys.path.insert(0, args.framework_root)
if args.sdk_root:
    sys.path.insert(0, args.sdk_root)
    from importlib.metadata import version

    assert version("mcp") == "2.2.0"

from mcp.types import ToolAnnotations  # noqa: E402

from universal_tools.extensions import ExtensionServer  # noqa: E402
from universal_tools.journal import SqliteJournal  # noqa: E402
from universal_tools.models import BrokerError, load_settings  # noqa: E402
from universal_tools.skill_catalog import SkillCatalog  # noqa: E402
from universal_tools.tasks import TaskExtension, TaskObservation, TaskRoute, TaskStore  # noqa: E402

FORM = {
    "method": "elicitation/create",
    "params": {
        "mode": "form",
        "message": "Approve synthetic export?",
        "requestedSchema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
        },
    },
}


class DurableJobs(SqliteJournal):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, result TEXT)"
            )

    async def authorize(self, context, handle):
        if not context.principal:
            raise BrokerError("synthetic principal required")

    async def start(self, context, arguments):
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?, 'review', NULL)", (context.task_id,))
        if arguments.get("lose_reply"):
            raise ConnectionError("synthetic lost start response")
        return context.task_id

    async def inspect(self, context, handle, pending):
        with self.connect() as db:
            row = db.execute(
                "SELECT state, result FROM jobs WHERE id=?", (context.task_id,)
            ).fetchone()
        if row is None:
            raise BrokerError("app reconciliation required; do not start another job")
        if row[0] == "review":
            return TaskObservation(
                "input_required",
                input_requests={"review": FORM},
                handle=context.task_id,
                reconciled=True,
            )
        return TaskObservation("completed", result=json.loads(row[1]), reconciled=True)

    async def update(self, context, handle, responses):
        answer = responses["review"]
        accepted = (
            answer["action"] == "accept" and answer.get("content", {}).get("approved") is True
        )
        result = {
            "content": [{"type": "text", "text": "exported" if accepted else "declined"}],
            "isError": not accepted,
        }
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='completed', result=? WHERE id=? AND state='review'",
                (json.dumps(result), context.task_id),
            )

    async def cancel(self, context, handle):
        pass  # Cooperative intent only; no claim that this fake job stopped.


jobs = DurableJobs(args.state / "jobs.sqlite3")


def validate(arguments):
    if (
        set(arguments) - {"request_id", "lose_reply"}
        or not isinstance(arguments.get("request_id"), str)
        or type(arguments.get("lose_reply", False)) is not bool
    ):
        raise BrokerError("invalid synthetic job arguments")


tasks = TaskExtension(
    TaskStore(args.state / "tasks.sqlite3"), {"export": TaskRoute(jobs, "v1", validate)}
)
skill_root = args.state / "synthetic"
skill_root.mkdir(parents=True, exist_ok=True)
(skill_root / "SKILL.md").write_bytes(
    b"---\nname: synthetic\ndescription: Synthetic fixture\n---\nRead reference.txt\n"
)
(skill_root / "reference.txt").write_bytes(b"unchanged\r\nbytes\n")
skills = SkillCatalog([skill_root], directory_read=True)
server = ExtensionServer(
    "Synthetic extensions",
    extensions=[tasks, skills],
    resource_providers=[skills],
    frontend_identity="a" * 32 if args.stdio else None,
)


@server.tool(annotations=ToolAnnotations(read_only_hint=False))
async def export(request_id: str, lose_reply: bool = False) -> dict:
    raise AssertionError("the Task adapter must own this operation")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def job_count() -> dict:
    with jobs.connect() as db:
        return {"jobs": db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]}


if args.stdio:
    server.run()
else:
    from contextlib import asynccontextmanager

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse

    from universal_tools.daemon import InstanceLock, ProtectedApp

    settings = load_settings(args.state)
    base = server.streamable_http_app(host="127.0.0.1", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = Starlette(routes=base.routes, lifespan=lifespan)

    class NameGuard:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            headers = dict(scope.get("headers", []))
            if headers.get(b"mcp-method", b"").startswith(b"tasks/") and not headers.get(
                b"mcp-name"
            ):
                return await JSONResponse(
                    {"error": "task routing header required"}, status_code=400
                )(scope, receive, send)
            return await self.app(scope, receive, send)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=settings.port, log_level="warning", access_log=False
    )
    http = uvicorn.Server(config)
    http.config.app = ProtectedApp(NameGuard(app), settings, http, secrets.token_hex(16))
    with InstanceLock(args.state / "fixture.lock"):
        http.run()
