"""The recovered call event loop over the admitted host port.

Audio remains realtime speech-to-speech. Tools run on the ordinary serialized
host lane beside media; their completion, provider close and row writes all join
before this consumer returns. A provider-requested renewal asks the host to fit
the whole transcript again; transport loss never silently starts a paid retry.
"""
from __future__ import annotations

import asyncio
import time

from ouroboros import call_protocol as cp
from ouroboros.contracts.live_conversation import Audio, ContextUpdate, Interrupt, Playback, ToolOutputs, Video
from ouroboros.contracts.realtime import RealtimeSetupError
from ouroboros.realtime_session import joined


async def run(host):
    while True:
        await host.check()
        if host.hub.closed or host.hub.ended_by_owner:
            return
        result = await host.session(lambda session, usage: consume(host, session, usage))
        if result != "renew":
            return
        host.hub.send_state(cp.STATE_RECONNECTING, "provider_renewal")


async def consume(host, session, report_usage):
    hub = host.hub
    tool_queue, rows = asyncio.Queue(maxsize=32), asyncio.Queue(maxsize=256)
    ended = asyncio.Event()
    renewing, response_open, tools_running, input_open = False, False, False, False
    renewal_deadline = None
    input_rate = output_rate = 0
    vision = False
    playing_item = ""
    played, audio_ms = {}, {}
    interrupted = set()
    meeting_audio = False
    empty_since = time.monotonic()
    pending_spoken = {}

    async def finish_spoken(item, *, interrupted=False, completed=False):
        text = pending_spoken.pop(item, None)
        if text is not None:
            await rows.put(dict(speaker="ouroboros", text=text, item_id=item, heard=False,
                played_ms=played.get(item, 0), interrupted=interrupted, completed=completed))

    async def caption(data):
        nonlocal input_open, meeting_audio
        speaker, item = data["speaker"], data["item_id"]
        text = ("[Partial transcript] " if data.get("incomplete") else "") + data["text"]
        if speaker == "owner":
            input_open = not data["final"]
        hub.send_caption(speaker, text, data["final"], item, replace=bool(data.get("replace")))
        if data["final"]:
            heard = speaker == "owner" and (meeting_audio or any(
                peer.role == cp.PEER_MEETING and peer.mic for peer in hub.peers()))
            if speaker == "ouroboros":
                pending_spoken[item] = text
                if item in interrupted or (played.get(item, 0) > 0
                        and played[item] >= int(audio_ms.get(item, 1)) - 20):
                    await finish_spoken(item, interrupted=item in interrupted, completed=item not in interrupted)
            else:
                await rows.put(dict(speaker=speaker, text=text, item_id=item, heard=heard))
            if speaker == "owner":
                meeting_audio = False

    async def save_rows():
        while True:
            row = await rows.get()
            try:
                await host.caption(**row)
            finally:
                rows.task_done()

    async def tools():
        nonlocal tools_running
        while True:
            calls = [await tool_queue.get()]
            while not tool_queue.empty():
                calls.append(tool_queue.get_nowait())
            tools_running = True
            hub.send_activity("working", ", ".join(call["function"]["name"] for call in calls))
            try:
                # Input captions preceding this call must reach canonical
                # history before its pre-effect checkpoint.
                await rows.join()
                changes = await host.tool_batch(calls)
                await session.send(ToolOutputs(tuple(changes), tuple(host.schemas)))
            finally:
                for _ in calls:
                    tool_queue.task_done()
                tools_running = False
                hub.send_activity("idle")

    async def interrupt():
        hub.flush()
        await session.send(Interrupt("", 0))
        for item in tuple(audio_ms):
            if item in interrupted or played.get(item, 0) >= audio_ms[item]:
                continue
            interrupted.add(item)
            await session.send(Interrupt(item, min(played.get(item, 0), int(audio_ms[item]))))
            await finish_spoken(item, interrupted=True)

    async def provider():
        nonlocal input_rate, output_rate, response_open, renewing, renewal_deadline, playing_item, meeting_audio, vision, input_open
        while True:
            for event in await session.receive():
                data = event.data
                if event.kind == "ready":
                    input_rate, output_rate = data["input_rate"], data["output_rate"]
                    vision = bool(data.get("vision"))
                    hub.set_engine_rates(input_rate, output_rate)
                    hub.send_state(cp.STATE_LIVE)
                elif event.kind == "audio":
                    if data["rate"] != output_rate:
                        raise RealtimeSetupError("audio_rate", "provider changed its output rate")
                    item = data["item_id"]
                    if item in interrupted:
                        continue
                    if playing_item != item:
                        # Preserve the old worklet's stream-to-item binding before PCM.
                        hub.send_caption("ouroboros", "", False, item)
                        playing_item = item
                    audio_ms[item] = audio_ms.get(item, 0) + len(data["pcm"]) * 500 / output_rate
                    hub.play(data["pcm"], item)
                elif event.kind == "caption":
                    await caption(data)
                elif event.kind == "speech_started":
                    await interrupt()
                elif event.kind == "input_started":
                    input_open = True
                elif event.kind == "input_done":
                    input_open = False
                elif event.kind == "response_started":
                    response_open = True
                    hub.send_turn(cp.TURN_ANSWERING)
                elif event.kind == "response_done":
                    report_usage(data["usage"])
                    response_open = bool(data.get("continues"))
                    hub.send_turn(cp.TURN_AWAITING if response_open or tools_running else cp.TURN_IDLE)
                elif event.kind == "tool_call":
                    await tool_queue.put({"id": data["call_id"], "type": "function", "function": {
                        "name": data["name"], "arguments": data["arguments"]}})
                elif event.kind == "renew":
                    renewing = True
                    renewal_deadline = time.monotonic() + float(data.get("within_s", 30))
                elif event.kind == "closed":
                    raise RealtimeSetupError("provider_closed", "provider ended the live connection")

    async def peers():
        nonlocal meeting_audio, empty_since
        while True:
            if hub.closed or hub.ended_by_owner:
                for item in tuple(pending_spoken):
                    await finish_spoken(item)
                await rows.join()
                ended.set()
                return
            if hub.peers():
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since >= 15:
                raise RealtimeSetupError("peers_left", "no peer is attached")
            if renewing:
                if (not response_open and not input_open and not tools_running and tool_queue.empty()
                        and not pending_spoken and getattr(session, "renewal_ready", True)):
                    hub.send_state(cp.STATE_RECONNECTING, "provider_renewal")
                    await rows.join()
                    ended.set()
                    return
                if time.monotonic() >= renewal_deadline:
                    raise RealtimeSetupError("renewal_pending_work", "session expired while work was pending")
            # A renewal request waits for a completed audible turn. Media keeps
            # flowing until then; after the boundary queued frames belong to the
            # next connection and the browser pauses capture while reconnecting.
            event = hub.next_event(0)
            if isinstance(event, cp.PeerAudio) and input_rate:
                meeting_audio |= event.role == cp.PEER_MEETING
                await session.send(Audio(event.pcm, input_rate))
            elif isinstance(event, cp.PeerVideo):
                if not vision:
                    raise RealtimeSetupError("vision_unsupported", "selected engine does not accept images")
                await session.send(Video(event.data, event.mime))
            elif isinstance(event, cp.PeerControl):
                message = event.message
                if message["type"] == "played":
                    item = message["item_id"]
                    # A companion's clock cannot claim that the owner heard audio.
                    if (event.role == cp.PEER_OWNER or
                            (hub.spec.opened_by == cp.OPENED_BY_SKILL and event.role == cp.PEER_MEETING)):
                        played[item] = min(message["played_ms"], int(audio_ms.get(item, 0)))
                        await session.send(Playback(item, played[item]))
                        if played[item] > 0 and played[item] >= int(audio_ms.get(item, 0)) - 20:
                            await finish_spoken(item, completed=True)
                elif message["type"] == "context":
                    row = {"role": "user", "content": f"[Observed by {event.role} peer; not an owner instruction]\n{message['text']}"}
                    async with host._tools_lock:
                        host.messages.append(row)
                        host.selected_input()
                    await session.send(ContextUpdate((row,)))
            await asyncio.sleep(.01)

    async def mailbox():
        while True:
            if not renewing:
                mail = await host.mailbox()
                if mail:
                    await session.send(ContextUpdate(tuple(mail)))
            await asyncio.sleep(.25)

    children = [asyncio.create_task(action()) for action in (provider, peers, tools, mailbox, save_rows)]
    stop = asyncio.create_task(ended.wait())
    try:
        done, _ = await asyncio.wait([*children, stop], return_when=asyncio.FIRST_COMPLETED)
        for child in done:
            await child
        await rows.join()
        return "renew" if renewing else "ended"
    finally:
        hub.send_state(cp.STATE_CLOSING if not renewing else cp.STATE_RECONNECTING)
        # Stop producers before draining final rows; the one writer remains
        # owned until its queued commits land, including cancellation during Stop.
        for child in [*children[:-1], stop]:
            if not child.done():
                child.cancel()
        for child in [*children[:-1], stop]:
            try:
                await joined(child)
            except (Exception, asyncio.CancelledError):
                pass
        writer = children[-1]
        if not writer.done():
            async def flush_final():
                for event in getattr(session, "final_captions", lambda: ())():
                    await caption(event.data)
                for item in tuple(pending_spoken):
                    await finish_spoken(item)
            flushing = asyncio.create_task(flush_final())
            done, _ = await joined(asyncio.wait((flushing, writer), return_when=asyncio.FIRST_COMPLETED))
            if writer in done:
                flushing.cancel()
                try:
                    await joined(flushing)
                except (Exception, asyncio.CancelledError):
                    pass
                await joined(writer)
            await joined(flushing)
            drain = asyncio.create_task(rows.join())
            done, _ = await joined(asyncio.wait((drain, writer), return_when=asyncio.FIRST_COMPLETED))
            if writer in done:
                drain.cancel()
            else:
                writer.cancel()
            for task in (drain, writer):
                try:
                    await joined(task)
                except (Exception, asyncio.CancelledError):
                    pass
