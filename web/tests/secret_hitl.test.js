// The integration-secret form: button-opened, OK / Close, explicit card cancellation.
// Values live in the open dialog's controls and nowhere else; every close empties them.
import assert from 'node:assert/strict';
import test from 'node:test';
import { NodeStub, fixture, turn } from './chat_decision_fixture.js';
import { createSecretRequestForms, secretRequestOf } from '../modules/secret_hitl.js';

const CANARY = 'zq7-CANARY-Ωlj3~p0+kx/2';
const REQUEST = { plugin_name: 'telegram', content_hash: 'h', fields: [{ id: 'botToken', setting_key: 'TELEGRAM_BOT_TOKEN' }] };
const QUIZ = { quizId: 'qz-s', taskId: 't-1', projectId: '', secretRequest: secretRequestOf({ secret_request: REQUEST }) };
const SECRET_MSG = { type: 'quiz', role: 'assistant', quiz_id: 'qz-s', task_id: 't-1', state: 'open', options: [],
    question: 'Secret required: telegram — botToken.', stake: '', assumption: '', wait_for_answer: true,
    ts: '2026-10-19T10:00:00Z', secret_request: REQUEST };

const reply = (status, body) => async () => ({ ok: status < 400, status, json: async () => body });
const saved = { ok: true, saved: true, state: 'answered', decision_id: 'quiz:t-1:qz-s', duplicate: false,
    secret_filled: { pluginName: 'telegram', fields: ['botToken'] },
    secret_outcome: { event: 'secret_filled', pluginName: 'telegram', fields: ['botToken'], settings_keys: ['TELEGRAM_BOT_TOKEN'] } };

function setup({ fetchImpl = reply(200, saved), readQuestion = async () => null, paintHead, canUse } = {}) {
    const prior = globalThis.document;
    globalThis.document = { createElement: (tag) => new NodeStub(tag) };
    const host = new NodeStub('body');
    const calls = [], accepted = [], toasts = [], focus = [];
    const stored = [];
    const priorStorage = globalThis.localStorage;
    globalThis.localStorage = { getItem: () => null, setItem: (...args) => stored.push(args), removeItem() {} };
    const forms = createSecretRequestForms({
        apiFetch: async (url, init) => { calls.push({ url, body: JSON.parse(init.body) }); return fetchImpl(url, init); },
        readQuestion, accept: (card, body) => { accepted.push(body); card.dataset.state = body.state; },
        showToast: (text) => toasts.push(text), host: () => host,
        bindFocus: (panel, options) => { focus.push(options); return () => focus.push('released'); },
        ...(paintHead ? { paintHead } : {}), ...(canUse ? { canUse } : {}),
    });
    const card = new NodeStub('div');
    card.dataset.state = 'open';
    return { forms, card, host, calls, accepted, toasts, focus, stored, restore() {
        globalThis.document = prior; globalThis.localStorage = priorStorage;
    } };
}
const input = (fx) => fx.host.querySelector('.secret-request-input');
const type = (field, value) => { field.value = value; field.listeners.get('input')?.({}); };
const everything = (node) => JSON.stringify(node, (key, value) => (key === 'parentNode' || key === 'listeners' ? undefined : value));

test('the popup names the plugin, masks every asked field and enables OK only when filled', async () => {
    const fx = setup();
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.host.children.length, 0, 'arrival never opens a modal');
        fx.card.querySelector('chat-secret-open').click();
        const dialog = fx.host.querySelector('secret-request-dialog');
        assert.ok(dialog, 'the owner opens the form from its card');
        assert.equal(dialog.getAttribute('role'), 'dialog');
        assert.equal(dialog.getAttribute('aria-modal'), 'true');
        assert.match(fx.host.querySelector('secret-request-title').textContent, /telegram: secret required/);
        const field = input(fx);
        assert.equal(field.type, 'password');
        assert.equal(field.autocomplete, 'off');
        assert.equal(fx.host.querySelector('secret-request-label').textContent, 'botToken');
        assert.match(fx.host.querySelector('ui-field-help').textContent, /TELEGRAM_BOT_TOKEN/);
        const ok = fx.host.querySelector('secret-request-ok');
        assert.equal(ok.textContent, 'OK');
        assert.equal(fx.host.querySelector('secret-request-cancel').textContent, 'Close');
        assert.equal(ok.disabled, true);
        type(field, CANARY);
        assert.equal(ok.disabled, false);
        assert.equal(fx.focus[0].initialFocus, field);
    } finally { fx.restore(); }
});

