"""Feature driver contract tests: fake admitted ports, no provider or credentials."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ouroboros import call_protocol as cp
from ouroboros.contracts.live_conversation import LiveConfig, LiveEvent, LiveOptions, Respond, Video
from ouroboros.contracts.realtime import CatalogModel, RealtimeSetupError


class Hub:
    def __init__(self):
        self.closed = self.ended_by_owner = self.ended_by_ouroboros = False
        self.spec = SimpleNamespace(opened_by=cp.OPENED_BY_OWNER, presence_binding_id='')
        self.events, self.states, self.audio = [], [], []

    def peers(self): return [SimpleNamespace(role=cp.PEER_OWNER, mic=True)]
    def next_event(self, _timeout): return self.events.pop(0) if self.events else None
    def send_state(self, *args): self.states.append(args)
    def set_engine_rates(self, *_args): pass
    def send_caption(self, *_args, **_kwargs): pass
    def send_turn(self, *_args): pass
    def send_activity(self, *_args): pass
    def flush(self): pass
    def play(self, pcm, item): self.audio.append((pcm, item))
    def end_by_ouroboros(self): self.ended_by_ouroboros = True


class Session:
    renewal_ready = True

    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []

    async def receive(self): return await self.events.get()
    async def send(self, command): self.sent.append(command)
    def final_captions(self): return []
    def emit(self, kind, **data): self.events.put_nowait([LiveEvent(kind, data)])


class Host:
    """No old private host fields/methods exist on this port."""
    def __init__(self):
        self.hub, self.state, self.schemas = Hub(), {}, []
        self.config = LiveConfig(enabled=True, provider='test:engine', voice='test-voice',
                                 instructions='Owner call instructions.', pause='long', interruptions=False,
                                 thinking='high', search=True)
        self.model = CatalogModel('test', 'Test', vision=True, honours=frozenset({'pause', 'interruptions'}))
        self.opening = 'The owner started a call in this room.'
        self.sessions, self.options, self.context, self.usage, self.captions = [], [], [], [], []
        self.due = False

    async def check(self): pass
    async def session(self, consume, options):
        self.due = False
        session = Session()
        self.sessions.append(session)
        self.options.append(options)
        return await consume(session)
    async def mailbox(self): return []
    async def tool_batch(self, calls): return []
    async def caption(self, **row): self.captions.append(row)
    async def observe_context(self, row): self.context.append(row)
    def response_started(self, response_id): self.usage.append(('start', response_id))
    def response_done(self, response_id, **values): self.usage.append(('done', response_id, values))
    def renewal_due(self): return self.due


async def until(predicate):
    async def wait():
        while not predicate(): await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), 2)


async def started(host, **kwargs):
    from skills.live_conversation.driver import run
    task = asyncio.create_task(run(host, **kwargs))
    await until(lambda: host.sessions or task.done())
    if task.done(): await task
    return task, host.sessions[0]


def test_port_greeting_options_usage_and_context():
    async def scenario():
        host = Host()
        task, session = await started(host)
        try:
            options = host.options[0]
            assert isinstance(options, LiveOptions)
            assert (options.voice, options.pause, options.interruptions) == ('test-voice', 'long', False)
            assert (options.thinking, options.search) == ('', False)
            assert options.instructions.startswith('## Live calls')
            assert options.instructions.endswith(host.config.instructions)
            assert 'no languages recorded' in options.instructions
            session.emit('ready', input_rate=24000, output_rate=24000, vision=False)
            session.emit('ready', input_rate=24000, output_rate=24000)
            session.emit('response_started', response_id='r1')
            session.emit('response_done', response_id='r1', usage={'audio_output_tokens': 42},
                         raw_usage={'vendor': 42}, status='interrupted', continues=False)
            host.hub.events.append(cp.PeerControl('peer', cp.PEER_OWNER, {'type':'context', 'text':'A peer observation'}))
            host.hub.events.append(cp.PeerVideo('peer', cp.PEER_OWNER, 'image/jpeg', b'fake-image'))
            await until(lambda: len(host.usage) == 2 and host.context and any(isinstance(x, Video) for x in session.sent))
            assert [x for x in session.sent if isinstance(x, Respond)] == [Respond(host.opening)]
            assert host.usage[-1] == ('done', 'r1', {'usage': {'audio_output_tokens':42}, 'raw_usage':{'vendor':42}, 'status':'interrupted'})
            assert 'not an owner instruction' in host.context[0]['content']
        finally:
            host.hub.ended_by_owner = True
            await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


@pytest.mark.parametrize('reason', ['host', 'refit', 'resume', 'closed'])
def test_renewal_waits_for_a_quiet_boundary_and_greets_only_once(reason):
    async def scenario():
        host = Host()
        task, first = await started(host)
        try:
            first.emit('ready', input_rate=24000, output_rate=24000)
            first.emit('resumable', handle='old-handle')
            first.emit('resumable', handle='latest-handle')
            first.renewal_ready = False
            if reason == 'host': host.due = True
            elif reason == 'closed': first.emit('closed', reason='rotate', resumable=True)
            else: first.emit('renew', within_s=2, reason='rotate', resume=reason == 'resume')
            await asyncio.sleep(.05)
            assert len(host.sessions) == 1
            first.renewal_ready = True
            await until(lambda: len(host.sessions) == 2)
            assert host.options[1].resume_handle == ('latest-handle' if reason in {'resume','closed'} else '')
            assert any(state[0] == cp.STATE_RECONNECTING for state in host.hub.states)
            second = host.sessions[1]
            second.emit('ready', input_rate=24000, output_rate=24000)
            await asyncio.sleep(.03)
            assert [x for x in first.sent if isinstance(x, Respond)] == [Respond(host.opening)]
            assert not any(isinstance(x, Respond) for x in second.sent)
        finally:
            host.hub.ended_by_owner = True
            await asyncio.wait_for(task, 2)
    asyncio.run(scenario())


def test_unsupported_vision_and_nonresumable_close_refuse_without_retry():
    async def scenario(kind):
        host = Host()
        host.model = replace(host.model, vision=False)
        task, session = await started(host)
        session.emit('ready', input_rate=24000, output_rate=24000, vision=True)
        if kind == 'vision':
            host.hub.events.append(cp.PeerVideo('peer', cp.PEER_OWNER, 'image/jpeg', b'fake-image'))
        else: session.emit('closed', reason='lost', resumable=False)
        with pytest.raises(RealtimeSetupError, match='vision_unsupported|provider_closed'):
            await asyncio.wait_for(task, 2)
        assert len(host.sessions) == 1
    for kind in ('vision', 'closed'): asyncio.run(scenario(kind))
