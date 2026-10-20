"""The extension adapters execute on the SDK 2.2 floor in a separate process."""

from contextlib import asynccontextmanager
from pathlib import Path

from mcp import Client, StdioServerParameters
from test_integration import data
from test_tasks_extension import host_form_ui

import universal_tools
from universal_tools.skill_client import SkillClient, SkillRegistry, SkillSource
from universal_tools.tasks import TaskClient


async def test_sdk22_tasks_and_skills(tmp_path, sdk2_python, sdk22_root):
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/extensions_mcp.py"
    target = StdioServerParameters(
        command=str(sdk2_python),
        args=[
            str(fixture),
            "--state",
            str(tmp_path),
            "--stdio",
            "--sdk-root",
            str(sdk22_root),
            "--framework-root",
            str(Path(universal_tools.__file__).parents[1]),
        ],
    )
    tasks = TaskClient()
    async with Client(
        target, mode="2026-07-28", extensions=[tasks], elicitation_callback=host_form_ui, cache=None
    ) as client:
        created = await tasks.create(client.session, "export", {"request_id": "sdk22-task-001"})
        task_id = created["taskId"]
        assert (await tasks.get(client.session, task_id))["status"] == "input_required"
        await tasks.update(client.session, task_id, {"review": {"action": "decline"}})
        final = await tasks.get(client.session, task_id)
        assert final["status"] == "completed" and final["result"]["isError"] is True
        assert data(await client.call_tool("job_count")) == {"jobs": 1}

    @asynccontextmanager
    async def connect():
        async with Client(
            target, mode="2026-07-28", extensions=[SkillClient()], cache=None
        ) as client:
            yield client.session

    registry = SkillRegistry([SkillSource("sdk22-fixture", connect)])
    uri = (await registry.discover("sdk22-fixture"))[0]["uri"]
    registry.approve(
        "sdk22-fixture", uri, registry.describe("sdk22-fixture", uri)["approval_digest"]
    )
    assert (
        await registry.read("sdk22-fixture", uri, "skill://synthetic/reference.txt")
        == b"unchanged\r\nbytes\n"
    )
