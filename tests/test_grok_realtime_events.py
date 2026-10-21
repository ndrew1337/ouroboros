"""xAI's native function and caption events through fake sockets."""
import asyncio

import pytest

from ouroboros.contracts.live_conversation import ToolOutputs
from tests.test_grok_openai_protocol import providers as providers, deny_network as deny_network, make, drain


@pytest.mark.parametrize("sources", [("arguments",), ("arguments", "response"), ("response", "arguments")])
def test_native_function_arguments_emit_once_per_call_id(providers, sources):
    async def run():
        adapter, wire = make(providers, "grok")
        call = {"type": "function_call", "name": "read_file", "call_id": "c", "arguments": '{"path":"notes"}'}
        arguments = {**call, "type": "response.function_call_arguments.done"}
        events = {"arguments": arguments, "response": {"type": "response.done", "response": {
            "id": "r", "status": "completed", "output": [call]}}}
        try:
            await adapter.open()
            await adapter.receive()
            calls = []
            for source in sources:
                wire.put(events[source])
                calls.extend(event.data for event in await adapter.receive() if event.kind == "tool_call")
            assert calls == [{key: call[key] for key in ("name", "call_id", "arguments")}]
            assert adapter.waiting_tools == {"c"}
            await adapter.send(ToolOutputs(({"role": "tool", "tool_call_id": "c", "content": "result"},), (), silent=("c",)))
            await drain(adapter, wire)
            assert not adapter.waiting_tools
            wire.put(arguments)  # A repeat after completion must not re-arm the tool.
            assert await adapter.receive() == []
            assert not adapter.waiting_tools
            wire.put({**arguments, "call_id": "next"})
            assert (await adapter.receive())[0].data["call_id"] == "next"
            assert adapter.waiting_tools == {"next"}
            assert not any(event["type"] == "response.create" for event in wire.sent)
        finally:
            await adapter.close()
    asyncio.run(run())
