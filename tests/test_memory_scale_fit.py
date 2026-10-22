"""A large room must fit through the ordinary ladder, with measured margins."""
from ouroboros import memory_floor as mf, memory_view as mv
from tests import _memory_scale_fixture as scale


def test_owner_shape_admits_92000_with_route_working_reserve(tmp_path, monkeypatch):
    plan = scale.build(tmp_path, monkeypatch)
    shown = plan.projection(plan.initial_mode)
    margin = 92000 - 10000 - shown.calibrated_tokens
    assert margin >= 0, f'input={shown.calibrated_tokens}, reserve=10000, window=92000, margin={margin}'
    assert plan.preferred_mode == 'max' and plan.initial_mode == 'nano'
    assert 'F7a' in shown.memory_facts['floor']['steps']


def test_same_room_admits_128000_text_route(tmp_path, monkeypatch):
    plan = scale.build(tmp_path, monkeypatch, window=128000, reserve=None)
    shown = plan.projection(plan.initial_mode)
    assert shown.calibrated_tokens + plan.output_reserve_tokens <= 128000


def test_floor_size_depends_on_protected_items_not_history_count():
    sizes = []
    for rows, pages in ((150, 100), (1500, 1000)):
        snap = scale.snapshot(rows, pages)
        level = mf.fit_memory_view(snap, {'margin': 0, 'physical': 0, 'budget': None})
        rendered = mv.render_story(snap, level) + mv.render_room(snap, level)
        sizes.append(len(rendered.encode()))
        assert all(note['text'] in rendered for note in snap.room['notes'])
        assert all(row['line'] in rendered for row in snap.room['lane1'] if row.get('immune'))
    assert abs(sizes[1] / sizes[0] - 1) <= .05, f'floor bytes at 150/100 and 1500/1000: {sizes}'
