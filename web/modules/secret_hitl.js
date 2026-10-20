// Integration-secret request form (DESIGN "Quiz card" → Secret request). The agent's
// showSecretHitl(pluginName, fields) arrives as an ordinary question card carrying typed
// `secret_request` metadata; this module turns that card's answer area into one protected
// form opened only by its chat button: masked fields, OK / Close, saved through `saveSecretToken`
// (POST /api/decisions/secret) and nowhere else.
//
// A value lives only in the open dialog's password controls. It is never written to a
// dataset, attribute, local/session storage, a toast or a log; every close — OK confirmed,
// Close, Escape, settlement from another tab, release — empties the controls. A pending
// request reopens after reload with empty fields. A refused save keeps the live entries for
// an explicit retry under the same request id (a changed entry mints a new one). A save whose
// outcome is unknown — no reply, `saved: null`, or written but unverified/unrecorded — blocks
// OK until Check status (the exact-question read) answers; saving again over an unconfirmed
// attempt is then the owner's explicit choice under a NEW request id. Only fixed, code-driven
// copy is shown: a response's free text is never rendered.
import { apiClient } from './api_client.js';
import { bindDialogFocus } from './ui_interactions.js';
import { fmt, tr } from './i18n.js';

const REASON_TEXT = {
    secret_value_empty: ['secret.error_empty', 'Enter a value for every field.'],
    secret_value_placeholder: ['secret.error_placeholder', 'That looks like a masked placeholder, which Settings would not keep. Enter the real value.'],
    secret_value_invalid: ['secret.error_invalid', 'The value contains a line break or control character.'],
    secret_value_too_long: ['secret.error_too_long', 'The value is too long.'],
    secret_request_stale: ['secret.error_stale', 'The skill changed since this request; nothing was saved. Ask the agent to request it again.'],
    secret_request_inactive: ['secret.error_inactive', 'The task that asked has finished; nothing was saved.'],
    settings_busy: ['secret.error_busy', 'Settings are being saved elsewhere; nothing was saved. Try again.'],
    settings_locked: ['secret.error_busy', 'Settings are being saved elsewhere; nothing was saved. Try again.'],
    secret_save_failed: ['secret.error_failed', 'Settings could not be written; nothing was saved. Try again.'],
    secret_not_retained: ['secret.error_not_retained', 'Settings did not keep the value as entered; it was not reported saved. Try again.'],
    secret_completion_unrecorded: ['secret.error_unrecorded', 'The value was saved to Settings, but the request could not be marked complete. Check status before trying again.'],
    secret_save_unverified: ['secret.error_unverified', 'The value was written to Settings, but it could not be read back. Check status before trying again.'],
    secret_save_unconfirmed: ['secret.error_unconfirmed', 'This save was never confirmed; its result is unknown. Check status before trying again.'],
    secret_request_settled: ['secret.error_settled', 'This request was already completed or cancelled; nothing was saved.'],
    secret_fields_mismatch: ['secret.error_mismatch', 'The form no longer matches the request; nothing was saved. Reload and try again.'],
};
const REFUSED = ['secret.error_refused', 'The save was refused; nothing was saved.'];
const UNCONFIRMED = ['secret.unconfirmed', 'The save was not confirmed; its result is unknown. Check status before trying again.'];
const nonce = () => globalThis.crypto?.randomUUID?.() || `s-${Date.now()}-${Math.random().toString(36).slice(2)}`;

/** The card's typed request ({pluginName, fields:[{id, settingKey}]}) or null. */
export function secretRequestOf(src) {
    const raw = src && typeof src.secret_request === 'object' ? src.secret_request : null;
    const fields = Array.isArray(raw?.fields) ? raw.fields.filter((row) => row && typeof row.id === 'string' && row.id) : [];
    if (!raw || typeof raw.plugin_name !== 'string' || !raw.plugin_name || !fields.length) return null;
    return { pluginName: raw.plugin_name, fields: fields.map((row) => ({ id: row.id, settingKey: String(row.setting_key || '') })) };
}

const el = (tag, className = '', text = '') => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
};
const button = (className, text) => { const node = el('button', className, text); node.type = 'button'; return node; };
// Fixed copy per typed reason; an unknown code is a generic refusal, never the reply's own text.
const reasonText = (body) => tr(...(REASON_TEXT[body?.reason_code] || REFUSED));

