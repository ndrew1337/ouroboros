"""Input overflow is measurable for the whole call, including failing teardown."""
import json
from contextlib import nullcontext

import pytest

from ouroboros import call_peers, call_protocol as cp
from ouroboros.contracts.realtime import RealtimeSetupError
from tests.test_live_conversation import host_world as host_world


def fill_input(hub, rate, excess):
    hub.join(cp.PeerInfo('owner', cp.PEER_OWNER, True, True), rate)
    frame = cp.AudioFrame(cp.FRAME_AUDIO_IN, hub.epoch, 0, rate, 0, b'\x01\x00' * rate)
    assert hub.receive_audio('owner', frame)
    if excess:
        frame = cp.AudioFrame(cp.FRAME_AUDIO_IN, hub.epoch, 1, rate, 0, b'\x02\x00' * excess)
        assert hub.receive_audio('owner', frame)
    assert not hub.receive_audio('owner', frame)  # a duplicate cannot add a drop


@pytest.mark.parametrize('excess', [0, 137])
def test_input_overflow_totals_survive_peer_rejoin_rate_change_and_close(excess):
    hub = call_peers.LiveCallHub(cp.CallSpec('audio-evidence', 1, 'fixture', 'voice'))
    samples, milliseconds = 0, 0.0
    assert hub.input_audio_evidence() == {'dropped_samples': 0, 'dropped_ms': 0.0}
    for rate in (24000, 16000):
        hub.set_engine_rates(rate, rate)
        assert hub.input_audio_evidence() == {'dropped_samples': samples, 'dropped_ms': milliseconds}
        fill_input(hub, rate, excess)
        samples += excess
        milliseconds += excess * 1000 / rate
        expected = {'dropped_samples': samples, 'dropped_ms': milliseconds}
        assert hub.input_audio_evidence() == expected
        # Existing one-second capacity and oldest-sample discard are unchanged.
        queued = hub._peers['owner'].tracks[0].samples
        assert len(queued) == rate
        assert queued[0] == 1 and queued[-1] == (2 if excess else 1)
        hub.leave('owner')
        assert hub.input_audio_evidence() == expected
    hub.close('finished')
    assert hub.input_audio_evidence() == expected


def test_input_overflow_counts_converted_samples_not_capture_samples():
    hub = call_peers.LiveCallHub(cp.CallSpec('resampled-evidence', 1, 'fixture', 'voice'))
    hub.join(cp.PeerInfo('owner', cp.PEER_OWNER, True, True), 16000)
    # 16,000 capture samples yield 23,999 samples at the hub's 24 kHz rate.
    assert hub.receive_audio('owner', cp.AudioFrame(cp.FRAME_AUDIO_IN, 0, 0, 16000, 0, b'\x01\x00' * 16000))
    assert hub.input_audio_evidence() == {'dropped_samples': 0, 'dropped_ms': 0.0}
    # One more capture sample yields two converted samples, overflowing by one.
    assert hub.receive_audio('owner', cp.AudioFrame(cp.FRAME_AUDIO_IN, 0, 1, 16000, 0, b'\x01\x00'))
    assert hub.input_audio_evidence() == {'dropped_samples': 1, 'dropped_ms': 1000 / 24000}
    hub.close('finished')


def admit_run(host, monkeypatch, ending='finished'):
    monkeypatch.setitem(call_peers._HUBS, host.turn.spec.call_id, host.hub)
    published = []
    def publish(event):
        assert host.hub.closed and host.turn._closed
        published.append(event)
    monkeypatch.setattr(host.turn, 'publish', publish)
    async def handler(port):
        if ending == 'owner_stop':
            raise RealtimeSetupError('owner_stop', 'The owner stopped this call.')
        if ending == 'call_failed':
            raise RuntimeError('synthetic call failure')
    monkeypatch.setitem(host.turn.feature, 'handler', handler)
    return published


def audio_events(host):
    if not host.wire_path.exists():
        return []
    events = [json.loads(json.loads(line)['raw']) for line in host.wire_path.read_text().splitlines()]
    return [event for event in events if event.get('type') == 'call.input_audio']


@pytest.mark.parametrize('ending', ['finished', 'owner_stop', 'call_failed'])
@pytest.mark.parametrize('excess', [0, 137])
def test_call_teardown_records_input_overflow_on_end_stop_and_failure(host_world, monkeypatch, ending, excess):
    host = host_world.host
    published = admit_run(host, monkeypatch, ending)
    fill_input(host.hub, 24000, excess)
    result = host.turn.run(host.ctx, host.messages, host.tools, task=host.task, initial_effort='')
    assert audio_events(host) == [{'type': 'call.input_audio',
                                  'dropped_samples': excess, 'dropped_ms': excess * 1000 / 24000}]
    assert result[1]['reason_code'] == ending
    assert host.hub.closed and host.turn._closed
    assert call_peers.get_hub(host.turn.spec.call_id) is None
    assert len(published) == 1 and published[0]['reason'] == ending


@pytest.mark.parametrize('write_fails', [False, True])
def test_input_evidence_write_cannot_bypass_close_and_finished_publication(host_world, monkeypatch, write_fails):
    from ouroboros.live_conversation import ConversationHost
    host = host_world.host
    published = admit_run(host, monkeypatch)
    fill_input(host.hub, 24000, 137)
    attempted = []
    original = ConversationHost.observe
    def observe(port, direction, raw):
        event = json.loads(raw)
        if event.get('type') == 'call.input_audio':
            assert direction == 'host'
            attempted.append(event)
            if write_fails:
                raise OSError('synthetic input evidence write failure')
        return original(port, direction, raw)
    monkeypatch.setattr(ConversationHost, 'observe', observe)
    with pytest.raises(OSError, match='synthetic input evidence write failure') if write_fails else nullcontext():
        host.turn.run(host.ctx, host.messages, host.tools, task=host.task, initial_effort='')
    assert attempted == [{'type': 'call.input_audio', 'dropped_samples': 137, 'dropped_ms': 137 * 1000 / 24000}]
    assert bool(audio_events(host)) is (not write_fails)
    assert host.hub.closed and host.turn._closed
    assert call_peers.get_hub(host.turn.spec.call_id) is None
    assert published == [{'type': 'conversation_finished', 'chat_id': host.turn.spec.chat_id,
                         'call_id': host.turn.spec.call_id, 'task_id': host.ctx.task_id, 'reason': 'finished'}]
