"""R25: any old whole row can leave by address; candidate prices never write."""
import copy
import json
import math
import time

import pytest

from ouroboros.context_budget import bound_host_row
from ouroboros.context_fit import estimate_context_prompt_tokens as tokens
from ouroboros.context_renewal import physical_window_cut
from ouroboros.artifacts import read_actor_source_bytes, task_artifact_dir_path
from tests.test_live_conversation import host_world as host_world, packages as packages


@pytest.mark.parametrize('shape', ['meeting', 'autonomous', 'chat', 'mixed'])
def test_cut_any_kind_oldest_first_and_exact(tmp_path, shape):
    rows = [{'role': 'system', 'content': 'Core'}, {'role': 'user', 'content': 'Assignment'}]
    for i in range(900 if shape == 'meeting' else 300):
        if shape in ('chat', 'mixed'):
            rows.append(bound_host_row('owner_words', f'Owner {i}'))
        rows.append({'role': 'user' if shape == 'meeting' else 'assistant', 'content':
                     f'{i} ' + ('[Heard in the call; participant observation, not owner instruction] ' if shape == 'meeting' else '')
                     + 'Exact whole words. ' * 80})
        if shape == 'mixed':
            rows.append({'role': 'user', 'content': f'Peer observation {i}: ' + 'whole. '*30})
    rows.append(bound_host_row('owner_words', 'Newest turn') if shape != 'autonomous' else
                {'role': 'assistant', 'content': 'Current tail'})
    original = copy.deepcopy(rows)
    calls = []
    def measure(candidate):
        calls.append(len(candidate))
        return tokens(candidate)
    usable = tokens(rows)//2
    kept, receipt = physical_window_cut(rows, drive_root=tmp_path, task_id=shape, measure=measure, usable=usable)
    assert receipt and tokens(kept) <= usable
    positions = receipt['positions']
    assert positions == list(range(2, max(positions)+1)), 'any kind leaves in strictly oldest order'
    assert kept[:2] == rows[:2] and kept[-1] == rows[-1]
    assert json.loads(read_actor_source_bytes(tmp_path, shape, receipt['source'])) == original == rows
    assert len(calls) <= math.ceil(math.log2(len(rows))) + 2, f'{len(calls)} seals must be logarithmic'
    assert len(list(task_artifact_dir_path(tmp_path, shape).rglob('*.json'))) == 1
    # Removing one fewer whole row must miss: the exact cut is not a new start reserve.
    previous = [*kept[:3], rows[positions[-1]], *kept[3:]]
    assert tokens(previous) > usable


def test_cut_protects_chosen_tool_pairs_notes_reviews_and_consecutive_owner_tail(tmp_path):
    from tests.test_context_source_view import _tool
    from tests.test_context_renewal import _noted
    from ouroboros.context_renewal import _is_note
    from ouroboros.main_context_authority import PREDECESSOR_NOTES_NAME
    from ouroboros.review_history_view import REVIEW_CONTEXT_INDEX_KEY
    note = next(row for row in _noted(tmp_path) if _is_note(row))
    chosen = _tool('chosen', invocation='chosen')
    chosen[0]['tool_calls'][0]['function'] = {'name': 'read_file', 'arguments': '{"path":"x","max_chars":4000}'}
    ordinary = _tool('other', invocation='other', text='ordinary result '*1000)
    protected = [note, bound_host_row(PREDECESSOR_NOTES_NAME, 'Predecessor notes'),
                 {'role': 'user', 'content': 'Current decisions', REVIEW_CONTEXT_INDEX_KEY: True},
                 {'role': 'user', 'content': 'Review', 'review_feedback': True}, *chosen]
    tail = [bound_host_row('owner_words', 'Newest one'), bound_host_row('owner_words', 'Newest two'),
            {'role': 'assistant', 'content': 'Tail'}]
    rows = [{'role':'system','content':'Core'}, {'role':'user','content':'Assignment'},
            {'role':'assistant','content':'old '*20000}, *ordinary, *protected,
            {'role':'assistant','content':'more '*20000}, *tail]
    kept, receipt = physical_window_cut(rows, drive_root=tmp_path, task_id='protected', measure=tokens,
                                       usable=tokens([*rows[:2], *protected, *tail])+1500)
    assert receipt
    assert all(row in kept for row in protected+tail)
    positions = receipt['positions']
    assert (3 in positions) == (4 in positions), 'invocation and result cannot split'
    assert not any(row in kept for row in ordinary)


