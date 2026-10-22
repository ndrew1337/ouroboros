"""Owner-shaped counts and measured text lengths; no private owner content."""
from dataclasses import replace
import json
from types import SimpleNamespace

from ouroboros import context_fit as cf, memory_view as mv
from ouroboros.section_floor import HostSection
from tests import _memory_view_synthetic as syn

PROTECTED = {'system': 34800, 'bible': 51900, 'identity': 46000,
             'shared_understanding': 38000, 'scratchpad': 14400, 'memory_marks': 7700}
HOST = {'knowledge_index': 20000, 'runtime': 15200, 'subagents': 10400,
        'review_continuity': 7200, 'installed_skills': 3200, 'review_ledger': 3200}


def text(label, size):
    return (label + ' ' + 'x' * size)[:size]


def snapshot(rows=158, pages=113):
    mine = round(rows * 115 / 158)
    lane = [syn.spoken(i, 'ouroboros' if i < mine else 'human', 1900) for i in range(rows)]
    for row in lane[mine:]:
        row.update(owner_side=True, carried=False)
    lane += [{**syn.spoken(rows + i, 'human', 350), 'owner_side': True, 'immune': True}
             for i in range(29)]
    room = syn.room('1', lane1=lane, lane2=163, notes=6, under=5)
    for note in room['notes']:
        note['text'] = text('Protected own unsealed note.', 3700)
    live = [syn.live_room(i, notes=(1 + (i < 7)) if i < 66 else 0) for i in range(228)]
    for other in live:
        other['label'] = text('Project room ' + other['room_id'], 220)
        for note in other['notes']:
            note['text'] = text('Unsealed project note.', 1600)
    story = syn.pages(pages, 1800)
    for entry in story:
        entry['label'] = text('Main and project experience', 60)
    snap = syn.snapshot(room_facts=room, live=live, story=story)
    return replace(snap, legacy_blocks={**snap.legacy_blocks, 'folded': 23, 'total': 23,
                                       'open_records': 0, 'open_rows': 0, 'open_chars': 0})


def core(root, snap=None):
    snap = snap or snapshot()
    stable = (HostSection('identity', text('Identity', PROTECTED['identity'])),)
    head = (HostSection('shared_understanding', text('Overview', PROTECTED['shared_understanding'])),
            HostSection('knowledge_index', text('Knowledge full', 40000), text('Knowledge outline', HOST['knowledge_index'])))
    dynamic = tuple(HostSection(name, text(name, size)) for name, size in PROTECTED.items()
                    if name in {'scratchpad', 'memory_marks'}) + tuple(
        HostSection(name, text(name + ' full', size * 2), text(name + ' narrow', size))
        if name in {'runtime', 'subagents', 'installed_skills'} else HostSection(name, text(name, size))
        for name, size in HOST.items() if name != 'knowledge_index')
    return cf.ContextCore(base_prompt=text('SYSTEM', PROTECTED['system']), bible_md=text('BIBLE', PROTECTED['bible']),
        architecture_md='# Architecture\n\n' + 'book ' * 80000, development_md='', docs_need_development=False,
        semi_stable_text='\n\n'.join(stable), dynamic_head_text='\n\n'.join(head), dynamic_text='\n\n'.join(dynamic),
        semi_stable_sections=stable, dynamic_head_sections=head, dynamic_sections=dynamic,
        memory_view_json=mv.snapshot_json(snap), user_content_json=json.dumps('Continue in this room.'),
        section_root=str(root), section_task_id='scale-fit')


def tools():
    return [{'type': 'function', 'function': {'name': name, 'description': text(name, 1460),
             'parameters': {'type': 'object', 'properties': {}}}}
            for name in ('enable_tools', 'list_available_tools', 'compact_context', 'task', 'read_file')]


def build(root, monkeypatch, *, window=92000, reserve=10000, snap=None, mode='max'):
    captured = core(root, snap)
    monkeypatch.setattr(cf, '_route_calibration_ratio', lambda *a, **kw: 1.0)
    evidence = SimpleNamespace(window_tokens=window, status='confirmed', stale=False, route_fp='', source='fixture',
        response_limit={'max_output_tokens': 16384}, route_facts={'working_reserve_tokens': reserve, 'basis': 'provider'})
    return cf.build_context_fit_plan(SimpleNamespace(drive_root=root), captured,
        {'id': 'scale-fit', 'chat_id': 1, '_is_direct_chat': True}, preferred_mode=mode, tool_schemas=tools(),
        route_resolver=lambda *a, **kw: ({'provider': 'fixture', 'model': 'fixture'}, evidence))
