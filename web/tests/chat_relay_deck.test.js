// Notes between works (chat_relay_deck.js) over the DOM double: a relay row's bubble is dressed as a note card and
// keeps its row (address, history identity, actions); a run of notes is one deck with the latest card in front and the
// older ones tucked; the owner's words keep a card of their own; a row between notes splits the run and taking it out
// joins it again, still open; a note without the teller's line opens with its own words in the meta ink; the deck fans
// out and folds back; a note opens as written; a quote's jump to a tucked note opens its deck. Motion and layout run in
// the browser lane.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createRelayDecks } from '../modules/chat_relay_deck.js';
import { createChatReplies } from '../modules/chat_reply.js';
import { installDomDouble } from './dom_double.js';

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function fixture() {
    const env = installDomDouble();
    const { el } = env;
    const feed = el('div', { className: 'chat-messages' });
    env.doc.body.append(feed);
    const decks = createRelayDecks({ feed });
    let seq = 0;
    const note = (text, { summary = '', sender = { name: 'Main', chat_id: 1 }, quote = '', source = null } = {}) => {
        seq += 1;
        const ts = `2026-10-21T23:${String(10 + seq).padStart(2, '0')}:00Z`;
        const bubble = el('div', { className: 'chat-bubble assistant',
            dataset: { systemType: 'relay_entry', ts: String(Date.parse(ts)), rowAddress: `row:${seq}@r#${seq}` } },
        el('div', { className: 'sender', text: 'Ouroboros' }),
        ...(quote ? [el('div', { className: 'chat-quote', dataset: { mode: 'relay' }, text: quote })] : []),
        el('div', { className: 'message', text }),
        el('div', { className: 'msg-time', text: '23:10' }),
        el('div', { className: 'chat-message-actions' }, el('button', { className: 'chat-message-reply' }), el('button', { className: 'chat-message-copy' })));
        const relay = { relay_id: `r${seq}`, text: quote, quotes: quote ? [{ text: quote }] : [],
            ...(summary ? { summary } : {}), ...(sender ? { sender } : {}), ...(source ? { source } : {}) };
        decks.adopt(bubble, { text, ts, relay }, Boolean(quote));  // dressed before it lands, as decorate does
        feed.append(bubble);
        return bubble;
    };
    const row = (text) => {
        const bubble = el('div', { className: 'chat-bubble user', text });
        feed.append(bubble);
        return bubble;
    };
    const visible = (bubble) => bubble.dataset.deckTucked !== '1';
    const act = (bubble) => bubble.querySelector('.chat-relay-act');
    return { env, feed, decks, note, row, visible, act, close() { decks.destroy(); env.restore(); } };
}

test('a run of notes is one deck of the rows themselves, the latest note in front, the older tucked', async () => {
    const f = fixture();
    try {
        const rows = Array.from({ length: 7 }, (_, i) => f.note(`raw${i}:glued-work-words`, { summary: `Line ${i} for the owner` }));
        await settle();
        const front = rows[6];
        assert.equal(f.feed.children.length, 7, 'no element is added to the feed: the rows are the cards');
        assert.ok(rows.every((bubble) => bubble.classList.contains('chat-relay-note') && !bubble.hidden), 'every row stays a row');
        assert.ok(rows.every((bubble, i) => bubble.dataset.rowAddress === `row:${i + 1}@r#${i + 1}`), 'addresses unchanged');
        assert.ok(rows.slice(0, 6).every((bubble) => bubble.dataset.deckTucked === '1'), 'the older notes are tucked');
        assert.equal(f.visible(front), true);
        assert.equal(front.dataset.deckEdges, '2', 'the front card draws two edges behind it');
        assert.equal(front.querySelector('.chat-relay-from').textContent, 'Main');
        assert.equal(front.querySelector('.chat-relay-gist').textContent, 'Line 6 for the owner');
        assert.equal(f.act(front).textContent, 'Show 7 notes');
        assert.equal(f.act(front).getAttribute('aria-expanded'), 'false');
        assert.equal(front.dataset.raw, '0', 'the note as written waits for a press');
    } finally { f.close(); }
});

test('the owner\'s words keep a card of their own, and any other row ends a run', async () => {
    const f = fixture();
    try {
        const first = f.note('first');
        const second = f.note('second');
        f.row('owner says something');
        const quoted = f.note('third, passing his words', { quote: 'please check the logs', summary: 'Asks the work to check the logs' });
        const fourth = f.note('fourth');
        await settle();
        assert.equal(first.dataset.deckTucked, '1');
        assert.equal(second.dataset.deckEdges, '1', 'two notes: one edge behind the front card');
        assert.equal(quoted.dataset.deck, undefined, 'a relay with his words is a card of its own');
        const kids = quoted.children;
        const quoteAt = kids.indexOf(quoted.querySelector('.chat-quote'));
        assert.ok(quoteAt > kids.indexOf(quoted.querySelector('.chat-relay-card-head'))
            && quoteAt < kids.indexOf(quoted.querySelector('.chat-relay-gist')), 'his words between the head and the line');
        assert.equal(fourth.dataset.deck, undefined, 'a note after his words starts anew');
        assert.equal(f.act(fourth).textContent, 'Show note');
    } finally { f.close(); }
});