/**
 * One controller per chat surface. `accept(card, body)` merges a canonical receipt into the
 * card (chat_decision's observation and setCardState); `readQuestion(quiz)` is the exact-question
 * read; `canUse(card)` is false while the surface's authority is stale; `paintHead(card)` repaints the
 * question head's mark from `card.dataset.submission`, as for an ordinary answer in flight.
 */
export function createSecretRequestForms({ apiFetch, readQuestion = async () => null, accept = () => {},
    showToast = () => {}, canUse = () => true, host = () => document.body, bindFocus = bindDialogFocus,
    paintHead = () => false } = {}) {
    const views = new Map();
    const transport = apiFetch ? { fetchImpl: apiFetch } : {};

    function mount(card, quiz) {
        const request = quiz.secretRequest;
        if (!request || views.has(card)) return;
        card.dataset.secretRequest = '1';
        const box = el('div', 'chat-secret-request');
        const list = el('ul', 'chat-secret-fields');
        for (const field of request.fields) {
            const row = el('li', 'chat-secret-field');
            row.append(el('span', 'chat-secret-field-id', field.id),
                el('span', 'chat-secret-field-key', fmt('Saved as {key} in Settings', { key: field.settingKey })));
            list.append(row);
        }
        const status = el('div', 'chat-secret-status');
        status.setAttribute('role', 'status');
        const actions = el('div', 'chat-secret-actions');
        const open = button('btn btn-primary btn-sm chat-secret-open', tr('secret.enter', 'Enter secret…'));
        const cancel = button('btn btn-default btn-sm chat-secret-cancel', tr('secret.cancel_request', 'Cancel request'));
        actions.append(open, cancel);
        box.append(list, status, actions);
        card.append(box);
        const view = { card, quiz, request, box, status, actions, open, cancel, dialog: null, requestId: '', pending: false,
            blocked: false };
        views.set(card, view);
        open.addEventListener('click', () => openDialog(view));
        cancel.addEventListener('click', () => cancelRequest(view));
    }

    // The card's settled or pending facts; returns whether the DOM changed.
    function sync(card, state, current = {}) {
        const view = views.get(card);
        if (!view) return false;
        const answerable = state === 'open' && canUse(view.card);
        const outcome = current.secret_outcome || {};
        let text = '';
        if (state === 'answered' && outcome.event === 'secret_filled')
            text = fmt('Saved to Settings: {fields}. Saving is not a connection; the task continues the setup.',
                { fields: (outcome.fields || []).join(', ') });
        else if (state === 'closed' && outcome.interrupted_save)
            text = tr('secret.cancel_interrupted', 'An earlier save was interrupted; whether its value reached Settings is unknown.');
        else if (state === 'expired_terminal')
            text = tr('secret.inactive', 'The task that asked has finished; this form no longer saves. Ask again, or use Settings.');
        else if (state === 'open' && current.secret_save?.state === 'writing' && !view.pending)
            text = tr('secret.unconfirmed_card', 'A save was not confirmed; its result is unknown. Enter the value again to save it, or cancel.');
        if (state === 'open' && current.secret_save?.state === 'writing' && !view.pending)
            setError(view, tr(...UNCONFIRMED), { unknown: true });
        let changed = view.status.textContent !== text;
        if (changed) view.status.textContent = text;
        const settled = !['open', 'unknown'].includes(state);
        if (settled && view.actions.isConnected) { view.actions.remove(); changed = true; }
        view.open.disabled = !answerable || view.pending;
        view.cancel.disabled = !answerable || view.pending;
        if (settled) closeDialog(view, { restoreFocus: false });
        else if (view.dialog) view.dialog.ok.disabled = view.pending || view.blocked || !answerable || !filled(view);
        return paintSubmission(view, settled) || changed;
    }

    // A save in flight or of unknown outcome is not the owner's move: the head leaves amber
    // exactly as for an ordinary answer (DESIGN "Status marks"); a settled card has none.
    function paintSubmission(view, settled = false) {
        const phase = settled ? '' : view.pending ? 'sending' : view.blocked ? 'unknown' : '';
        if ((view.card.dataset.submission || '') === phase) return false;
        view.card.dataset.submission = phase;
        return paintHead(view.card) !== false;
    }

    // A view released while its request was in flight (remount, navigation, disposal) is stale:
    // its late reply touches neither that card nor focus; the next view reads the canonical state.
    const released = (view) => views.get(view.card) !== view;

    function filled(view) {
        return Boolean(view.dialog) && view.dialog.inputs.every((input) => String(input.value || '').trim());
    }

    function openDialog(view) {
        if (view.dialog) { view.dialog.inputs[0]?.focus?.(); return; }
        if (view.card.dataset.state !== 'open' || !canUse(view.card)) return;
        const backdrop = el('div', 'marketplace-modal-backdrop secret-request-backdrop');
        // Every visible string here comes through tr/fmt; the overlay never walks the form.
        backdrop.setAttribute('data-i18n-authored', '');
        const panel = el('div', 'marketplace-modal secret-request-dialog');
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        const titleId = `secret-request-title-${view.quiz.quizId}`;
        panel.setAttribute('aria-labelledby', titleId);
        const head = el('div', 'marketplace-modal-head');
        const title = el('h3', 'secret-request-title', fmt('{plugin}: secret required', { plugin: view.request.pluginName }));
        title.id = titleId;
        const close = button('btn btn-default btn-sm secret-request-close', tr('secret.close', 'Close'));
        close.setAttribute('aria-label', tr('secret.close_form', 'Close the form; the request stays open'));
        head.append(title, close);
        const body = el('div', 'marketplace-modal-body secret-request-body');
        body.append(el('p', 'secret-request-intro', tr('secret.intro',
            'The value goes straight to Settings. It is not shown to Ouroboros, the chat or logs.')));
        const inputs = [];
        for (const field of view.request.fields) {
            const wrap = el('div', 'ui-field secret-request-field');
            const inputId = `secret-request-${view.quiz.quizId}-${field.id}`;
            const label = el('label', 'secret-request-label', field.id);
            label.setAttribute('for', inputId);
            const input = el('input', 'ui-control secret-input secret-request-input');
            input.type = 'password';
            input.value = '';
            input.id = inputId;
            input.autocomplete = 'off';
            input.setAttribute('autocapitalize', 'off');
            input.setAttribute('spellcheck', 'false');
            input.dataset.secretField = field.id;
            input.addEventListener('input', () => {
                view.requestId = '';  // a changed entry is a new request; a retry keeps the same one
                if (!view.blocked) setError(view, '');
                view.dialog.ok.disabled = view.pending || view.blocked || !filled(view);
            });
            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && !event.isComposing && !view.dialog.ok.disabled) {
                    event.preventDefault();
                    submit(view);
                }
            });
            wrap.append(label, input, el('div', 'ui-field-help', fmt('Saved as {key}', { key: field.settingKey })));
            body.append(wrap);
            inputs.push(input);
        }
        const error = el('div', 'secret-request-error');
        error.setAttribute('role', 'alert');
        error.hidden = true;
        const check = button('btn btn-default btn-sm secret-request-check', tr('secret.check_status', 'Check status'));
        check.hidden = true;
        body.append(error, check);
        const footer = el('div', 'marketplace-modal-actions');
        const cancel = button('btn btn-default secret-request-cancel', tr('secret.close', 'Close'));
        const ok = button('btn btn-primary secret-request-ok', tr('secret.ok', 'OK'));
        ok.disabled = true;
        footer.append(cancel, ok);
        panel.append(head, body, footer);
        backdrop.append(panel);
        view.dialog = { backdrop, panel, inputs, ok, error, check, cancel, release: null, okLabel: ok.textContent };
        close.addEventListener('click', () => closeDialog(view));
        cancel.addEventListener('click', () => closeDialog(view));
        ok.addEventListener('click', () => submit(view));
        check.addEventListener('click', () => checkStatus(view));
        backdrop.addEventListener('click', (event) => { if (event.target === backdrop && !view.pending) closeDialog(view); });
        host().append(backdrop);
        view.dialog.release = bindFocus(panel, { initialFocus: inputs[0], returnFocus: view.open,
            onEscape: () => { if (!view.pending) closeDialog(view); } });
        setPending(view, view.pending);
        if (view.blocked) setError(view, tr(...UNCONFIRMED), { unknown: true });
    }

    // `unknown` blocks OK until Check status: no attempt of unknown outcome is repeated by itself.
    function setError(view, text, { unknown = false } = {}) {
        view.blocked = unknown;
        paintSubmission(view);
        if (!view.dialog) { view.status.textContent = text; return; }
        view.dialog.error.textContent = text;
        view.dialog.error.hidden = !text;
        view.dialog.check.hidden = !unknown;
        view.dialog.ok.disabled = view.pending || unknown || !filled(view);
    }

    function setPending(view, pending) {
        view.pending = pending;
        paintSubmission(view);
        view.open.disabled = pending || !canUse(view.card);
        view.cancel.disabled = pending || !canUse(view.card);
        if (!view.dialog) return;
        view.dialog.ok.textContent = pending ? tr('secret.saving', 'Saving…') : view.dialog.okLabel;
        view.dialog.ok.disabled = pending || view.blocked || !filled(view);
        for (const input of view.dialog.inputs) input.readOnly = pending;
    }

    function closeDialog(view, { restoreFocus = true } = {}) {
        const dialog = view.dialog;
        if (!dialog) return;
        for (const input of dialog.inputs) input.value = '';
        view.dialog = null;
        view.requestId = '';
        // Hiding a dialog clears values, not an in-flight/unknown save's status.
        dialog.release?.({ restoreFocus });
        dialog.backdrop.remove();
    }

    async function submit(view) {
        if (view.pending || view.blocked || !view.dialog || !filled(view) || !canUse(view.card)) return;
        const values = Object.fromEntries(view.dialog.inputs.map((input) => [input.dataset.secretField, input.value]));
        if (!view.requestId) view.requestId = nonce();
        const decisionId = `quiz:${view.quiz.taskId}:${view.quiz.quizId}`;
        setError(view, '');
        setPending(view, true);
        let body = null;
        try {
            body = await apiClient.saveSecretToken({ request_id: view.requestId, decision_id: decisionId, values }, transport);
        } catch (error) {
            body = error?.body && typeof error.body === 'object' ? error.body : null;
            if (released(view)) return;
            setPending(view, false);
            if (!body || !error.status) { setError(view, tr(...UNCONFIRMED), { unknown: true }); return; }
            if (body.saved !== false) { setError(view, reasonText(body), { unknown: true }); return; }
            if (['answered', 'closed', 'expired_terminal'].includes(body.state)) {
                accept(view.card, body);
                showToast(reasonText(body), 'error');
                return;
            }
            setError(view, reasonText(body));
            return;
        } finally {
            Object.keys(values).forEach((key) => { values[key] = ''; });
        }
        if (released(view)) return;
        setPending(view, false);
        if (body?.ok === true && body.saved === true && body.state === 'answered') {
            closeDialog(view, { restoreFocus: false });
            accept(view.card, body);
            view.card.querySelector?.('.chat-quiz-question')?.focus?.({ preventScroll: true });
        } else setError(view, tr(...UNCONFIRMED), { unknown: true });
    }

    // Status first: only the exact-question read decides what the next OK may do.
    async function checkStatus(view) {
        if (view.pending) return;
        setPending(view, true);
        let question = null;
        try { question = await readQuestion(view.quiz); } catch { question = null; }
        if (released(view)) return;
        setPending(view, false);
        if (!question) { setError(view, tr('secret.status_unavailable', 'Status unavailable. Try again.'), { unknown: true }); return; }
        accept(view.card, question);
        if (question.state !== 'open' || !view.dialog) return;
        if (question.secret_save?.state === 'writing') {
            // Saving again replaces whatever the unconfirmed attempt may have stored: a new request.
            view.requestId = '';
            view.dialog.okLabel = tr('secret.save_again', 'Save again');
            setError(view, tr('secret.status_unknown', 'The earlier save is still unconfirmed. Save again replaces whatever it may have stored.'));
        } else setError(view, tr('secret.status_not_saved', 'Nothing was saved. OK saves these entries.'));
        view.dialog.ok.textContent = view.dialog.okLabel;
    }

    async function cancelRequest(view) {
        if (view.pending || !canUse(view.card) || view.card.dataset.state !== 'open') return;
        setPending(view, true);
        let body = null;
        try {
            body = await apiClient.cancelSecretRequest({ request_id: nonce(),
                decision_id: `quiz:${view.quiz.taskId}:${view.quiz.quizId}` }, transport);
        } catch (error) {
            body = error?.body && typeof error.body === 'object' ? error.body : null;
            if (!body?.state && !released(view)) showToast(tr('secret.cancel_unconfirmed', 'The cancellation was not confirmed. Check the request before trying again.'), 'error');
        }
        if (released(view)) return;
        setPending(view, false);
        if (body?.state) {
            closeDialog(view, { restoreFocus: false });
            accept(view.card, body);
        }
    }

    function release(card) {
        const view = views.get(card);
        if (!view) return;
        closeDialog(view, { restoreFocus: false });
        views.delete(card);
    }

    return { mount, sync, release, dispose: () => [...views.keys()].forEach(release), _views: views };
}
