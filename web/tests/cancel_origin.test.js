import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { cardMetaKeys } from '../modules/chat_activity.js';
import { summarizeChatLiveEvent, taskReasonDetail, taskTerminalSummary } from '../modules/log_events.js';

test('recorded cancellation: Python/browser parity including absent cause and punctuation', () => {
    const cases = JSON.parse(readFileSync(new URL('./fixtures/cancel_cause_parity.json', import.meta.url)));
    for (const { record, text } of cases) {
        assert.equal(taskReasonDetail(record), text, JSON.stringify(record));
        assert.equal(taskTerminalSummary(record).body, text);
    }
    assert.equal(taskReasonDetail({ status: 'cancelled', cancel_origin: { reason: '🙂'.repeat(200) } }),
        '🙂'.repeat(159) + '…');
});

test('child live and replay use genuine lineage and keep saved work inspectable', () => {
    const origin = { source: 'cascade_descendant', requested_by: 'root' };
    const carried = cardMetaKeys({ cancel_origin: origin });
    assert.deepEqual(carried.cancel_origin, origin);
    for (const frame of [
        { subagent_event: 'cancelled' },
        { subagent_event: 'completed', outcome_axes: { lifecycle: { status: 'cancelled' } } },
    ]) {
        const view = summarizeChatLiveEvent({
            type: 'send_message', is_progress: true, delegation_role: 'subagent',
            parent_task_id: 'root', subagent_task_id: 'child', result: 'Saved partial work',
            ...carried, ...frame,
        });
        assert.equal(view.phase, 'cancelled');
        assert.equal(view.body, 'Stopped with the task tree it belongs to · Stopped with its parent task.');
        assert.equal(view.activityPreview, view.body);
        assert.match(view.fullBody, /Saved partial work/);
        assert.doesNotMatch(view.body, /initiator:|root/);
    }
});

test('retained origin does not replace a non-cancelled child frame', () => {
    for (const subagent_event of ['running', 'failed']) {
        const view = summarizeChatLiveEvent({
            type: 'send_message', is_progress: true, delegation_role: 'subagent',
            parent_task_id: 'root', subagent_task_id: 'child', subagent_event,
            result: 'Saved partial work', error: subagent_event === 'failed' ? 'Worker failed' : '',
            status: 'cancelled', cancel_origin: { source: 'http_single' },
        });
        assert.doesNotMatch(view.body, /Stopped from/);
        assert.equal(view.phase, subagent_event === 'failed' ? 'error' : 'working');
    }
});