test('a later note comes to the front; a row landing between notes splits the run and taking it out joins it, still open', async () => {
    const f = fixture();
    try {
        const one = f.note('one');
        const two = f.note('two');
        await settle();
        const three = f.note('three');
        await settle();
        assert.equal(three.dataset.deckEdges, '2');
        assert.equal(two.dataset.deckTucked, '1', 'the previous front card joins the deck behind the new one');
        assert.equal(f.act(three).textContent, 'Show 3 notes');
        const between = f.env.el('div', { className: 'chat-bubble user', text: 'between' });
        f.feed.insertBefore(between, two);
        f.decks.schedule();
        await settle();
        assert.equal(one.dataset.deck, undefined, 'one note before the row is a card on its own');
        assert.equal(f.visible(one), true);
        assert.equal(two.dataset.deckTucked, '1');
        assert.equal(three.dataset.deckEdges, '1');
        f.act(three).click();
        assert.equal(three.dataset.deck, 'open');
        between.remove();
        f.decks.schedule();
        await settle();
        assert.ok([one, two, three].every((bubble) => bubble.dataset.deck === 'open'), 'the joined deck stays open');
        assert.equal(one.dataset.deckFirst, '1', 'the caption moves to the new first card');
        assert.match(one.querySelector('.chat-relay-deck-top').textContent, /^3 notes/);
    } finally { f.close(); }
});

test('an ordinary reconcile rewrites nothing: a focused Hide keeps its focus, a front card keeps its count node', async () => {
    const f = fixture();
    try {
        const rows = [f.note('a'), f.note('b'), f.note('c')];
        await settle();
        const count = f.act(rows[2]).querySelector('.chat-relay-count');
        f.decks.schedule();
        await settle();
        assert.equal(f.act(rows[2]).querySelector('.chat-relay-count'), count, 'an unchanged deck keeps its nodes');
        f.act(rows[2]).click();
        const hide = rows[0].querySelector('.chat-relay-hide');
        hide.focus();
        f.row('an answer lands');
        f.decks.schedule();
        await settle();
        assert.equal(rows[0].querySelector('.chat-relay-hide'), hide, 'the caption stays the same node');
        assert.equal(f.env.doc.activeElement, hide, 'and Hide keeps the focus');
    } finally { f.close(); }
});

test('a focused Hide follows the caption when an older note becomes the first card of an open deck', async () => {
    const f = fixture();
    try {
        const later = [f.note('b'), f.note('c')];
        await settle();
        f.act(later[1]).click();
        later[0].querySelector('.chat-relay-hide').focus();
        const older = f.note('a, older');
        f.feed.insertBefore(older, later[0]);
        f.decks.schedule();
        await settle();
        assert.equal(older.dataset.deckFirst, '1');
        assert.equal(later[0].querySelector('.chat-relay-deck-top'), null, 'the caption left the old first card');
        assert.equal(f.env.doc.activeElement, older.querySelector('.chat-relay-hide'), 'and Hide kept the focus');
    } finally { f.close(); }
});

test('a note read on its own opens the deck it joins, so the passage stays; Hide keeps a deck shut', async () => {
    const f = fixture();
    try {
        const first = f.note('a long note being read');
        await settle();
        f.act(first).click();
        assert.equal(first.dataset.raw, '1');
        const second = f.note('a second note arrives');
        await settle();
        assert.equal(first.dataset.deck, 'open', 'the deck it joins opens');
        assert.equal(f.visible(first), true, 'the note being read stays on screen');
        first.querySelector('.chat-relay-hide').click();
        assert.equal(first.dataset.deckTucked, '1');
        f.note('a third note');
        await settle();
        assert.equal(second.dataset.deck, 'closed', 'after Hide a new note keeps the deck shut');
        assert.equal(first.dataset.raw, '1', 'its own open text stays open behind the front card');
    } finally { f.close(); }
});

test('a note without the teller\'s line opens with its own words, in the meta ink; a summary leads otherwise', async () => {
    const f = fixture();
    try {
        const raw = f.note('a0e1:research-done,no-safe-restore');
        f.row('break');
        const summarized = f.note('a0e1:research-done', { summary: 'Research is done; no safe fix yet' });
        await settle();
        assert.equal(raw.querySelector('.chat-relay-gist').dataset.raw, '1');
        assert.equal(raw.querySelector('.chat-relay-gist').textContent, 'a0e1:research-done,no-safe-restore');
        assert.equal(summarized.querySelector('.chat-relay-gist').dataset.raw, undefined, 'the teller\'s line reads in the voice\'s ink');
        assert.equal(summarized.querySelector('.chat-relay-gist').textContent, 'Research is done; no safe fix yet');
    } finally { f.close(); }
});

