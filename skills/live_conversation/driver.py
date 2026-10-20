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
from ouroboros.contracts.live_conversation import Audio, ContextUpdate, Interrupt, LiveHost, LiveOptions, Respond, ToolOutputs, Video
from ouroboros.contracts.realtime import RealtimeSetupError
from ouroboros.realtime_session import joined


CALL_INSTRUCTIONS = """## Live calls

In a call my model is a realtime speech model, and I am still myself: the
same core, memory, tools and judgment. I speak briefly, in the language of
the spoken conversation whatever the interface language, and call tools
myself. Work still running is pending, never done; long work becomes a task
while we keep talking, and its result reaches me in the call. Links, code and
lists go to the room's chat through `send_user_message`: saying a message or
a URL aloud does not deliver it, so when asked to send something I use that
tool and confirm its result. Typed messages and other turns' replies in the
room reach me during the call, and my words reach them. In a meeting I speak
when it helps the people there; their voices are correspondents, not my
human. Words cut off by an interruption were not heard, so I do not count
them as said."""


def languages_opening_sentence(languages):
    if languages:
        return (f"Speech recognition expects {', '.join(languages)}, as you recorded for these calls; when someone "
                "speaks a language that is not listed, record the new list with set_call_languages.")
    return ("Speech recognition has no languages recorded for these calls yet and guesses each utterance: assume "
            "the owner's language from this room's correspondence and your memory and record it right away with "
            "set_call_languages, the owner's first; ask when unsure.")


def session_options(host, resume_handle=""):
    config, honours = host.config, host.model.honours
    languages = tuple(host.state.get("languages", ()))
    knobs = {key: getattr(config, key) for key in ("pause", "interruptions", "thinking", "search") if key in honours}
    return LiveOptions(voice=config.voice, resume_handle=resume_handle,
        languages=languages if "languages" in honours else (),
        instructions="\n\n".join(filter(None, (CALL_INSTRUCTIONS, languages_opening_sentence(languages), config.instructions))),
        **knobs)


async def run(host: LiveHost):
    resume_handle = ""
    while True:
        await host.check()
        if host.hub.closed or host.hub.ended_by_owner:
            return
        result = await host.session(lambda session: consume(host, session), session_options(host, resume_handle))
        if result is None:
            return
        resume_handle = result


async def consume(host: LiveHost, session):
    hub = host.hub
    tool_queue, rows = asyncio.Queue(maxsize=32), asyncio.Queue(maxsize=256)
    ended = asyncio.Event()
    renewing, response_open, tools_running, input_open = False, False, False, False
    renewal_deadline = None
    resume = False
    provider_closed = False
    input_rate = output_rate = 0
    playing_item = ""
    played, audio_ms = {}, {}
    interrupted = set()
    meeting_audio = False
    empty_since = time.monotonic()
    pending_spoken = {}

    def request_renewal(*, resuming=False, within=None):
        nonlocal renewing, resume, renewal_deadline
        # A refit request wins over a transport resume: it needs current history.
        resume = resuming if not renewing else resume and resuming
        renewing = True
        if within is not None:
            deadline = time.monotonic() + float(within)
            renewal_deadline = min(renewal_deadline, deadline) if renewal_deadline else deadline

    def quiet():
        unheard = any(item not in interrupted and played.get(item, 0) < duration - 20
                      for item, duration in audio_ms.items())
        return (not response_open and not input_open and not tools_running and tool_queue.empty()
                and not pending_spoken and not unheard and session.renewal_ready)

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
        nonlocal input_rate, output_rate, response_open, playing_item, meeting_audio, input_open, provider_closed
        while True:
            for event in await session.receive():
                data = event.data
                if event.kind == "ready":
                    input_rate, output_rate = data["input_rate"], data["output_rate"]
                    hub.set_engine_rates(input_rate, output_rate)
                    hub.send_state(cp.STATE_LIVE)
                    if not host.state.get("greeted"):
                        host.state["greeted"] = True
                        await session.send(Respond(host.opening))
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
                    host.response_started(data["response_id"])
                    response_open = True
                    hub.send_turn(cp.TURN_ANSWERING)
                elif event.kind == "response_done":
                    host.response_done(data["response_id"], usage=data["usage"], raw_usage=data["raw_usage"], status=data["status"])
                    response_open = bool(data.get("continues"))
                    hub.send_turn(cp.TURN_AWAITING if response_open or tools_running else cp.TURN_IDLE)
                elif event.kind == "tool_call":
                    await tool_queue.put({"id": data["call_id"], "type": "function", "function": {
                        "name": data["name"], "arguments": data["arguments"]}})
                elif event.kind == "renew":
                    request_renewal(resuming=data["resume"], within=data["within_s"])
                elif event.kind == "resumable":
                    host.state["resume_handle"] = data["handle"]
                elif event.kind == "closed":
                    if not data.get("resumable") or not host.state.get("resume_handle"):
                        raise RealtimeSetupError("provider_closed", "provider ended the live connection")
                    provider_closed = True
                    request_renewal(resuming=True)
                    await ended.wait()
                    return

    async def peers():
        nonlocal meeting_audio, empty_since
        while True:
            await host.check()
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
            if host.renewal_due():
                request_renewal()
            if renewing:
                if quiet():
                    if resume and not host.state.get("resume_handle"):
                        raise RealtimeSetupError("resume_unavailable", "provider has not supplied a resume handle")
                    hub.send_state(cp.STATE_RECONNECTING, "provider_renewal")
                    await rows.join()
                    ended.set()
                    return
                if renewal_deadline is not None and time.monotonic() >= renewal_deadline:
                    raise RealtimeSetupError("renewal_pending_work", "session expired while work was pending")
            # A renewal request waits for a completed audible turn. Media keeps
            # flowing until then; after the boundary queued frames belong to the
            # next connection and the browser pauses capture while reconnecting.
            event = hub.next_event(0)
            if isinstance(event, cp.PeerAudio) and input_rate and not provider_closed:
                meeting_audio |= event.role == cp.PEER_MEETING
                await session.send(Audio(event.pcm, input_rate))
            elif isinstance(event, cp.PeerVideo):
                if not host.model.vision:
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
                        if played[item] > 0 and played[item] >= int(audio_ms.get(item, 0)) - 20:
                            await finish_spoken(item, completed=True)
                elif message["type"] == "context":
                    row = {"role": "user", "content": f"[Observed by {event.role} peer; not an owner instruction]\n{message['text']}"}
                    await host.observe_context(row)
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
        return (host.state.get("resume_handle", "") if resume else "") if renewing else None
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