test('OK posts the values once to saveSecretToken, then empties the controls and settles the card', async () => {
    const fx = setup();
    try {
        fx.forms.mount(fx.card, QUIZ);
        fx.card.querySelector('chat-secret-open').click();
        const field = input(fx);
        type(field, CANARY);
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        assert.equal(fx.calls.length, 1);
        assert.equal(fx.calls[0].url, '/api/decisions/secret');
        assert.deepEqual(Object.keys(fx.calls[0].body).sort(), ['decision_id', 'request_id', 'values']);
        assert.equal(fx.calls[0].body.decision_id, 'quiz:t-1:qz-s');
        assert.equal(fx.calls[0].body.values.botToken, CANARY);
        assert.equal(field.value, '', 'the live control is emptied on success');
        assert.equal(fx.host.children.length, 0, 'the dialog is gone');
        assert.equal(fx.accepted[0].secret_filled.pluginName, 'telegram');
        fx.forms.sync(fx.card, 'answered', { secret_outcome: saved.secret_outcome });
        assert.match(fx.card.querySelector('chat-secret-status').textContent, /Saved to Settings: botToken/);
        assert.equal(fx.card.querySelector('chat-secret-actions'), null);
        assert.equal(fx.stored.length, 0, 'nothing reaches browser storage');
        assert.ok(!everything(fx.card).includes(CANARY) && !everything(fx.host).includes(CANARY));
    } finally { fx.restore(); }
});

test('a refused save keeps the live entries for an explicit same-id retry; an edit mints a new id', async () => {
    let status = 503;
    const fx = setup({ fetchImpl: async () => ({ ok: status < 400, status, json: async () => (status < 400 ? saved
        : { ok: false, saved: false, reason_code: 'settings_busy', error: 'busy' }) }) });
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.host.children.length, 0, 'arrival never opens a modal');
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        const error = fx.host.querySelector('secret-request-error');
        assert.match(error.textContent, /nothing was saved/);
        assert.equal(error.hidden, false);
        assert.equal(input(fx).value, CANARY, 'the owner retries without retyping');
        assert.equal(fx.host.querySelector('secret-request-check').hidden, true);
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        assert.equal(fx.calls[1].body.request_id, fx.calls[0].body.request_id, 'same values, same request');
        type(input(fx), `${CANARY}x`);
        status = 200;
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        assert.notEqual(fx.calls[2].body.request_id, fx.calls[0].body.request_id, 'a changed entry is a new request');
        assert.equal(fx.toasts.length, 0);
    } finally { fx.restore(); }
});

test('an unconfirmed save blocks OK until Check status; saving again is a new request', async () => {
    let reads = 0, failing = true;
    const fx = setup({ fetchImpl: async () => { if (failing) throw new TypeError('network down'); return reply(200, saved)(); },
        readQuestion: async () => { reads += 1;
            return { task_id: 't-1', quiz_id: 'qz-s', state: 'open', secret_save: { state: 'writing', request_id: 'x' } }; } });
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.host.children.length, 0, 'arrival never opens a modal');
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        const ok = fx.host.querySelector('secret-request-ok');
        ok.click();
        await turn();
        assert.match(fx.host.querySelector('secret-request-error').textContent, /not confirmed/);
        const check = fx.host.querySelector('secret-request-check');
        assert.equal(check.hidden, false);
        assert.equal(ok.disabled, true, 'status first: OK cannot resend an attempt of unknown outcome');
        ok.click();
        type(input(fx), `${CANARY}!`);
        assert.equal(ok.disabled, true, 'an edit does not unblock it either');
        await turn();
        assert.equal(fx.calls.length, 1, 'no automatic or blocked second POST');
        check.click();
        await turn();
        assert.equal(reads, 1);
        assert.equal(ok.disabled, false);
        assert.equal(ok.textContent, 'Save again');
        assert.match(fx.host.querySelector('secret-request-error').textContent, /replaces whatever it may have stored/);
        failing = false;
        ok.click();
        await turn();
        assert.equal(fx.calls.length, 2);
        assert.notEqual(fx.calls[1].body.request_id, fx.calls[0].body.request_id, 'save-again is a new request');
        assert.equal(fx.accepted.at(-1).state, 'answered');
    } finally { fx.restore(); }
});

