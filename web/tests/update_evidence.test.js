import assert from 'node:assert/strict';
import test from 'node:test';
import { afterUpdateFactsText, bindPreflightDescription, preflightEvidenceText } from '../modules/update_status.js';
import { updateLetterView } from '../modules/updates.js';

const plan = { base_sha: 'aaaa', target_sha: 'bbbb' };
const letter = { key: plan, text: 'Exact range', description_current: true, state: 'ready', relation: 'pending' };

test('a new preflight removes the older description and preserves the exact one', () => {
    const status = { available: true, managed: true, check_ok: true, letter };
    const matching = bindPreflightDescription(status, { merge_plan: plan, letter });
    assert.equal(updateLetterView(matching).markdown, 'Exact range');
    const moved = bindPreflightDescription(status, { merge_plan: { ...plan, target_sha: 'cccc' }, letter });
    assert.equal(moved.letter, null);
    assert.equal(updateLetterView(moved).markdown, '');
    assert.match(preflightEvidenceText({ merge_plan: moved.preflight_range, letter }), /No description.*exact preflight range/);
    assert.doesNotMatch(preflightEvidenceText({ merge_plan: moved.preflight_range, letter }), /Exact range/);
});

test('confirmation names failed, absent and green evidence without converting declarations to proof', () => {
    const receipt = { checks: [{ name: 'full_battery', status: 'failed', source: 'author_declared', detail: 'two FAILs accepted' }],
        review: { observed: 'FAIL', declared: 'AUTHOR ACCEPTED' } };
    const qualification = { ...plan, commits: [{ sha: 'bbbb', subject: '#123', receipt }, { sha: 'missing', subject: '#122' }],
        github: { status: 'observed', runs: [{ sha: 'bbbb', conclusion: 'failure', url: 'https://github.com/run/1' }] } };
    const text = preflightEvidenceText({ merge_plan: plan, letter, qualification });
    for (const part of ['#123', 'failed (author_declared)', 'two FAILs accepted', '#122: no qualification receipt', 'post-merge: failure']) {
        assert.ok(text.includes(part), part);
    }
    qualification.commits = [{ sha: 'bbbb', subject: 'green', receipt: { checks: [{ name: 'full_battery', status: 'passed', source: 'author_declared' }], review: { observed: 'PASS', declared: 'PASS' } } }];
    qualification.github.runs[0].conclusion = 'success';
    const green = preflightEvidenceText({ merge_plan: plan, letter, qualification });
    assert.match(green, /full_battery passed/);
    assert.doesNotMatch(green, /failed|failure|no qualification receipt|FAIL/);
    assert.doesNotMatch(green, /also covers/);
    qualification.github.runs[0].covered_shas = ['earlier-merge'];
    assert.match(preflightEvidenceText({ merge_plan: plan, letter, qualification }), /This run also covers: earlier-/);
});

test('generation facts show scope, measured values and missing observations without a verdict', () => {
    assert.equal(afterUpdateFactsText(null), '');
    const row = { source_sha: 'aaaa', rss_bytes: 1048576, cpu_seconds: 2, cpu_percent: null, cpu_interval_seconds: null,
        duty_overruns: {}, direct_message_first_dispatch: null };
    const text = afterUpdateFactsText({ before: row, after: { ...row, direct_message_first_dispatch: { task_id: 'direct', seconds: 33 } } });
    assert.match(text, /RSS 1.00 MiB/);
    assert.match(text, /server process/);
    assert.match(text, /first dispatch unobserved/);
    assert.match(text, /first dispatch 33.00 seconds \(direct\)/);
    assert.doesNotMatch(text, /bad update|healthy|regression|warning/i);
});
