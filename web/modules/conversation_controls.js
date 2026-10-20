/** A loaded conversation feature mounts in the ordinary room's composer lifetime. */
let hosts = null;
let openSettings = () => {};
const rooms = new Set();
export function startConversations({ hosts: runtime, openSettings: settings }) {
    hosts = runtime;
    openSettings = settings;
    runtime.onProvidersChange(() => { for (const room of rooms) room.sync(); });
    for (const room of rooms) room.sync();
}
export function bindConversationControls({ composer, page, chatId, ws }) {
    let active = null, key = null;
    const priorPending = composer.hasPendingWork;
    composer.hasPendingWork = () => Boolean(active?.retains?.()) || priorPending();
    const room = { sync() {
        const available = hosts?.clientProviders('conversation') || [];
        // Ambiguous feature selection never substitutes another active feature.
        const entry = available.length === 1 ? available[0] : null;
        if (entry === key) return;
        active?.destroy(); active = null; key = entry;
        if (typeof entry?.impl.mountRoom === 'function')
            active = entry.impl.mountRoom({ page, chatId, ws, composer, openSettings,
                onScreen: () => page.getClientRects().length > 0 });
    } };
    rooms.add(room); room.sync();
    return () => { rooms.delete(room); active?.destroy(); composer.hasPendingWork = priorPending; };
}