test('a note saved before its room was recorded says Main only when it came from Main', async () => {
    const f = fixture();
    try {
        const main = f.note('from main', { sender: null, source: { chat_id: 1, label: 'Main' } });
        f.row('break');
        const unknown = f.note('from a project', { sender: null, source: { chat_id: 52601710, label: 'Main' } });
        f.row('break');
        const named = f.note('named', { sender: { name: 'Claudexor 478', chat_id: 52601710 } });
        await settle();
        assert.equal(main.querySelector('.chat-relay-from').textContent, 'Main');
        assert.equal(main.dataset.kind, 'main');
        assert.equal(unknown.querySelector('.chat-relay-from').textContent, 'Another work', 'never an invented room');
        assert.equal(unknown.dataset.kind, 'project');
        assert.equal(named.querySelector('.chat-relay-from').textContent, 'Claudexor 478');
        assert.equal(named.dataset.kind, 'project');
    } finally { f.close(); }
});

test('Show N notes fans the deck out; Show note opens the note as written with the row\'s actions; Hide folds it', async () => {
    const f = fixture();
    try {
        const rows = [f.note('w1'), f.note('w2:as-written'), f.note('w3')];
        await settle();
        f.act(rows[2]).click();
        assert.ok(rows.every((bubble) => bubble.dataset.deck === 'open' && f.visible(bubble)), 'every card stands');
        assert.equal(rows[0].dataset.deckFirst, '1');
        assert.match(rows[0].querySelector('.chat-relay-deck-top').textContent, /^3 notes · /);
        assert.equal(rows[2].dataset.deckEdges, undefined, 'open, no edges');
        const middle = rows[1];
        assert.equal(f.act(middle).textContent, 'Show note');
        f.act(middle).click();
        assert.equal(middle.dataset.raw, '1', 'the note as written and its Reply and Copy show');
        assert.equal(f.act(middle).textContent, 'Hide note');
        assert.equal(f.act(middle).getAttribute('aria-expanded'), 'true');
        f.act(middle).click();
        assert.equal(middle.dataset.raw, '0');
        rows[0].querySelector('.chat-relay-hide').click();
        assert.ok(rows.slice(0, 2).every((bubble) => bubble.dataset.deckTucked === '1'));
        assert.equal(f.act(rows[2]).textContent, 'Show 3 notes');
        assert.equal(rows[0].querySelector('.chat-relay-deck-top'), null, 'folded, no caption');
    } finally { f.close(); }
});

test('a quote\'s jump to a tucked note opens its deck; a visible note or any other row stays as it is', async () => {
    const f = fixture();
    try {
        const first = f.note('one');
        const second = f.note('two');
        const owner = f.row('mine');
        await settle();
        assert.equal(f.decks.surface(second), null, 'the front card is already visible');
        assert.equal(f.decks.surface(first), first);
        assert.equal(first.dataset.deck, 'open');
        assert.equal(f.visible(first), true);
        assert.equal(f.decks.surface(owner), null);
    } finally { f.close(); }
});

test('a relay row the room decorates is dressed as a note; a bubble without the relay type stays a bubble', async () => {
    const env = installDomDouble();
    const feed = env.el('div', { className: 'chat-messages' });
    const input = env.el('textarea', { attrs: { id: 'chat-input' } });
    env.doc.body.append(feed, env.el('div', { className: 'chat-text-row' }, input));
    const replies = createChatReplies({ chatId: 7, feed, input, reading: () => ({ cancel() {}, scroll() {}, remember() {}, stick: true }) });
    try {
        const make = (dataset) => env.el('div', { className: 'chat-bubble assistant', dataset },
            env.el('div', { className: 'sender', text: 'Ouroboros' }), env.el('div', { className: 'message', text: 'words' }));
        const relayRow = make({ systemType: 'relay_entry' });
        const plain = make({});
        const relay = { relay_id: 'r', text: '', quotes: [], summary: 'Tells the work what to check', sender: { name: 'Main', chat_id: 1 } };
        replies.decorate(relayRow, { row_address: 'row:1@a#1', role: 'assistant', text: 'words', relay });
        replies.decorate(plain, { row_address: 'row:2@a#2', role: 'assistant', text: 'words', relay });
        feed.append(relayRow, plain);
        await settle();
        assert.equal(relayRow.classList.contains('chat-relay-note'), true);
        assert.equal(relayRow.querySelector('.chat-relay-gist').textContent, 'Tells the work what to check');
        assert.equal(relayRow.querySelector('.chat-message-reply') !== null, true, 'the row keeps its own Reply');
        assert.equal(plain.classList.contains('chat-relay-note'), false, 'only a relay row becomes a note');
    } finally { replies.destroy(); env.restore(); }
});
