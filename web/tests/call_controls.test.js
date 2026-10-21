import test from 'node:test';
import assert from 'node:assert/strict';
import { bindConversationControls, startConversations } from '../modules/conversation_controls.js';

test('conversation mounts receive the room label, reveal, visibility and settled callback from navigation', () => {
    let options, shown = false, active = true, pending = false, destroyed = 0, revealed = 0, released = 0;
    const entry = { impl: { mountRoom(value) { options = value; return {
        retains: () => active, destroy: () => destroyed++,
    }; } } };
    const runtime = { onProvidersChange() {}, clientProviders: () => [entry] };
    startConversations({ hosts: runtime, openSettings() {}, roomOptions: ({ chatId }) => ({
        label: () => `Project ${chatId}`, onScreen: () => shown,
        reveal() { shown = true; revealed++; }, onSettled() { released++; },
    }) });
    const composer = { hasPendingWork: () => pending };
    const dispose = bindConversationControls({ composer, page: { getClientRects: () => [1] }, chatId: 71 });
    try {
        assert.equal(options.label?.(), 'Project 71');
        assert.equal(options.onScreen(), false);
        options.reveal();
        assert.equal(revealed, 1);
        assert.equal(options.onScreen(), true);
        assert.equal(composer.hasPendingWork(), true);
        active = false;
        assert.equal(composer.hasPendingWork(), false);
        pending = true;
        assert.equal(composer.hasPendingWork(), true);
        options.onSettled();
        assert.equal(released, 1);
    } finally { dispose(); }
    assert.equal(destroyed, 1);
    assert.equal(composer.hasPendingWork(), true, 'original pending-work owner restored');
});
