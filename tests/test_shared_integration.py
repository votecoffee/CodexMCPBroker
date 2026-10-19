"""Origin-only forms/progress when two frontends reuse one downstream stdio worker."""

import asyncio

from mcp.types import ElicitResult
from test_elicitation_integration import frontends
from test_integration import data, exclusive_app, remote_call
from test_integration import stack as stack
from test_progress_integration import call, clients, progress_app


async def test_shared_forms_change_origin_without_reusing_the_previous_answer(stack):
    seen = [[], []]

    async def first_answer(_, params):
        seen[0].append(params.message)
        return ElicitResult(action="accept", content={"approved": len(seen[0]) == 1})

    async def second_answer(_, params):
        seen[1].append(params.message)
        return ElicitResult(action="decline")

    async with (
        exclusive_app(stack, session_mode="shared", timeout=20),
        frontends(stack[0], first_answer, second_answer) as (first, second),
    ):
        for index, (client, expected) in enumerate(
            ((first, True), (second, False), (first, False))
        ):
            value = data(
                await remote_call(
                    client,
                    "confirm",
                    {"candidate": f"candidate-{index}"},
                    None,
                    request_id=f"shared-confirm-{index}",
                )
            )
            assert value["committed"] is expected and value["commits"] == 1
        assert len(seen[0]) == 2 and len(seen[1]) == 1
        assert seen[0][0].endswith("candidate-0") and seen[0][1].endswith("candidate-2")
        assert seen[1][0].endswith("candidate-1")
        assert data(await remote_call(second, "confirmation_state", {}, None))["commits"] == 1


async def test_shared_worker_does_not_send_previous_or_equal_progress_tokens_to_another_chat(stack):
    async with (
        progress_app(stack, session_mode="shared"),
        clients(stack[0], 2) as ((first, a), (second, b)),
    ):
        results = await asyncio.gather(
            call(first, None, token=0, arguments={"label": "first"}),
            call(second, None, token=0, arguments={"label": "second"}),
        )
        assert data(results[0])["instance"] == data(results[1])["instance"]
        for events, label in ((a, "first"), (b, "second")):
            assert events and all(event.progress_token == 0 for event in events)
            assert all(
                event.message == f"App stateful/progress_work progress:\n{label}"
                for event in events
            )
        counts = (len(a), len(b))
        await call(second, None, arguments={"label": "no progress opt-in"})
        assert counts == (len(a), len(b))
