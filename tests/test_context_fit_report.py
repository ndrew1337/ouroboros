"""Read-only diagnostic over real synthetic stores, with no provider or server."""
import hashlib
import json
import os
import sqlite3

import pytest

from devtools.context_fit_report import report, measure_core
from devtools._context_fit_readonly import readonly_inputs, server_alive, snapshot_root
from tests import _memory_scale_fixture as scale


def fingerprint(root):
    return {str(path.relative_to(root)): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob('*') if path.is_file()}


def test_measurement_uses_ordinary_scale_fitter_and_composition(tmp_path, monkeypatch):
    from ouroboros import memory_floor as floor
    with monkeypatch.context() as old:
        old.setattr(floor, 'LADDER', tuple(step for step in floor.LADDER if step not in {'F7a', 'F5a', 'F8', 'F8a'}))
        before = measure_core(scale.core(tmp_path), scale.tools(), root=tmp_path, window=92000, reserve=10000)
    assert before['verdict'] == 'MinimumViewUnfit' and before['deficit_tokens'] > 0
    result = measure_core(scale.core(tmp_path), scale.tools(), root=tmp_path, window=92000, reserve=10000)
    assert result['verdict'] == 'fits', result
    assert sum(row['tokens'] for row in result['blocks']) == result['input_tokens']
    assert result['ladder'].get('F7a') and result['ladder'].get('F5a')
    assert result['input_tokens'] + 10000 <= 92000
    print('SCALE_COMPARISON ' + json.dumps({'before': before, 'after': result}, sort_keys=True))


def test_real_scale_root_read_only_report(tmp_path, monkeypatch, capsys):
    import socket
    from ouroboros import agent_startup_checks
    from devtools.context_fit_report import main

    def forbidden(*args, **kwargs):
        pytest.fail('offline measurement attempted a network connection or live process scan')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(agent_startup_checks, 'check_stray_server_processes', forbidden)
    root = scale.write_root(tmp_path / 'data')
    before = fingerprint(root)
    connected = []
    original = sqlite3.connect
    def connect(database, *args, **kwargs):
        if str(root) in str(database):
            connected.append(str(database))
            assert 'mode=ro&immutable=1' in str(database)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    result = report(root, 1, 92000, 10000)
    assert connected and fingerprint(root) == before
    assert sum(row['count'] for row in result['blocks'] if row['block'] == 'live rooms') == 228
    assert next(row for row in result['blocks'] if row['block'] == 'this room notes')['count'] == 6
    assert next(row for row in result['blocks'] if row['block'] == 'story')['count'] == 113
    assert result['verdict'] == 'fits' and result['margin_tokens'] >= 0, result
    with readonly_inputs(root), pytest.raises(PermissionError):
        (root / 'forbidden').write_text('not allowed')
    with readonly_inputs(root), pytest.raises(PermissionError):
        sqlite3.connect(root / 'forbidden.sqlite')
    (root / 'state/server_process.json').write_text(json.dumps({'pid': os.getpid()}))
    before_snapshot = fingerprint(root)
    assert main(['--data', str(root), '--room', '1', '--window', '92000', '--reserve', '10000',
                 '--snapshot', '--json']) == 0
    copied = json.loads(capsys.readouterr().out)
    assert copied['snapshot'] and copied['verdict'] == 'fits'
    assert fingerprint(root) == before_snapshot
    print('SCALE_ROOT_REPORT ' + json.dumps(result, sort_keys=True))
    print('SCALE_ROOT_SNAPSHOT_REPORT ' + json.dumps(copied, sort_keys=True))


def test_live_pid_and_kernel_lock_refuse_without_snapshot(tmp_path):
    import fcntl
    (tmp_path / 'state').mkdir()
    pid = tmp_path / 'state/server_process.json'
    pid.write_text(json.dumps({'pid': os.getpid()}))
    assert server_alive(tmp_path)
    with pytest.raises(ValueError, match='live server'):
        report(tmp_path, 1, 92000, 10000)
    pid.unlink()
    with (tmp_path / 'state/server.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert server_alive(tmp_path)
    assert not server_alive(tmp_path)


def test_snapshot_includes_committed_wal_without_changing_original(tmp_path):
    root, copy = tmp_path / 'original', tmp_path / 'copy'
    (root / 'state').mkdir(parents=True)
    copy.mkdir()
    db = sqlite3.connect(root / 'state/fixture.sqlite')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE sample (value)')
        db.execute('INSERT INTO sample VALUES (123)')
        db.commit()
        before = fingerprint(root)
        snapshot_root(root, copy)
        assert fingerprint(root) == before
        with sqlite3.connect((copy / 'state/fixture.sqlite').as_uri() + '?mode=ro&immutable=1', uri=True) as frozen:
            assert frozen.execute('SELECT value FROM sample').fetchone() == (123,)
    finally:
        db.close()
