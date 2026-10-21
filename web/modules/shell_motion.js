/** The phone shell's motion between places (docs/DESIGN.md §10 "Motion"; owner 20.10, 4A), presentation only.
 *
 * On a phone the room is the card the drawer pushes aside. A place chosen in the open drawer changes inside that card
 * (owner 4A: «Карточка возвращается»): the drawer stays painted open for one beat, the leaving room's words fade out
 * and the arriving room fades in with one breath, and the chosen place's fill, the pill, slides from its old row to the
 * tapped one («Маяк места»); then the drawer's own close brings the card home. Opening the drawer switches the pill on
 * at the place you are in.
 *
 * `settled()` is the end of what the shell is doing: the room card's own transform transition (motion.js), or the beat
 * and the close after it. The layer stack waits for it before it makes the covered room inert, and the app before it
 * puts away a room that was left. Every animation here is WAAPI on the real elements and ends in the stylesheet's own
 * state, so a cancel (a new navigation, a finger on the drawer, the layout switching, the motion preference changing,
 * the page going away) leaves nothing behind. It chooses no destination, writes no history, reads nothing from the
 * server and keeps no timer but the beat. Docked it does nothing; under reduced motion there is no beat and no pill,
 * only a short fade of the arriving room. */

import { afterMotion, onReducedMotionChange, reducedMotion } from './motion.js';

export const MOTION = Object.freeze({
    hold: 160,       // ms the drawer stays painted open while the rooms hand over
    out: 100,        // the leaving room's words fade (ease-in)
    inDelay: 60,     // then the arriving room starts …
    in: 220,         // … and materialises: opacity on `ease`, one breath of its content on the sheet curve
    breath: 0.97,    // the arriving content's starting scale
    reducedIn: 160,  // reduced motion: the arriving room's fade, nothing else
    pillMin: 160,    // the pill's slide: 160 ms plus 0.06 ms a pixel, at most 220 ms
    pillPerPx: 0.06,
    pillMax: 220,
    pillTurn: 0.8,   // a place pill turning into a Project's veil (or back) cross-fades over this share of its slide
    sweep: 300,      // opening the drawer: the pill switches on at the current place …
    sweepDelay: 50,  // … a moment after the drawer starts
    switchOn: 0.84,  // the pill grows from its left mark at this width
});
const SHEET = 'cubic-bezier(0.32, 0.72, 0, 1)';  // --ease-sheet's own value, where the token cannot be read

