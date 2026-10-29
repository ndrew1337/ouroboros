import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeConsciousness } from '../modules/consciousness_api.js';
import { deadlineWords, splitWakes } from '../modules/consciousness_words.js';
import { applyPayload, setMissTransport } from '../modules/i18n.js';
import { initActivity } from '../modules/activity.js';
import { setup } from './dashboard_dom_fixture.js';

const now = Date.parse('2027-01-15T12:00:00Z');
const facts = { enabled: true, next_wake_at: '2027-01-15T11:50:00Z', appointment_at: '2027-01-15T11:30:00Z' };

for (const [zone, past, future] of [['UTC', '11:30', '13:00'], ['Europe/Moscow', '14:30', '16:00']]) {
    test(`deadline words use the appointment and the viewer's clock in ${zone}`, (t) => {
        const prior = process.env.TZ;
        process.env.TZ = zone;
        t.after(() => { if (prior === undefined) delete process.env.TZ; else process.env.TZ = prior; });
        const snapshot = normalizeConsciousness(facts);
        assert.equal(snapshot.appointmentAt, facts.appointment_at);
        assert.equal(snapshot.liveWakeHeld, false);
        assert.deepEqual(deadlineWords(snapshot, now), { chip: `since ${past}`, line: `Due since ${past}` });
        assert.deepEqual(deadlineWords({ ...snapshot, nextWakeAt: '2027-01-15T13:00:00Z' }, now),
            { chip: future, line: `Next wake at ${future}` }, 'a future retry is never since its old appointment');
        assert.deepEqual(deadlineWords({ ...snapshot, appointmentAt: '' }, now), { chip: 'due', line: 'Due' });
        for (const override of [{ liveWakeTaskId: 'own' }, { liveWakeHeld: true }, { enabled: false }, { enabled: null }, { nextWakeAt: '' }]) {
            assert.deepEqual(deadlineWords({ ...snapshot, ...override }, now), { chip: '', line: '' });
        }
        const localMidnight = Date.parse(zone === 'UTC' ? '2027-01-15T00:01:00Z' : '2027-01-14T21:01:00Z');
        const appointmentAt = new Date(localMidnight - 120000).toISOString();
        const previousDay = new Date(appointmentAt).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
        assert.deepEqual(deadlineWords({ ...snapshot, appointmentAt, nextWakeAt: appointmentAt }, localMidnight),
            { chip: `since ${previousDay} 23:59`, line: `Due since ${previousDay} 23:59` });
        const tomorrow = new Date(localMidnight + 86400000).toISOString();
        assert.match(deadlineWords({ ...snapshot, nextWakeAt: tomorrow }, localMidnight).chip, /2027.*00:01/);
    });
}

test('split wakes preserves each source order, with absent legacy cause treated as own', () => {
    const wakes = [{ taskId: 'place1', cause: 'focus' }, { taskId: 'own1', cause: 'own' },
        { taskId: 'legacy' }, { taskId: 'place2', cause: 'focus' }];
    assert.deepEqual(splitWakes(wakes), { own: [wakes[1], wakes[2]], places: [wakes[0], wakes[3]] });
    assert.deepEqual(splitWakes(null), { own: [], places: [] });
    assert.deepEqual(splitWakes([wakes[1]]), { own: [wakes[1]], places: [] });
    assert.deepEqual(splitWakes([wakes[0]]), { own: [], places: [wakes[0]] });
});

test('deadline host words are registered with i18n', (t) => {
    setMissTransport(() => Promise.resolve(null));
    applyPayload({ language: 'ru', english: false, entries: {
        'since {time}': { text: 'с {time}' }, 'Due since {time}': { text: 'Пора с {time}' },
        'Next wake at {time}': { text: 'Следующее в {time}' },
        'code:consciousness.due': { text: 'пора' }, 'code:consciousness.due_line': { text: 'Пора' },
    } });
    t.after(() => applyPayload({ language: '', english: true, entries: {} }));
    assert.match(deadlineWords(normalizeConsciousness(facts), now).line, /^Пора с /);
    assert.match(deadlineWords(normalizeConsciousness(facts), now).chip, /^с /);
    assert.match(deadlineWords(normalizeConsciousness({ ...facts, next_wake_at: '2027-01-15T13:00:00Z' }), now).line, /^Следующее в /);
    assert.deepEqual(deadlineWords(normalizeConsciousness({ ...facts, appointment_at: '' }), now), { chip: 'пора', line: 'Пора' });
});

test('Activity refresh replaces a past next-wake time with due, and suppresses time for live or held wakes', async (t) => {
    const prior = process.env.TZ;
    process.env.TZ = 'UTC';
    t.after(() => { if (prior === undefined) delete process.env.TZ; else process.env.TZ = prior; });
    const cleanups = [];
    const { mount, routes, ws } = setup({ after: (cleanup) => cleanups.push(cleanup) });
    t.mock.method(Date, 'now', () => now);
    const response = (data) => ({ ok: true, status: 200, json: async () => data });
    routes.set('/api/tasks?queue_only=1', response({ queue: { running: [], pending: [] } }));
    routes.set('/api/schedules', response({ tasks: [] }));
    const activity = initActivity({ mount, ws });
    t.after(() => { activity.destroy(); cleanups.forEach((cleanup) => cleanup()); });
    for (const override of [{}, { live_wake_task_id: 'own' }, { live_wake_held: true },
        { appointment_at: '' }, { next_wake_at: '2027-01-15T13:00:00Z' }]) {
        const bg = { ...facts, tasks_running: 1, max_tasks: 2, ...override };
        routes.set('/api/state', response({ bg_consciousness_enabled: true, bg_consciousness_state: bg }));
        await activity.refresh();
        const text = mount.querySelector('[data-activity-section="background"]').textContent;
        if (override.live_wake_task_id || override.live_wake_held) assert.doesNotMatch(text, /Next wake|Due|since/);
        else if (override.appointment_at === '') {
            assert.match(text, /Due/);
            assert.doesNotMatch(text, /since/);
        } else if (override.next_wake_at) assert.match(text, /Next wake at 13:00/);
        else assert.match(text, /Due since 11:30/);
        assert.doesNotMatch(text, /11:50/);
        assert.match(text, /own tasks 1\/2/);
        assert.doesNotMatch(text, /next wake /);
    }
});
