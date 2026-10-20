"""Real authenticated HTTP, process restart and task-name routing headers."""

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from test_integration import data, free_port
from test_modern_http import connection

from universal_tools.cli import initialize, process_options
from universal_tools.skill_client import SkillRegistry, SkillSource
from universal_tools.tasks import TaskClient


async def test_http_process_restart_retains_handle_and_denies_another_chat(tmp_path):
    state = tmp_path / "state"
    settings = initialize(state, free_port())
    fixture = Path(__file__).with_name("fixtures") / "extensions_mcp.py"
    processes = []
    logs = []

    async def start():
        log = (tmp_path / f"fixture-{len(processes)}.log").open("wb")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, str(fixture), "--state", str(state)],
            stdout=log,
            stderr=log,
            **process_options(),
        )
        processes.append(process)
        async with httpx2.AsyncClient(trust_env=False) as http:
            async with asyncio.timeout(40):
                while True:
                    try:
                        response = await http.get(
                            settings.url + "/health",
                            headers={"Authorization": "Bearer " + settings.token},
                        )
                        if response.status_code == 200:
                            return process
                    except httpx2.HTTPError:
                        pass
                    assert process.poll() is None
                    await asyncio.sleep(0.1)

    tasks = TaskClient()
    try:
        first = await start()
        async with connection(settings, "a" * 32, extensions=[tasks]) as client:
            created = await tasks.create(
                client.session,
                "export",
                {"request_id": "process-crash-job-001", "lose_reply": True},
            )
            task_id = created["taskId"]
            assert data(await client.call_tool("job_count")) == {"jobs": 1}
        first.kill()  # Only this disposable fixture, with synthetic jobs.
        await asyncio.to_thread(first.wait, 10)
        await start()
        async with connection(settings, "b" * 32, extensions=[tasks]) as other:
            with pytest.raises(MCPError, match="unknown or expired"):
                await tasks.get(other.session, task_id)
        async with connection(settings, "a" * 32, extensions=[tasks]) as client:
            pending = await tasks.get(client.session, task_id)
            assert pending["status"] == "input_required"
            await tasks.update(
                client.session,
                task_id,
                {"review": {"action": "accept", "content": {"approved": True}}},
            )
            final = await tasks.get(client.session, task_id)
            assert final["status"] == "completed"
            assert final["result"]["content"][0]["text"] == "exported"
            duplicate = await tasks.create(
                client.session,
                "export",
                {"request_id": "process-crash-job-001", "lose_reply": True},
            )
            assert duplicate["taskId"] == task_id
            assert data(await client.call_tool("job_count")) == {"jobs": 1}
        # Resource retrieval and verification through another fresh HTTP connection.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def skill_connection():
            async with connection(settings, "a" * 32) as client:
                yield client.session

        registry = SkillRegistry([SkillSource("fixture", skill_connection)])
        uri = (await registry.discover("fixture"))[0]["uri"]
        registry.approve("fixture", uri, registry.describe("fixture", uri)["approval_digest"])
        assert (
            await registry.read("fixture", uri, "skill://synthetic/reference.txt")
            == b"unchanged\r\nbytes\n"
        )
    finally:
        async with httpx2.AsyncClient(trust_env=False) as http:
            try:
                await http.post(
                    settings.url + "/shutdown",
                    headers={"Authorization": "Bearer " + settings.token},
                )
            except httpx2.HTTPError:
                pass
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, 15)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
        for log in logs:
            log.close()
