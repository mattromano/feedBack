/*
 * fee[dB]ack v0.3.0 — play-queue tray for the Songs toolbar.
 *
 * The player is fullscreen and leaving it abandons the active queue, so a
 * queue is BUILT in the library ("+" on a card, ⋮ → Add to queue, + Queue
 * album / + Queue all) and started from here. The list itself lives in
 * window.feedBack.playQueue (pending()/enqueue()/startPending(), persisted per
 * browser); this module only renders it: a toolbar button carrying the count
 * (hidden while nothing is queued) and the "Up next" panel it opens — reorder,
 * remove, play from a song, Play / Shuffle all, Clear. It re-renders on the
 * queue's 'queue:changed' event.
 *
 * songs.js rebuilds its toolbar on every render, so it calls
 * window.__fbMountQueueTray(button) each time; the event listener is bound
 * once and always targets the current button.
 */
(function () {
    'use strict';
    const sm = window.feedBack;
    if (!sm) return;

    let btn = null;        // current toolbar button (#v3-songs-queue)
    let panel = null;      // open panel element, or null
    let closePanel = null; // tears the panel + its document listeners down

    const q = () => (sm.playQueue && typeof sm.playQueue.pending === 'function') ? sm.playQueue : null;
    const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    // Untitled entries (queued by filename only) show the file's base name.
    const label = (it) => it.title || String(it.filename || '').split('/').pop().replace(/\.[^.]+$/, '');

    function renderButton() {
        if (!btn || !btn.isConnected) return;
        const queue = q();
        const n = queue ? queue.pending().length : 0;
        btn.classList.toggle('hidden', n === 0);
        btn.textContent = 'Queue · ' + n;
        btn.setAttribute('aria-expanded', panel ? 'true' : 'false');
        if (!n && closePanel) closePanel();
    }

    function renderPanel() {
        const queue = q();
        if (!panel || !queue) return;
        const items = queue.pending();
        const iconBtn = 'px-1.5 text-fb-textDim hover:text-fb-text disabled:opacity-30';
        panel.innerHTML =
            '<div class="flex items-center justify-between px-3 py-2 border-b border-fb-border/40">' +
            '<span class="text-sm font-semibold text-fb-text">Up next · ' + items.length + '</span>' +
            '<button data-q="clear" class="text-xs text-fb-textDim hover:text-fb-accent">Clear</button></div>' +
            '<ol class="max-h-72 overflow-y-auto v3-scroll py-1">' + items.map((it, i) =>
                '<li class="group flex items-center gap-2 px-3 py-1 hover:bg-white/5">' +
                '<span class="text-xs text-fb-textDim w-5 text-right shrink-0">' + (i + 1) + '</span>' +
                '<button data-q="from" data-i="' + i + '" title="Play from here" class="flex-1 min-w-0 text-left">' +
                '<span class="block text-sm text-fb-text truncate">' + esc(label(it)) + '</span>' +
                (it.artist ? '<span class="block text-xs text-fb-textDim truncate">' + esc(it.artist) + '</span>' : '') +
                '</button>' +
                '<span class="flex items-center shrink-0">' +
                '<button data-q="up" data-i="' + i + '" title="Move up" aria-label="Move up" class="' + iconBtn + '"' + (i === 0 ? ' disabled' : '') + '>↑</button>' +
                '<button data-q="down" data-i="' + i + '" title="Move down" aria-label="Move down" class="' + iconBtn + '"' + (i === items.length - 1 ? ' disabled' : '') + '>↓</button>' +
                '<button data-q="remove" data-i="' + i + '" title="Remove" aria-label="Remove from queue" class="' + iconBtn + ' hover:text-fb-accent">✕</button>' +
                '</span></li>').join('') + '</ol>' +
            '<div class="flex gap-2 px-3 py-2 border-t border-fb-border/40">' +
            '<button data-q="play" class="flex-1 bg-fb-primary hover:bg-fb-primaryHi text-white text-sm font-medium px-3 py-1.5 rounded-md">▶ Play</button>' +
            '<button data-q="shuffle" class="flex-1 bg-fb-card/60 hover:bg-fb-card border border-fb-border/50 text-fb-text text-sm px-3 py-1.5 rounded-md">⇄ Shuffle</button>' +
            '</div>';
    }

    function onPanelClick(e) {
        const b = e.target.closest('[data-q]');
        const queue = q();
        if (!b || !queue) return;
        e.stopPropagation();
        const act = b.getAttribute('data-q');
        const i = Number(b.getAttribute('data-i'));
        if (act === 'up') queue.movePending(i, i - 1);
        else if (act === 'down') queue.movePending(i, i + 1);
        else if (act === 'remove') queue.removePending(i);
        else if (act === 'clear') queue.clearPending();
        else if (act === 'play' || act === 'shuffle' || act === 'from') {
            if (closePanel) closePanel();
            queue.startPending(act === 'from' ? { from: i } : { shuffle: act === 'shuffle' });
        }
    }

    function openPanel() {
        if (panel || !btn) return;
        panel = document.createElement('div');
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-label', 'Play queue');
        panel.className = 'fixed z-40 w-72 max-w-[90vw] bg-fb-card border border-fb-border/60 rounded-lg shadow-xl';
        panel.addEventListener('click', onPanelClick);
        document.body.appendChild(panel);
        renderPanel();
        // Anchor under the button, clamped to the viewport.
        const r = btn.getBoundingClientRect();
        panel.style.top = Math.round(r.bottom + 6) + 'px';
        panel.style.left = Math.round(Math.max(8, Math.min(r.right - panel.offsetWidth, window.innerWidth - panel.offsetWidth - 8))) + 'px';
        const outside = (e) => { if (panel && !panel.contains(e.target) && e.target !== btn) closePanel(); };
        const onKey = (e) => { if (e.key === 'Escape') closePanel(); };
        closePanel = () => {
            document.removeEventListener('click', outside, true);
            document.removeEventListener('keydown', onKey);
            if (panel) panel.remove();
            panel = null;
            closePanel = null;
            renderButton();
        };
        document.addEventListener('click', outside, true);
        document.addEventListener('keydown', onKey);
        renderButton();
    }

    window.__fbMountQueueTray = function (el) {
        if (closePanel) closePanel();
        btn = el || null;
        if (!btn) return;
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (panel) closePanel(); else openPanel();
        });
        renderButton();
    };

    sm.on('queue:changed', () => { renderButton(); renderPanel(); });
    // Leaving the library (e.g. a queued song starts) drops the panel.
    sm.on('screen:changed', () => { if (closePanel) closePanel(); });
})();
