import { haptic } from './haptics.js';
import { normalizeTone } from './ui_helpers.js';

function getStack() {
    let stack = document.getElementById('toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toast-stack';
        stack.className = 'toast-stack';
        stack.setAttribute('aria-live', 'polite');
        stack.setAttribute('aria-relevant', 'additions');
        document.body.appendChild(stack);
    }
    return stack;
}

/**
 * One toast; a press anywhere on it dismisses it. `ttl` 0 keeps it until then. `action` ({label, run}) adds one
 * button that dismisses the toast and runs (a recovery notice's way back, host_modules.js). An error toast
 * answers the owner's own action and plays Telegram's error haptic (haptics.js); a notice the owner did not
 * cause (a host incident) passes `{ haptic: false }`.
 */
export function showToast(message, tone = 'info', { ttl = 6000, action = null, haptic: felt = true } = {}) {
    const stack = getStack();
    const toast = document.createElement('div');
    const cleanTone = normalizeTone(tone || 'info', 'info');
    if (felt && cleanTone === 'danger') haptic('error');
    toast.className = `toast toast-${cleanTone}`;
    toast.setAttribute('role', cleanTone === 'danger' ? 'alert' : 'status');
    toast.textContent = message || '';
    const dismiss = () => {
        if (toast.classList.contains('is-hiding')) return;
        // It fades where it stood, out of the column, while the toasts that stay glide into its place at once: the
        // shift is measured here, the glide is the stylesheet's (none with reduced motion).
        const stay = [...(stack.children)].filter((el) => el !== toast);
        const before = stay.map((el) => el.offsetTop);
        Object.assign(toast.style, { position: 'absolute', top: `${toast.offsetTop}px`, right: '0', width: `${toast.offsetWidth}px` });
        toast.classList.add('is-hiding');
        const moved = stay.filter((el, i) => {
            const shift = before[i] - el.offsetTop;
            if (shift) { el.style.setProperty('--toast-shift', `${shift}px`); el.setAttribute('data-gliding', ''); }
            return shift !== 0;
        });
        void stack.offsetHeight;  // the shifted places are where the glide starts
        moved.forEach((el) => el.removeAttribute('data-gliding'));
        setTimeout(() => toast.remove(), 180);
    };
    if (action?.label && typeof action.run === 'function') {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-sm btn-default toast-action';
        button.textContent = action.label;
        button.addEventListener('click', (event) => {
            event.stopPropagation();
            dismiss();
            action.run();
        });
        toast.append(button);
    }
    stack.appendChild(toast);
    if (ttl > 0) setTimeout(dismiss, ttl);
    toast.addEventListener('click', dismiss);
    return toast;
}
