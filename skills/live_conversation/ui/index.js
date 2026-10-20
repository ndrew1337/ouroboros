/** Loaded, revision-bound Talk A feature; all device capture starts at the owner's press. */
import { mountCall } from './call.js';
import { fetchJson } from '/static/modules/api_client.js';
export function mount(ctx) {
    const rooms = new Set();
    let config = { enabled: false }, disposed = false;
    async function refresh() {
        try { config = await fetchJson(`/api/extensions/${encodeURIComponent(ctx.skill)}/config`, { cache: 'no-store' }); }
        catch { config = { enabled: false }; }
        if (!disposed) for (const room of rooms) room.state(config);
    }
    const unregister = ctx.providers.register('conversation', {
        mountRoom(options) {
            const room = mountCall(options);
            rooms.add(room); room.state(config);
            const destroy = room.destroy;
            room.destroy = () => { rooms.delete(room); destroy(); };
            return room;
        },
    });
    // Settings writes do not require a page reload. This read cannot enable a
    // skill or reveal a key; it returns only the feature's explicit configuration.
    void refresh();
    const timer = setInterval(refresh, 5000);
    function dispose() {
        if (disposed) return;
        disposed = true; clearInterval(timer); unregister();
        for (const room of [...rooms]) room.destroy();
    }
    ctx.signal.addEventListener('abort', dispose, { once: true });
    if (ctx.signal.aborted) dispose();
    return { dispose };
}