test('saved-but-unrecorded keeps the truth on screen and offers status, not a repost', async () => {
    const fx = setup({ fetchImpl: reply(500, { ok: false, saved: true, reason_code: 'secret_completion_unrecorded', error: 'x' }) });
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.host.children.length, 0, 'arrival never opens a modal');
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        assert.match(fx.host.querySelector('secret-request-error').textContent, /saved to Settings, but the request could not be marked complete/);
        assert.equal(fx.host.querySelector('secret-request-check').hidden, false);
        assert.equal(fx.host.querySelector('secret-request-ok').disabled, true);
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        assert.equal(fx.calls.length, 1);
    } finally { fx.restore(); }
});

test('a reply is shown only through fixed copy, never its own text', async () => {
    const fx = setup({ fetchImpl: reply(400, { ok: false, saved: false, reason_code: 'something_new',
        error: `echo ${CANARY}` }) });
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.host.children.length, 0, 'arrival never opens a modal');
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        const text = fx.host.querySelector('secret-request-error').textContent;
        assert.equal(text, 'The save was refused; nothing was saved.');
        assert.ok(!text.includes(CANARY));
    } finally { fx.restore(); }
});

test('Cancel sends no values and closes; Close only hides the form and empties it', async () => {
    const fx = setup({ fetchImpl: reply(200, { ok: true, saved: false, state: 'closed',
        secret_cancelled: { pluginName: 'telegram', fields: ['botToken'] } }) });
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.host.children.length, 0, 'arrival never opens a modal');
        fx.card.querySelector('chat-secret-open').click();
        const field = input(fx);
        type(field, CANARY);
        fx.host.querySelector('secret-request-close').click();
        assert.equal(field.value, '');
        assert.equal(fx.host.children.length, 0);
        assert.equal(fx.calls.length, 0, 'closing the form is not a cancel');
        assert.equal(fx.card.querySelector('chat-secret-open').disabled, false);
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.host.querySelector('secret-request-cancel').click();
        assert.equal(fx.host.children.length, 0, 'footer Close only dismisses');
        assert.equal(fx.calls.length, 0);
        fx.card.querySelector('chat-secret-open').click();
        assert.equal(input(fx).value, '');
        fx.card.querySelector('chat-secret-cancel').click();
        await turn();
        assert.equal(fx.calls[0].body.action, 'cancel');
        assert.ok(!('values' in fx.calls[0].body));
        assert.equal(fx.accepted[0].state, 'closed');
        assert.equal(fx.host.children.length, 0);
    } finally { fx.restore(); }
});

test('a settlement from another tab closes an open form and empties it', () => {
    const fx = setup();
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.host.children.length, 0, 'arrival never opens a modal');
        fx.card.querySelector('chat-secret-open').click();
        const field = input(fx);
        type(field, CANARY);
        fx.forms.sync(fx.card, 'answered', { secret_outcome: saved.secret_outcome });
        assert.equal(field.value, '');
        assert.equal(fx.host.children.length, 0);
    } finally { fx.restore(); }
});

test('the question card renders the protected form instead of a free answer or draft storage', async () => {
    const fx = fixture();
    try {
        const replayed = fx.decision.buildQuizCard({ msg_type: 'quiz', task_id: 't-1', text: SECRET_MSG.question,
            quiz: { ...SECRET_MSG, type: undefined } });
        assert.ok(replayed.querySelector('chat-secret-request'));
        assert.equal(replayed.querySelector('chat-quiz-comment'), null, 'no free-answer field to paste a token into');
        assert.equal(replayed.querySelector('chat-quiz-close'), null);
        assert.equal(replayed.dataset.secretRequest, '1');
        assert.equal(globalThis.localStorage.getItem('ouro_quiz_reply_v1:["t-1","qz-s"]'), null);
        fx.decision.applyQuizStateFrame({}, { type: 'quiz_state', task_id: 't-1', quiz_id: 'qz-s', state: 'answered',
            secret_outcome: saved.secret_outcome });
        await turn();
        assert.match(replayed.querySelector('chat-secret-status').textContent, /Saved to Settings: botToken/);
    } finally { fx.restore(); }
});