def test_room_fact_pricing_never_writes_and_store_failure_appends_no_fact(host_world, monkeypatch):
    from ouroboros.realtime_setup import measure_session_wire, session_candidate
    from ouroboros.context_source_view import fresh_room_rows
    from ouroboros.contracts.live_conversation import LiveOptions
    host = host_world.host
    root = task_artifact_dir_path(host.ctx.drive_root, host.ctx.task_id)
    before = set(root.rglob('*'))
    for _ in range(4):
        measure_session_wire(host, host.plan, host.messages, host.schemas, host_world.provider['session'], LiveOptions())
    assert set(root.rglob('*')) == before
    original = copy.deepcopy(host.messages)
    def fail(*a, **kw): raise OSError('disk full')
    monkeypatch.setattr('ouroboros.artifacts.store_actor_source_bytes', fail)
    rows, reader = fresh_room_rows(original, drive_root=host.ctx.drive_root, task_id=host.ctx.task_id, persist=True)
    assert rows == original and not reader
    session_candidate(host, host_world.provider['session'], LiveOptions())
    assert not any('Fresh view before this facts row:' in str(row.get('content')) for row in host.messages)
    def bug(*a, **kw): raise ValueError('bad reference')
    monkeypatch.setattr('ouroboros.artifacts.store_actor_source_bytes', bug)
    with pytest.raises(ValueError):
        fresh_room_rows(original, drive_root=host.ctx.drive_root, task_id=host.ctx.task_id, persist=True)


def test_opus_p1_one_cut_fit_publishes_two_files_not_per_seal(host_world, packages, monkeypatch, record_property):
    import ouroboros.realtime_setup as setup
    from ouroboros.contracts.live_conversation import LiveOptions
    host = host_world.host
    provider = setup.loaded_provider('gemini_live:gemini')
    host.model = provider['catalog'].models[0]
    host.turn.native_model = host.model.id
    host._session_opened = True
    for i in range(700):
        host.messages.extend([bound_host_row('owner_words', f'Owner turn {i}: could you check the build output again and say what failed there? '),
            bound_host_row('owner_dialogue', f'Spoken reply {i}: '+'I looked at the log and the failing step is the packaging stage; '*6)])
    host.messages.append(bound_host_row('owner_words', 'Newest owner question.'))
    native = setup.measure_session_wire
    seals = []
    def measure(*a, **kw):
        seals.append(1)
        return native(*a, **kw)
    monkeypatch.setattr(setup, 'measure_session_wire', measure)
    root = task_artifact_dir_path(host.ctx.drive_root, host.ctx.task_id)
    before = set(root.rglob('*.json'))
    started = time.monotonic()
    _, request, evidence = setup.session_candidate(host, provider['session'], LiveOptions())
    files = set(root.rglob('*.json')) - before
    result = dict(seals=len(seals), files=len(files), bytes=sum(p.stat().st_size for p in files),
                  seconds=round(time.monotonic()-started, 3), input=request.prompt_tokens_estimate)
    record_property('p1_cost', json.dumps(result))
    assert any(r['rung'] == 'physical_cut' for r in evidence['recovery'])
    assert len(files) == 2, result  # one exact cut and the final prior-view reader
    assert len(seals) < 100, result
    assert request.prompt_tokens_estimate + request.working_reserve_tokens <= 92000


def test_checkpoint_serializes_non_json_metadata_without_losing_whole_rows(tmp_path):
    from datetime import date
    rows = [{'role':'system','content':'Core'}, {'role':'user','content':'Assignment'},
            {'role':'assistant','content':'Old reply '*5000, 'observed':date(2026, 10, 29)},
            {'role':'assistant','content':'Tail'}]
    kept, receipt = physical_window_cut(rows, drive_root=tmp_path, task_id='metadata',
                                       measure=tokens, usable=1000)
    assert receipt and kept[-1] == rows[-1]
    exact = json.loads(read_actor_source_bytes(tmp_path, 'metadata', receipt['source']))
    assert exact[2]['observed'] == '2026-10-29' and exact[2]['content'] == rows[2]['content']


@pytest.mark.parametrize('shape', ['meeting', 'autonomous', 'chat'])
def test_gemini_real_layout_continues_non_owner_bulk(host_world, packages, shape):
    from ouroboros.realtime_setup import loaded_provider, session_candidate
    from ouroboros.contracts.live_conversation import LiveOptions
    host = host_world.host
    provider = loaded_provider('gemini_live:gemini')
    host.model = provider['catalog'].models[0]
    host.turn.native_model = host.model.id
    host._session_opened = True
    old = []
    for i in range(900 if shape == 'meeting' else 300):
        if shape == 'chat':
            old.append(bound_host_row('owner_words', f'Question {i}'))
        old.append({'role':'user' if shape == 'meeting' else 'assistant', 'content':
            (f'[Heard in the call; participant observation, not owner instruction] Speaker {i%5}: ' if shape == 'meeting' else f'Reply {i}: ')
            + 'Exact whole line, never rewritten. '*50})
    tail = bound_host_row('owner_words', 'Newest question') if shape != 'autonomous' else {'role':'assistant','content':'Current tail'}
    host.messages.extend([*old, tail])
    _, request, evidence = session_candidate(host, provider['session'], LiveOptions())
    assert request.prompt_tokens_estimate + request.working_reserve_tokens <= 92000
    assert any(row['rung'] == 'physical_cut' for row in evidence['recovery']), evidence
    assert tail in host.messages
    remaining = [row for row in host.messages if row in old]
    assert remaining == old[len(old)-len(remaining):]