export function createShellMotion({ doc = globalThis.document, win = globalThis.window, content, sidebar,
    isStack = () => true, onPaint = () => {} } = {}) {
    let painted = Boolean(doc?.body?.classList.contains('nav-drawer-open'));
    let wait = null;      // the motion settled() answers for: { done, cancel }
    let beat = null;      // the handover or the open sweep in flight
    let disposed = false;
    const ease = () => {
        try { return win.getComputedStyle(doc.documentElement).getPropertyValue('--ease-sheet').trim() || SHEET; } catch { return SHEET; }
    };
    const animatable = (el) => typeof el?.animate === 'function';

    // ---- the settle: the card's own transform transition, or the beat and the close after it
    function arm(next) {
        const previous = wait;
        wait = next;
        previous?.cancel();   // the latest motion is the one waited for
        next.done.then(() => { if (wait === next) wait = null; });
    }
    function settled() {
        const now = Boolean(doc?.body?.classList.contains('nav-drawer-open'));
        if (now !== painted) {
            painted = now;
            if (content) arm(afterMotion(content, { property: 'transform', win }));
        }
        return wait ? wait.done : Promise.resolve('none');
    }

    // ---- one beat at a time: what it animates, adds and marks, so the next navigation or its end undoes it all
    function newBeat() {
        finish(false);   // the caller paints: a handover starts inside the owner's own paint
        const b = { anims: [], nodes: [], covers: [], classes: [], marks: [], timer: 0, holding: false, onHold: null };
        b.animate = (el, frames, timing) => {
            if (!animatable(el)) return null;
            const anim = el.animate(frames, timing);
            b.anims.push(anim);
            return anim;
        };
        b.add = (el, name) => { if (el && !el.classList.contains(name)) { el.classList.add(name); b.classes.push([el, name]); } };
        b.mark = (el) => { if (el && !el.hasAttribute('data-leaving')) { el.setAttribute('data-leaving', ''); b.marks.push(el); } };
        beat = b;
        return b;
    }
    function finish(repaint = true) {
        const b = beat;
        if (!b) return;
        beat = null;
        if (b.timer) win.clearTimeout(b.timer);
        for (const anim of b.anims) { try { anim.cancel(); } catch { /* gone */ } }
        b.nodes.forEach((node) => node.remove());
        b.covers.forEach((node) => node.remove());
        for (const [el, name] of b.classes) el.classList.remove(name);
        b.marks.forEach((el) => el.removeAttribute('data-leaving'));
        if (b.holding) {
            b.holding = false;
            if (repaint) b.onHold?.();   // a held drawer is painted with the real state
        }
    }

    // ---- the pill: the chosen place's fill, under the drawer's rows, from one row to another
    const kindOf = (row) => (row?.matches?.('.nav-project-row') ? 'project' : 'place');
    function inSight(row) {
        if (!row?.isConnected || !row.getClientRects?.().length || row.closest?.('[hidden]')) return false;
        const r = row.getBoundingClientRect();
        const s = sidebar.getBoundingClientRect();
        if (r.top < s.top - 1 || r.bottom > s.bottom + 1) return false;
        const list = row.closest?.('.nav-projects-list');
        if (!list) return true;
        const l = list.getBoundingClientRect();
        return r.top >= l.top - 1 && r.bottom <= l.bottom + 1;
    }
    function boxOf(row) {
        const r = row.getBoundingClientRect();
        const s = sidebar.getBoundingClientRect();
        return { left: r.left - s.left - sidebar.clientLeft + sidebar.scrollLeft,
            top: r.top - s.top - sidebar.clientTop + sidebar.scrollTop, width: r.width, height: r.height };
    }
    function pillAt(b, row) {
        const box = boxOf(row);
        const pill = doc.createElement('div');
        pill.className = 'shell-pill';
        pill.setAttribute('aria-hidden', 'true');
        Object.assign(pill.style, { left: `${box.left}px`, top: `${box.top}px`, width: `${box.width}px`, height: `${box.height}px` });
        const looks = {};
        for (const kind of ['place', 'project']) {
            const look = pill.appendChild(doc.createElement('i'));
            look.className = `shell-pill-${kind}`;
            look.style.opacity = kindOf(row) === kind ? '1' : '0';
            looks[kind] = look;
        }
        sidebar.prepend(pill);
        b.nodes.push(pill);
        b.add(sidebar, 'shell-sliding');
        return { pill, looks, box, kind: kindOf(row) };
    }
    function switchOn(b, row, delay = 0, duration = MOTION.pillMin) {
        const { pill } = pillAt(b, row);
        b.animate(pill, [{ opacity: 0, transform: `scaleX(${MOTION.switchOn})` }, { opacity: 1, transform: 'none' }],
            { delay, duration, easing: ease(), fill: 'both' });
        return delay + duration;
    }
    function slide(b, fromRow, toRow) {
        if (!fromRow || fromRow === toRow || !inSight(fromRow) || !inSight(toRow)) return switchOn(b, toRow);
        const from = pillAt(b, fromRow);
        const dy = boxOf(toRow).top - from.box.top;
        const ms = Math.min(MOTION.pillMax, MOTION.pillMin + Math.abs(dy) * MOTION.pillPerPx);
        b.animate(from.pill, [{ transform: 'none' }, { transform: `translateY(${dy}px)` }], { duration: ms, easing: ease(), fill: 'forwards' });
        const to = kindOf(toRow);
        if (to !== from.kind) {
            const turn = { duration: ms * MOTION.pillTurn, easing: 'ease-in-out', fill: 'forwards' };
            b.animate(from.looks[from.kind], [{ opacity: 1 }, { opacity: 0 }], turn);
            b.animate(from.looks[to], [{ opacity: 0 }, { opacity: 1 }], turn);
        }
        return ms;
    }
    // The pill hands the place back to the row's own fill in one frame: the rows' tone transition is off while the
    // fill returns, or the row would blink transparent and fade back.
    function handOff(b) {
        if (!b.nodes.length) return;
        sidebar.classList.add('shell-handoff');
        b.nodes.forEach((node) => node.remove());
        b.nodes.length = 0;
        sidebar.classList.remove('shell-sliding');
        b.classes = b.classes.filter(([el, name]) => !(el === sidebar && name === 'shell-sliding'));
        void win.getComputedStyle(sidebar).backgroundColor;   // commit the rows' fill while transitions are off
        win.requestAnimationFrame(() => sidebar.classList.remove('shell-handoff'));
    }

    // A room's words fade. Main lies under every other room, so its words fade under a cover of the room's own
    // background inside it (the keyframes inverted) instead of by the page's opacity: animating the opacity of a room
    // of thousands of elements restyles all of them in WebKit. A room that lies over another fades itself, as the one
    // under it must show through.
    function fade(b, el, frames, timing) {
        if (!el || el.id !== 'page-chat') { b.animate(el, frames, timing); return; }
        const cover = doc.createElement('div');
        cover.className = 'shell-cover';
        cover.setAttribute('aria-hidden', 'true');
        el.append(cover);
        b.covers.push(cover);
        b.animate(cover, frames.map((f) => ({ opacity: 1 - f.opacity })), timing);
    }

    /** A place chosen in the open drawer. `leaving`: what holds the old room's words (a page, the Project panel, a
     * retired Project instance); `arriving`: the new room's element, `breathe`: what of it takes the breath (the
     * room itself, or the panel's content where the panel is the card); the rows the pill slides between. */
    function handover({ leaving = [], arriving = null, breathe = [], fromRow = null, toRow = null } = {}) {
        if (disposed || !isStack()) return;
        const b = newBeat();
        if (reducedMotion(win)) {
            fade(b, arriving, [{ opacity: 0 }, { opacity: 1 }], { duration: MOTION.reducedIn, easing: 'ease-out', fill: 'backwards' });
            // Nothing holds this beat: what it added goes once its fade has run.
            Promise.all(b.anims.map((anim) => anim.finished.catch(() => null))).then(() => { if (beat === b) finish(false); });
            return;
        }
        b.holding = true;
        b.onHold = onPaint;
        for (const el of leaving) {
            // A page that lost .active stays displayed for the beat.
            if (el?.classList?.contains('page') && !el.classList.contains('active')) b.mark(el);
            fade(b, el, [{ opacity: 1 }, { opacity: 0 }], { duration: MOTION.out, easing: 'ease-in', fill: 'forwards' });
        }
        const timing = { delay: MOTION.inDelay, duration: MOTION.in, fill: 'both' };
        fade(b, arriving, [{ opacity: 0 }, { opacity: 1 }], { ...timing, easing: 'ease' });
        for (const el of breathe) {
            b.animate(el, [{ transform: `scale(${MOTION.breath})` }, { transform: 'none' }], { ...timing, easing: ease() });
        }
        const pillMs = toRow ? slide(b, fromRow, toRow) : 0;
        // The settle answers for the beat until the close it hands over to arms its own wait (settled() after onPaint).
        let release;
        arm({ done: new Promise((resolve) => { release = resolve; }), cancel: () => release('cancel') });
        const end = () => {
            b.timer = 0;
            if (beat !== b) return;
            b.holding = false;
            onPaint();
            release('hold');
            win.setTimeout(() => { if (beat === b) handOff(b); }, Math.max(0, pillMs - MOTION.hold));
            const close = wait;
            (close ? close.done : Promise.resolve()).then(() => { if (beat === b) finish(); });
        };
        b.timer = win.setTimeout(end, MOTION.hold);
    }

    /** The drawer opened: the pill switches on at the place you are in, then hands over to its row. */
    function sweep(row) {
        if (disposed || !isStack() || reducedMotion(win) || !row || !inSight(row)) return;
        const b = newBeat();
        const ms = switchOn(b, row, MOTION.sweepDelay, MOTION.sweep);
        b.timer = win.setTimeout(() => { if (beat === b) { handOff(b); finish(); } }, ms + 20);
    }

    /** Ends whatever runs, at once: animations to the stylesheet's state, nodes and marks gone, a held drawer painted
     * with the real state. */
    function cancel() {
        finish();
    }

    const disposeReduce = onReducedMotionChange(() => cancel(), win);
    const onPageHide = () => cancel();
    win?.addEventListener?.('pagehide', onPageHide);

    return {
        settled,
        holding: () => Boolean(beat?.holding),
        handover,
        sweep,
        cancel,
        destroy() {
            disposed = true;
            cancel();
            wait?.cancel();
            disposeReduce();
            win?.removeEventListener?.('pagehide', onPageHide);
        },
    };
}