for (const timing of ['before close', 'after close']) test(`an unknown save ${timing} stays blocked when reopened`, async () => {
    let reject;
    const fx = setup({ fetchImpl: () => new Promise((_resolve, no) => { reject = no; }) });
    try {
        fx.forms.mount(fx.card, QUIZ);
        fx.card.querySelector('chat-secret-open').click();
        const field = input(fx);
        type(field, CANARY);
        fx.host.querySelector('secret-request-ok').click();
        if (timing === 'before close') { reject(new TypeError('network')); await turn(); }
        fx.host.querySelector('secret-request-close').click();
        assert.equal(field.value, '');
        assert.equal(fx.accepted.length, 0, 'dismissal sends no cancellation');
        if (timing === 'after close') { reject(new TypeError('network')); await turn(); }
        fx.card.querySelector('chat-secret-open').click();
        assert.equal(input(fx).value, '');
        type(input(fx), CANARY);
        assert.equal(fx.host.querySelector('secret-request-ok').disabled, true);
        assert.equal(fx.host.querySelector('secret-request-check').hidden, false);
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        assert.equal(fx.calls.length, 1, 'no repost without a status check');
    } finally { fx.restore(); }
});

test('a pending save survives dismissal and a reopened dialog remains read-only until success', async () => {
    let resolve;
    const fx = setup({ fetchImpl: () => new Promise(yes => { resolve = yes; }) });
    try {
        fx.forms.mount(fx.card, QUIZ);
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.host.querySelector('secret-request-ok').click();
        fx.host.querySelector('secret-request-close').click();
        fx.card.querySelector('chat-secret-open').click();
        assert.equal(input(fx).readOnly, true);
        assert.equal(fx.host.querySelector('secret-request-ok').disabled, true);
        assert.equal(fx.host.querySelector('secret-request-ok').textContent, 'Saving…');
        resolve(await reply(200, saved)());
        await turn();
        assert.equal(fx.calls.length, 1);
        assert.equal(fx.host.children.length, 0);
        assert.equal(fx.accepted[0].state, 'answered');
    } finally { fx.restore(); }
});

test('a recovered writing receipt requires Check status before saving', () => {
    const fx = setup();
    try {
        fx.forms.mount(fx.card, QUIZ);
        fx.forms.sync(fx.card, 'open', { secret_save: { state: 'writing' } });
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        assert.equal(fx.host.querySelector('secret-request-ok').disabled, true);
        assert.equal(fx.host.querySelector('secret-request-check').hidden, false);
        fx.host.querySelector('secret-request-check').hidden = true;
        fx.forms.sync(fx.card, 'open', { secret_save: { state: 'writing' } });
        assert.equal(fx.host.querySelector('secret-request-check').hidden, false, 'live writing also explains disabled OK');
    } finally { fx.restore(); }
});

test('secretRequestOf ignores malformed metadata', () => {
    assert.equal(secretRequestOf({}), null);
    assert.equal(secretRequestOf({ secret_request: { plugin_name: 'x', fields: [] } }), null);
    assert.deepEqual(secretRequestOf({ secret_request: REQUEST }),
        { pluginName: 'telegram', fields: [{ id: 'botToken', settingKey: 'TELEGRAM_BOT_TOKEN' }] });
});

test('a Project secret mirror updates an unconfirmed save without changing question state', () => {
    const column = new NodeStub();
    const fx = fixture({ isMain: true, insertMessageNode: (node) => { column.append(node); return true; } });
    try {
        const pointer = { ...SECRET_MSG, role: 'system', system_type: 'project_question_pointer',
            project_id: 'p1', project_chat_id: 23, project_name: 'Setup', quiz_state: 'open' };
        fx.decision.appendQuestionPointer(pointer);
        const card = column.querySelector('.chat-quiz-card');
        assert.ok(card.querySelector('.chat-secret-open'));
        assert.equal(card.querySelector('.chat-quiz-comment'), null);
        assert.equal(card.querySelector('.chat-secret-status').textContent, '');
        fx.decision.appendQuestionPointer({ ...pointer, secret_save: { state: 'writing', request_id: 'lost' } });
        assert.match(card.querySelector('.chat-secret-status').textContent, /result is unknown/);
    } finally { fx.restore(); }
});

test('the head mark leaves amber while a save is in flight or unconfirmed, as for an ordinary answer', async () => {
    let release;
    const painted = [];
    const fx = setup({ fetchImpl: () => new Promise((_resolve, reject) => { release = () => reject(new TypeError('network down')); }),
        readQuestion: async () => ({ task_id: 't-1', quiz_id: 'qz-s', state: 'open' }),
        paintHead: (card) => { painted.push(card.dataset.submission); return true; } });
    try {
        fx.forms.mount(fx.card, QUIZ);
        assert.equal(fx.card.dataset.submission || '', '', 'an open request is the owner\'s move');
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.host.querySelector('secret-request-ok').click();
        assert.equal(fx.card.dataset.submission, 'sending');
        release();
        await turn();
        assert.equal(fx.card.dataset.submission, 'unknown', 'an unconfirmed save is not amber either');
        fx.host.querySelector('secret-request-check').click();
        await turn();
        assert.equal(fx.card.dataset.submission, '', 'status says nothing was saved: the owner moves again');
        fx.forms.sync(fx.card, 'answered', { secret_outcome: saved.secret_outcome });
        assert.equal(fx.card.dataset.submission, '');
        assert.ok(painted.includes('sending') && painted.includes('unknown'), 'each phase repaints the head');
        assert.equal(painted.at(-1), '');
    } finally { fx.restore(); }
});

test('a view released while its save is in flight ignores the late reply: no accept, focus or dialog', async () => {
    let answer;
    const fx = setup({ fetchImpl: () => new Promise((resolve) => { answer = () => resolve({ ok: true, status: 200, json: async () => saved }); }) });
    try {
        fx.forms.mount(fx.card, QUIZ);
        let focused = 0;
        fx.card.append(Object.assign(new NodeStub('div'), { className: 'chat-quiz-question', focus: () => { focused += 1; } }));
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.host.querySelector('secret-request-ok').click();
        fx.forms.release(fx.card);  // remount or navigation while the request is in flight
        assert.equal(fx.host.children.length, 0, 'release empties and removes the dialog');
        answer();
        await turn();
        assert.equal(fx.accepted.length, 0, 'the stale card is not settled from a late reply');
        assert.equal(focused, 0, 'focus is not moved by a released view');
        assert.equal(fx.host.children.length, 0);
        assert.equal(fx.toasts.length, 0);
    } finally { fx.restore(); }
});


test('stale card authority blocks open, save and cancel; a fresh card keeps the positive path', async () => {
    const fx = setup({ canUse: card => card.dataset.authorityStale !== '1' });
    try {
        fx.forms.mount(fx.card, QUIZ);
        fx.card.dataset.authorityStale = '1';
        fx.forms.sync(fx.card, 'open');
        assert.equal(fx.card.querySelector('chat-secret-open').disabled, true);
        assert.equal(fx.card.querySelector('chat-secret-cancel').disabled, true);
        fx.card.querySelector('chat-secret-open').click();
        assert.equal(fx.host.children.length, 0);
        fx.card.dataset.authorityStale = '';
        fx.forms.sync(fx.card, 'open');
        fx.card.querySelector('chat-secret-open').click();
        type(input(fx), CANARY);
        fx.card.dataset.authorityStale = '1';
        fx.host.querySelector('secret-request-ok').click();
        fx.card.querySelector('chat-secret-cancel').click();
        await turn();
        assert.equal(fx.calls.length, 0);
        fx.card.dataset.authorityStale = '';
        fx.host.querySelector('secret-request-ok').click();
        await turn();
        assert.equal(fx.calls.length, 1);
        assert.equal(fx.accepted.at(-1).state, 'answered');
    } finally { fx.restore(); }
});
