// ── State ───────────────────────────────────────────────────────────────────────

const state = {
    events: [],
    eras: [],
    selectedId: null,
    deleteConfirmId: null,
    editingId: null,
    reflectionData: { questions: [], answers: [], currentQ: 0 },
    newEventId: null,
    displayOrder: 'past-to-present',
    arrangementMode: 'chronological',
    draggedEventId: null,
    reorderPending: false,
    auth: {
        status: 'loading',
        user: null,
        message: 'Checking your session…',
    },
};

const DISPLAY_ORDERS = {
    ASCENDING: 'past-to-present',
    DESCENDING: 'present-to-past',
};

const ARRANGEMENT_MODES = {
    CHRONOLOGICAL: 'chronological',
    MANUAL: 'manual',
};

const DISPLAY_ORDER_STORAGE_KEY = 'chronoscape.displayOrder';
const ARRANGEMENT_MODE_STORAGE_KEY = 'chronoscape.arrangementMode';

const ERA_COLORS = [
    '#355C4D', '#496A54', '#62734A', '#7B6A42',
    '#C96B3D', '#9C5A41', '#4C6257', '#B77A4A',
];

const ERA_COLOR_NAMES = {
    '#355C4D': 'Pine',
    '#496A54': 'Cedar',
    '#62734A': 'Moss',
    '#7B6A42': 'Lichen',
    '#C96B3D': 'Persimmon',
    '#9C5A41': 'Terracotta bark',
    '#4C6257': 'Juniper',
    '#B77A4A': 'Clay amber',
};

const SENTIMENT_LABELS = {
    '-5': 'Deeply painful',  '-4': 'Very difficult',     '-3': 'Difficult',
    '-2': 'Somewhat difficult', '-1': 'Slightly negative', '0': 'Neutral',
    '1': 'Slightly positive', '2': 'Somewhat positive',  '3': 'Good',
    '4': 'Very good',        '5': 'Profoundly meaningful',
};

let selectedEraColor = ERA_COLORS[0];
let deleteEraConfirm = null;
let _returnFocus = null;
let _drawerReturnFocus = null;
let _focusDrawerOnRender = false;

// ── Init ────────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', init);

async function init() {
    state.displayOrder = loadDisplayOrderPreference();
    state.arrangementMode = loadArrangementModePreference();
    bindStaticEventHandlers();
    renderAuthShell();
    renderTimeline();

    try {
        const me = await api('/auth/me', { handle401: false });
        state.auth = {
            status: 'authenticated',
            user: me.user,
            message: '',
        };
        await Promise.all([loadEvents(), loadEras()]);
        renderAuthShell();
        renderTimeline();
        scrollToPreferredAnchor();
    } catch (e) {
        handleLoggedOut('Sign in to access your private timeline.');
    }
}

// ── API helpers ─────────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
    const {
        rawResponse = false,
        handle401 = true,
        ...fetchOpts
    } = opts;
    const method = (fetchOpts.method || 'GET').toUpperCase();
    const headers = new Headers(fetchOpts.headers || {});

    fetchOpts.credentials = 'same-origin';
    if (opts.body && !(opts.body instanceof FormData)) {
        headers.set('Content-Type', 'application/json');
        fetchOpts.body = JSON.stringify(opts.body);
    } else if (opts.body) {
        fetchOpts.body = opts.body;
    }

    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
        const csrf = getCookie('chronoscape_csrf');
        if (csrf) headers.set('X-CSRF-Token', csrf);
    }

    fetchOpts.headers = headers;

    const res = await fetch(path, fetchOpts);
    if (res.status === 401) {
        if (handle401) {
            handleLoggedOut('Your session ended. Sign in again.');
        }
        throw new Error('Unauthorized');
    }
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        let msg = 'Request failed';
        if (err.detail) {
            if (Array.isArray(err.detail)) {
                // Formatting Pydantic validation error
                msg = err.detail.map(d => `${d.loc.join('.')}: ${d.msg}`).join(', ');
            } else if (typeof err.detail === 'string') {
                msg = err.detail;
            } else {
                msg = JSON.stringify(err.detail);
            }
        }
        throw new Error(msg);
    }
    if (rawResponse) return res;
    if (res.status === 204) return null;
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
        return res.json();
    }
    return res.text();
}

async function loadEvents() {
    state.events = await api('/api/events');
}

async function loadEras() {
    state.eras = await api('/api/eras');
}

function bindStaticEventHandlers() {
    setupKeyboard();
    setupSliderListeners();
    setupEraDayListeners();
    renderEraColorPicker();
    document.addEventListener('click', handleActionClick);
    document.getElementById('detail-backdrop').addEventListener('click', () => closeDetailDrawer());
    document.getElementById('restore-file').addEventListener('change', handleRestoreFileChange);
    document.getElementById('set-apikey').addEventListener('input', () => {
        if (document.getElementById('set-apikey').value.trim()) {
            document.getElementById('set-apikey-clear').checked = false;
        }
    });
}

function handleActionClick(e) {
    const actionEl = e.target.closest('[data-action]');
    if (!actionEl) return;

    const { action } = actionEl.dataset;
    const id = actionEl.dataset.id ? parseInt(actionEl.dataset.id, 10) : null;

    switch (action) {
        case 'sign-in':
            beginSignIn();
            break;
        case 'sign-out':
            signOut();
            break;
        case 'open-era-modal':
            openEraModal();
            break;
        case 'open-event-modal':
            openEventModal();
            break;
        case 'open-export-modal':
            openExportModal();
            break;
        case 'toggle-settings':
            toggleSettings();
            break;
        case 'close-event-modal':
            closeEventModal();
            break;
        case 'save-event':
            saveEvent();
            break;
        case 'start-reflection':
            startReflection();
            break;
        case 'skip-question':
            skipQuestion();
            break;
        case 'next-question':
            nextQuestion();
            break;
        case 'save-with-reflection':
            saveWithReflection();
            break;
        case 'back-to-form':
            backToForm();
            break;
        case 'close-era-modal':
            closeEraModal();
            break;
        case 'save-era':
            saveEra();
            break;
        case 'close-export-modal':
            closeExportModal();
            break;
        case 'do-export':
            doExport();
            break;
        case 'test-connection':
            testConnection();
            break;
        case 'download-backup':
            downloadBackup();
            break;
        case 'restore-timeline':
            restoreTimeline();
            break;
        case 'save-settings':
            saveSettings();
            break;
        case 'set-arrangement-mode':
            setArrangementMode(actionEl.dataset.mode);
            break;
        case 'set-display-order':
            setDisplayOrder(actionEl.dataset.order);
            break;
        case 'close-detail-drawer':
            closeDetailDrawer();
            break;
        case 'confirm-delete-event':
            if (id != null) doDelete(id);
            break;
        case 'cancel-delete-event':
            if (id != null) cancelDeleteEvent(id);
            break;
        case 'edit-event':
            if (id != null) editEvent(id);
            break;
        case 're-reflect':
            if (id != null) reReflect(id);
            break;
        case 'start-delete-event':
            if (id != null) startDeleteConfirm(id);
            break;
        case 'start-delete-era':
            if (id != null) deleteEra(id);
            break;
        case 'confirm-delete-era':
            if (id != null) doDeleteEra(id);
            break;
        case 'cancel-delete-era':
            cancelDeleteEra();
            break;
        case 'select-era-color':
            if (actionEl.dataset.color) selectEraColor(actionEl.dataset.color);
            break;
        default:
            break;
    }
}

function getCookie(name) {
    const encoded = `${encodeURIComponent(name)}=`;
    const match = document.cookie.split('; ').find((part) => part.startsWith(encoded));
    return match ? decodeURIComponent(match.slice(encoded.length)) : '';
}

function renderAuthShell() {
    const overlay = document.getElementById('auth-overlay');
    const message = document.getElementById('auth-overlay-message');
    const userLabel = document.getElementById('auth-user-label');
    const signOutBtn = document.getElementById('btn-signout');
    const authenticated = state.auth.status === 'authenticated';

    document.body.classList.toggle('auth-locked', !authenticated);
    overlay.classList.toggle('hidden', authenticated);
    message.textContent = state.auth.message || 'Sign in to access your private timeline.';

    if (authenticated && state.auth.user) {
        userLabel.textContent = state.auth.user.display_name || state.auth.user.email || '';
    } else {
        userLabel.textContent = '';
    }

    userLabel.classList.toggle('hidden', !authenticated);
    signOutBtn.classList.toggle('hidden', !authenticated);
}

function resetUiForLogout() {
    closeEventModal();
    closeEraModal();
    closeExportModal();
    closeDetailDrawer();

    const settingsPanel = document.getElementById('settings-panel');
    const settingsBackdrop = document.getElementById('settings-backdrop');
    settingsPanel.classList.remove('open');
    settingsBackdrop.classList.add('hidden');

    state.events = [];
    state.eras = [];
    state.selectedId = null;
    state.deleteConfirmId = null;
    state.editingId = null;
    state.reflectionData = { questions: [], answers: [], currentQ: 0 };
    state.newEventId = null;
    state.draggedEventId = null;
    state.reorderPending = false;
    deleteEraConfirm = null;
}

function handleLoggedOut(message = 'Sign in to access your private timeline.') {
    state.auth = {
        status: 'logged_out',
        user: null,
        message,
    };
    resetUiForLogout();
    renderAuthShell();
    renderTimeline();
}

function beginSignIn() {
    const next = `${window.location.pathname}${window.location.search}`;
    window.location.assign(`/auth/login?next=${encodeURIComponent(next)}`);
}

async function signOut() {
    try {
        await api('/auth/logout', { method: 'POST', handle401: false });
    } catch (e) {
        // Clear the local shell even if the server-side session is already gone.
    } finally {
        handleLoggedOut('You have been signed out.');
    }
}

// ── Timeline utilities ──────────────────────────────────────────────────────────

function sentimentColor(score) {
    const neg = { r: 56, g: 83, b: 70 };
    const neu = { r: 143, g: 130, b: 109 };
    const pos = { r: 201, g: 107, b: 61 };
    let t, from, to;
    if (score <= 0) {
        t = (score + 5) / 5;
        from = neg;
        to = neu;
    } else {
        t = score / 5;
        from = neu;
        to = pos;
    }
    const r = Math.round(from.r + (to.r - from.r) * t);
    const g = Math.round(from.g + (to.g - from.g) * t);
    const b = Math.round(from.b + (to.b - from.b) * t);
    return `rgb(${r},${g},${b})`;
}

function themeValue(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function eventTone(score) {
    if (score <= -4) return 'Deeply difficult';
    if (score <= -2) return 'Tender to revisit';
    if (score < 0) return 'Mixed with ache';
    if (score === 0) return 'Held in balance';
    if (score <= 2) return 'Warm and steady';
    if (score <= 4) return 'Bright and formative';
    return 'Luminous and lasting';
}

function precisionRank(precision) {
    return { day: 0, month: 1, year: 2 }[precision] ?? 3;
}

function sortEventsChronologically(events) {
    return [...events].sort((a, b) => {
        const dateDelta = new Date(a.date + 'T00:00:00') - new Date(b.date + 'T00:00:00');
        if (dateDelta !== 0) return dateDelta;
        const precisionDelta = precisionRank(a.date_precision) - precisionRank(b.date_precision);
        if (precisionDelta !== 0) return precisionDelta;
        return a.id - b.id;
    });
}

function normalizeDisplayOrder(value) {
    return value === DISPLAY_ORDERS.DESCENDING
        ? DISPLAY_ORDERS.DESCENDING
        : DISPLAY_ORDERS.ASCENDING;
}

function normalizeArrangementMode(value) {
    return value === ARRANGEMENT_MODES.MANUAL
        ? ARRANGEMENT_MODES.MANUAL
        : ARRANGEMENT_MODES.CHRONOLOGICAL;
}

function loadDisplayOrderPreference() {
    try {
        return normalizeDisplayOrder(window.localStorage.getItem(DISPLAY_ORDER_STORAGE_KEY));
    } catch (e) {
        return DISPLAY_ORDERS.ASCENDING;
    }
}

function persistDisplayOrderPreference(order) {
    try {
        window.localStorage.setItem(DISPLAY_ORDER_STORAGE_KEY, normalizeDisplayOrder(order));
    } catch (e) {
        // Ignore storage failures and keep the in-memory preference.
    }
}

function loadArrangementModePreference() {
    try {
        return normalizeArrangementMode(window.localStorage.getItem(ARRANGEMENT_MODE_STORAGE_KEY));
    } catch (e) {
        return ARRANGEMENT_MODES.CHRONOLOGICAL;
    }
}

function persistArrangementModePreference(mode) {
    try {
        window.localStorage.setItem(ARRANGEMENT_MODE_STORAGE_KEY, normalizeArrangementMode(mode));
    } catch (e) {
        // Ignore storage failures and keep the in-memory preference.
    }
}

function isReverseChronological() {
    return state.displayOrder === DISPLAY_ORDERS.DESCENDING;
}

function isManualArrangement() {
    return state.arrangementMode === ARRANGEMENT_MODES.MANUAL;
}

function getOrderedEvents(events, order = state.displayOrder) {
    const ordered = sortEventsChronologically(events);
    if (normalizeDisplayOrder(order) === DISPLAY_ORDERS.DESCENDING) {
        ordered.reverse();
    }
    return ordered;
}

function getManualOrderedEvents(events) {
    return [...events].sort((a, b) => {
        const sortDelta = (a.sort_index ?? 0) - (b.sort_index ?? 0);
        if (sortDelta !== 0) return sortDelta;
        return a.id - b.id;
    });
}

function getVisibleEvents(events = state.events) {
    return isManualArrangement() ? getManualOrderedEvents(events) : getOrderedEvents(events);
}

function getEraById(id) {
    return id ? state.eras.find((era) => era.id === id) || null : null;
}

function getSelectedEvent() {
    return state.events.find((event) => event.id === state.selectedId) || null;
}

function getEventsForRange(range) {
    if (!range || range === 'all' || range === 'visible') return state.events;
    if (range.startsWith('era-')) {
        const eraId = parseInt(range.replace('era-', ''), 10);
        return state.events.filter((event) => event.era_id === eraId);
    }
    return state.events;
}

function buildYearGroups(events) {
    const groups = new Map();
    events.forEach((event) => {
        const year = new Date(event.date + 'T00:00:00').getFullYear();
        if (!groups.has(year)) groups.set(year, []);
        groups.get(year).push(event);
    });

    return Array.from(groups.entries()).map(([year, yearEvents]) => ({
        year,
        events: yearEvents,
    }));
}

function renderEraBreak(era) {
    const divider = el('div', 'timeline-era-break');
    divider.style.setProperty('--era-color', era.color_hex);
    divider.innerHTML = `
        <span class="timeline-era-break-banner">
            <span class="timeline-era-break-kicker">${isReverseChronological() ? 'Earlier era' : 'New era'}</span>
            <span class="timeline-era-break-name">${esc(era.name)}</span>
        </span>
    `;
    return divider;
}

function renderDisplayOrderControls() {
    const ascending = state.displayOrder === DISPLAY_ORDERS.ASCENDING;
    const chronological = !isManualArrangement();
    return `
        <div class="timeline-controls-bar">
            <div class="timeline-controls-group" role="group" aria-label="Timeline layout">
                <button type="button" class="timeline-control-btn${chronological ? ' is-active' : ''}"
                    data-action="set-arrangement-mode" data-mode="${ARRANGEMENT_MODES.CHRONOLOGICAL}" aria-pressed="${chronological}">By Date</button>
                <button type="button" class="timeline-control-btn${!chronological ? ' is-active' : ''}"
                    data-action="set-arrangement-mode" data-mode="${ARRANGEMENT_MODES.MANUAL}" aria-pressed="${!chronological}">Manual</button>
            </div>
            ${chronological ? `
                <span class="timeline-controls-sep" aria-hidden="true"></span>
                <div class="timeline-controls-group" role="group" aria-label="Display order">
                    <button type="button" class="timeline-control-btn${ascending ? ' is-active' : ''}"
                        data-action="set-display-order" data-order="${DISPLAY_ORDERS.ASCENDING}" aria-pressed="${ascending}">
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M6 9V3M6 3L3.5 5.5M6 3l2.5 2.5"/></svg>
                        Oldest first
                    </button>
                    <button type="button" class="timeline-control-btn${!ascending ? ' is-active' : ''}"
                        data-action="set-display-order" data-order="${DISPLAY_ORDERS.DESCENDING}" aria-pressed="${!ascending}">
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M6 3v6M6 9l-2.5-2.5M6 9l2.5-2.5"/></svg>
                        Newest first
                    </button>
                </div>
            ` : `
                <span class="timeline-controls-sep" aria-hidden="true"></span>
                <span class="timeline-controls-hint">Drag to reorder</span>
            `}
        </div>
    `;
}

function renderMemoryRow(event, options = {}) {
    const manual = Boolean(options.manual);
    const selected = state.selectedId === event.id;
    const era = getEraById(event.era_id);
    const rowDate = manual
        ? formatDate(event.date, event.date_precision)
        : formatTimelineRowDate(event.date, event.date_precision);
    const scoreText = formatSignedScore(event.sentiment_score);
    const scoreAria = `Emotional weight: ${SENTIMENT_LABELS[String(event.sentiment_score)]} (${scoreText})`;
    const metaBits = [];

    if (rowDate) {
        metaBits.push(`<span class="memory-row-date">${rowDate}</span>`);
    }
    if (manual && era) {
        metaBits.push(`<span class="memory-row-era">${esc(era.name)}</span>`);
    }

    const absScore = Math.abs(event.sentiment_score);
    const tier = absScore >= 4 ? 'intense' : absScore >= 2 ? 'moderate' : 'mild';

    const row = el('div', `memory-row${manual ? ' is-manual' : ''} sentiment-${tier}`);
    row.dataset.id = event.id;
    row.style.setProperty('--memory-color', sentimentColor(event.sentiment_score));
    row.style.setProperty('--era-color', era ? era.color_hex : 'transparent');
    if (manual) {
        row.draggable = !state.reorderPending;
        row.addEventListener('dragstart', onMemoryRowDragStart);
        row.addEventListener('dragover', onMemoryRowDragOver);
        row.addEventListener('drop', onMemoryRowDrop);
        row.addEventListener('dragend', onMemoryRowDragEnd);
        row.addEventListener('dragleave', onMemoryRowDragLeave);
    }

    if (selected) row.classList.add('is-selected');
    if (state.newEventId === event.id) {
        row.classList.add('just-added');
        setTimeout(() => { state.newEventId = null; }, 900);
    }

    row.innerHTML = `
        <span class="memory-row-rail" aria-hidden="true"></span>
        <button
            type="button"
            class="memory-row-main"
            aria-expanded="${selected ? 'true' : 'false'}"
            aria-pressed="${selected ? 'true' : 'false'}"
        >
            <span class="memory-row-content">
                <span class="memory-row-header">
                    <span class="memory-row-title">${esc(event.headline)}</span>
                    <span class="memory-row-score" aria-label="${esc(scoreAria)}">
                        <span class="memory-row-score-label">Emotional weight</span>
                        <span class="memory-row-score-value">${scoreText}</span>
                    </span>
                </span>
                ${metaBits.length ? `<span class="memory-row-meta">${metaBits.join('')}</span>` : ''}
                <span class="memory-row-arrow" aria-hidden="true"></span>
            </span>
        </button>
        ${manual ? `
            <button
                type="button"
                class="memory-row-handle"
                aria-label="Drag to reorder ${esc(event.headline)}"
                title="Drag to reorder"
            >
                <span aria-hidden="true">⋮⋮</span>
            </button>
        ` : ''}
    `;

    row.querySelector('.memory-row-main').addEventListener('click', () => selectEvent(event.id));
    return row;
}

function renderTimeline(eventsOverride = state.events) {
    const canvas = document.getElementById('timeline-canvas');
    const emptyEl = document.getElementById('empty-state');
    const timelineEvents = getVisibleEvents(eventsOverride);
    const yearGroups = isManualArrangement() ? [] : buildYearGroups(timelineEvents);
    const yearCount = buildYearGroups(sortEventsChronologically(eventsOverride)).length;

    canvas.innerHTML = '';

    if (state.selectedId && !state.events.some((event) => event.id === state.selectedId)) {
        state.selectedId = null;
        state.deleteConfirmId = null;
    }

    if (timelineEvents.length === 0) {
        emptyEl.classList.remove('hidden');
        canvas.style.width = '100%';
        canvas.style.minHeight = '100%';
        renderDetailDrawer();
        return;
    }

    emptyEl.classList.add('hidden');
    canvas.style.width = '';
    canvas.style.minHeight = '';

    const shell = el('div', 'timeline-shell');
    shell.dataset.flowDirection = !isManualArrangement() && isReverseChronological() ? 'up' : 'down';
    shell.innerHTML = `
        <div class="timeline-intro">
            <p class="timeline-summary">${timelineEvents.length} ${timelineEvents.length === 1 ? 'memory' : 'memories'} across ${yearCount} ${yearCount === 1 ? 'year' : 'years'}.</p>
            ${renderDisplayOrderControls()}
        </div>
    `;

    if (isManualArrangement()) {
        const section = el('section', 'timeline-manual');
        section.innerHTML = `
            <div class="timeline-manual-header">
                <span class="timeline-manual-kicker">Manual arrangement</span>
                <span class="timeline-manual-count">${timelineEvents.length} ${timelineEvents.length === 1 ? 'memory' : 'memories'}</span>
            </div>
        `;

        const list = el('div', 'timeline-manual-list');
        timelineEvents.forEach((event) => {
            list.appendChild(renderMemoryRow(event, { manual: true }));
        });

        section.appendChild(list);
        shell.appendChild(section);
    } else {
        let previousEraId = null;
        yearGroups.forEach(({ year, events }) => {
            const section = el('section', 'timeline-year');
            section.dataset.year = year;
            section.innerHTML = `
                <div class="timeline-year-header">
                    <span class="timeline-year-label">${year}</span>
                    <span class="timeline-year-count">${events.length} ${events.length === 1 ? 'memory' : 'memories'}</span>
                </div>
            `;

            const list = el('div', 'timeline-year-list');

            events.forEach((event) => {
                const era = getEraById(event.era_id);
                const startsNewEra = Boolean(event.era_id && event.era_id !== previousEraId);

                if (era && startsNewEra) {
                    list.appendChild(renderEraBreak(era));
                }

                list.appendChild(renderMemoryRow(event));
                previousEraId = event.era_id;
            });

            section.appendChild(list);
            shell.appendChild(section);
        });
    }

    canvas.appendChild(shell);
    renderDetailDrawer();
}

function setDisplayOrder(order) {
    const normalized = normalizeDisplayOrder(order);
    if (normalized === state.displayOrder) return;

    state.displayOrder = normalized;
    persistDisplayOrderPreference(normalized);

    const anchorEventId = state.selectedId;
    renderTimeline();

    if (anchorEventId) {
        scrollToEvent(anchorEventId);
    } else {
        scrollToPreferredAnchor();
    }
}

function setArrangementMode(mode) {
    const normalized = normalizeArrangementMode(mode);
    if (normalized === state.arrangementMode) return;

    state.arrangementMode = normalized;
    persistArrangementModePreference(normalized);

    renderTimeline();
    if (normalized === ARRANGEMENT_MODES.MANUAL) {
        document.getElementById('timeline-viewport').scrollTo({ top: 0, behavior: 'smooth' });
        return;
    }
    scrollToPreferredAnchor();
}

function renderDetailDrawer() {
    const drawer = document.getElementById('detail-drawer');
    const backdrop = document.getElementById('detail-backdrop');
    const event = getSelectedEvent();

    document.body.classList.toggle('detail-open', !!event);

    if (!event) {
        teardownFocusTrap(drawer);
        drawer.classList.remove('open');
        drawer.setAttribute('aria-hidden', 'true');
        backdrop.classList.add('hidden');
        backdrop.setAttribute('aria-hidden', 'true');
        drawer.innerHTML = '';
        if (_drawerReturnFocus) {
            _drawerReturnFocus.focus();
            _drawerReturnFocus = null;
        }
        return;
    }

    const era = getEraById(event.era_id);
    const detailText = (event.explanation || '').trim() || `${eventTone(event.sentiment_score)}. This memory is still waiting for a fuller reflection.`;
    const confirmDelete = state.deleteConfirmId === event.id;

    drawer.innerHTML = `
        <div class="timeline-detail-shell">
            <div class="timeline-detail-header">
                <div>
                    <p class="timeline-detail-kicker">${esc(eventTone(event.sentiment_score))}</p>
                    <h2 class="timeline-detail-title">${esc(event.headline)}</h2>
                </div>
                <button type="button" class="timeline-detail-close" data-action="close-detail-drawer" aria-label="Close memory details">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>
            <div class="timeline-detail-meta">
                <span class="timeline-detail-pill">${formatDateLong(event.date, event.date_precision)}</span>
                ${era ? `<span class="timeline-detail-pill timeline-detail-pill--era" style="--era-color:${era.color_hex}">${esc(era.name)}</span>` : ''}
                <span class="timeline-detail-pill timeline-detail-pill--sentiment" style="--memory-color:${sentimentColor(event.sentiment_score)}">${SENTIMENT_LABELS[String(event.sentiment_score)]} · ${event.sentiment_score > 0 ? '+' : ''}${event.sentiment_score}</span>
            </div>
            <div class="timeline-detail-body">
                <p>${esc(detailText).replace(/\n/g, '<br>')}</p>
            </div>
            <div class="timeline-detail-actions">
                ${confirmDelete ? `
                    <span class="timeline-detail-confirm">Delete this memory?</span>
                    <button type="button" class="timeline-action timeline-action--danger" data-action="confirm-delete-event" data-id="${event.id}">Yes, delete</button>
                    <button type="button" class="timeline-action" data-action="cancel-delete-event" data-id="${event.id}">Cancel</button>
                ` : `
                    <button type="button" class="timeline-action timeline-action--primary" data-action="edit-event" data-id="${event.id}">Edit</button>
                    <button type="button" class="timeline-action" data-action="re-reflect" data-id="${event.id}">Re-reflect</button>
                    <button type="button" class="timeline-action timeline-action--danger" data-action="start-delete-event" data-id="${event.id}">Delete</button>
                `}
            </div>
        </div>
    `;

    teardownFocusTrap(drawer);
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    backdrop.classList.remove('hidden');
    backdrop.setAttribute('aria-hidden', 'false');
    setupFocusTrap(drawer);

    if (_focusDrawerOnRender) {
        _focusDrawerOnRender = false;
        const focusTarget = drawer.querySelector('.timeline-detail-close') || getFocusable(drawer)[0];
        if (focusTarget) requestAnimationFrame(() => focusTarget.focus());
    }
}

// ── Selection ───────────────────────────────────────────────────────────────────

function selectEvent(id) {
    const isClosing = state.selectedId === id;
    if (isClosing) {
        closeDetailDrawer();
        return;
    }

    if (!state.selectedId) _drawerReturnFocus = document.activeElement;
    state.selectedId = id;
    state.deleteConfirmId = null;
    _focusDrawerOnRender = true;
    renderTimeline();
}

function closeDetailDrawer() {
    if (!state.selectedId) return;
    state.selectedId = null;
    state.deleteConfirmId = null;
    renderTimeline();
}

function deselectEvent() {
    closeDetailDrawer();
}

// ── Manual ordering ─────────────────────────────────────────────────────────────

function clearManualDropIndicators() {
    document.querySelectorAll('.memory-row.drop-before, .memory-row.drop-after')
        .forEach((row) => row.classList.remove('drop-before', 'drop-after'));
}

function reindexEvents(events) {
    return events.map((event, index) => ({ ...event, sort_index: index }));
}

function applyManualMove(events, draggedId, targetId, placeAfter) {
    if (draggedId === targetId) return null;

    const ordered = getManualOrderedEvents(events);
    const draggedIndex = ordered.findIndex((event) => event.id === draggedId);
    const targetIndex = ordered.findIndex((event) => event.id === targetId);

    if (draggedIndex === -1 || targetIndex === -1) return null;

    const [dragged] = ordered.splice(draggedIndex, 1);
    let insertionIndex = targetIndex;

    if (draggedIndex < targetIndex) {
        insertionIndex -= 1;
    }
    if (placeAfter) {
        insertionIndex += 1;
    }

    ordered.splice(Math.max(0, insertionIndex), 0, dragged);
    const reordered = reindexEvents(ordered);

    const hasChanged = reordered.some((event, index) => event.id !== getManualOrderedEvents(events)[index].id);
    return hasChanged ? reordered : null;
}

async function persistManualOrder() {
    const ids = getManualOrderedEvents(state.events).map((event) => event.id);
    return api('/api/events/reorder', {
        method: 'POST',
        body: { ids },
    });
}

function onMemoryRowDragStart(e) {
    if (!isManualArrangement() || state.reorderPending) {
        e.preventDefault();
        return;
    }

    state.draggedEventId = parseInt(e.currentTarget.dataset.id, 10);
    e.currentTarget.classList.add('is-dragging');
    if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(state.draggedEventId));
    }
}

function onMemoryRowDragOver(e) {
    if (!isManualArrangement() || state.reorderPending || state.draggedEventId == null) return;

    const targetRow = e.currentTarget;
    const targetId = parseInt(targetRow.dataset.id, 10);
    if (targetId === state.draggedEventId) return;

    e.preventDefault();
    clearManualDropIndicators();

    const bounds = targetRow.getBoundingClientRect();
    const placeAfter = e.clientY > bounds.top + (bounds.height / 2);
    targetRow.classList.add(placeAfter ? 'drop-after' : 'drop-before');
}

function onMemoryRowDragLeave(e) {
    if (!e.currentTarget.contains(e.relatedTarget)) {
        e.currentTarget.classList.remove('drop-before', 'drop-after');
    }
}

async function onMemoryRowDrop(e) {
    if (!isManualArrangement() || state.reorderPending || state.draggedEventId == null) return;

    e.preventDefault();

    const targetId = parseInt(e.currentTarget.dataset.id, 10);
    const draggedId = state.draggedEventId;
    const placeAfter = e.currentTarget.classList.contains('drop-after');
    const reordered = applyManualMove(state.events, draggedId, targetId, placeAfter);

    clearManualDropIndicators();

    if (!reordered) return;

    const previousEvents = state.events;
    state.events = reordered;
    state.reorderPending = true;
    renderTimeline();
    scrollToEvent(draggedId);

    try {
        state.events = await persistManualOrder();
        toast('Order updated');
    } catch (err) {
        state.events = previousEvents;
        toast(err.message || 'Could not save the new order');
    } finally {
        state.reorderPending = false;
        state.draggedEventId = null;
        renderTimeline();
    }
}

function onMemoryRowDragEnd(e) {
    e.currentTarget.classList.remove('is-dragging');
    clearManualDropIndicators();
    state.draggedEventId = null;
}

// ── Event CRUD ──────────────────────────────────────────────────────────────────

function openEventModal(editId = null) {
    if (state.auth.status !== 'authenticated') return;
    state.editingId = editId;
    const title = document.getElementById('modal-title');
    const reflectBtn = document.getElementById('btn-reflect');

    // Populate era dropdown
    const eraSelect = document.getElementById('evt-era');
    eraSelect.innerHTML = '<option value="">None</option>';
    state.eras.forEach(era => {
        const opt = document.createElement('option');
        opt.value = era.id;
        opt.textContent = era.name;
        eraSelect.appendChild(opt);
    });

    if (editId) {
        const ev = state.events.find(e => e.id === editId);
        title.textContent = 'Edit Event';
        reflectBtn.textContent = 'Re-reflect';
        document.getElementById('evt-headline').value = ev.headline;
        // Populate year/month/day from stored date
        const parts = ev.date.split('-');
        document.getElementById('evt-year').value = parseInt(parts[0]);
        // Check date_precision to determine which fields were originally set
        const precision = ev.date_precision || 'day';
        if (precision === 'year') {
            document.getElementById('evt-month').value = '';
            document.getElementById('evt-day').value = '';
        } else if (precision === 'month') {
            document.getElementById('evt-month').value = parseInt(parts[1]);
            document.getElementById('evt-day').value = '';
        } else {
            document.getElementById('evt-month').value = parseInt(parts[1]);
            populateDays();
            document.getElementById('evt-day').value = parseInt(parts[2]);
        }
        document.getElementById('evt-sentiment').value = ev.sentiment_score;
        document.getElementById('evt-explanation').value = ev.explanation || '';
        document.getElementById('evt-era').value = ev.era_id || '';
        updateSentimentLabel(ev.sentiment_score);
    } else {
        title.textContent = 'New Event';
        reflectBtn.textContent = 'Reflect with AI';
        document.getElementById('evt-headline').value = '';
        document.getElementById('evt-year').value = new Date().getFullYear();
        document.getElementById('evt-month').value = '';
        document.getElementById('evt-day').value = '';
        document.getElementById('evt-sentiment').value = 0;
        document.getElementById('evt-explanation').value = '';
        document.getElementById('evt-era').value = '';
        updateSentimentLabel(0);
    }
    populateDays();
    updateHeadlineCount();
    showStep('step-form');
    openModal(document.getElementById('event-modal'), document.getElementById('evt-headline'));
}

function closeEventModal() {
    closeModal(document.getElementById('event-modal'));
    state.editingId = null;
    state.reflectionData = { questions: [], answers: [], currentQ: 0 };
}

function showStep(id) {
    ['step-form', 'step-loading', 'step-question', 'step-synthesizing', 'step-review']
        .forEach(s => document.getElementById(s).classList.toggle('hidden', s !== id));
}

function getFormData() {
    const year = document.getElementById('evt-year').value;
    const month = document.getElementById('evt-month').value;
    const day = document.getElementById('evt-day').value;

    // Build date string — default missing parts to 01
    const y = String(year).padStart(4, '0');
    const m = month ? String(month).padStart(2, '0') : '01';
    const d = day ? String(day).padStart(2, '0') : '01';

    // Track precision so we know how to display it
    let datePrecision = 'year';
    if (month) datePrecision = 'month';
    if (month && day) datePrecision = 'day';

    return {
        headline: document.getElementById('evt-headline').value.trim(),
        date: `${y}-${m}-${d}`,
        date_precision: datePrecision,
        sentiment_score: parseInt(document.getElementById('evt-sentiment').value),
        explanation: document.getElementById('evt-explanation').value.trim(),
        era_id: document.getElementById('evt-era').value ? parseInt(document.getElementById('evt-era').value) : null,
    };
}

async function saveEvent() {
    const data = getFormData();
    if (!data.headline) { toast('Please enter a headline'); return; }
    const year = parseInt(document.getElementById('evt-year').value);
    if (!year || year < 1 || year > 9999) { toast('Please enter a valid year (1–9999)'); return; }

    const saveBtn = document.getElementById('btn-save-event');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }

    const wasEditing = !!state.editingId;
    try {
        let result;
        if (state.editingId) {
            result = await api(`/api/events/${state.editingId}`, { method: 'PUT', body: data });
        } else {
            result = await api('/api/events', { method: 'POST', body: data });
            state.newEventId = result.id;
        }
        await loadEvents();
        state.selectedId = result.id;
        state.deleteConfirmId = null;
        closeEventModal();
        renderTimeline();
        scrollToEvent(result.id);
        toast(wasEditing ? 'Event updated' : 'Event saved');
    } catch (e) {
        console.error('Save failed:', e);
        toast(e.message || 'Failed to save event');
    } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
    }
}

function editEvent(id) {
    deselectEvent();
    openEventModal(id);
}

function startDeleteConfirm(id) {
    state.selectedId = id;
    state.deleteConfirmId = id;
    renderDetailDrawer();
    const confirmBtn = document.querySelector('.timeline-action--danger');
    if (confirmBtn) requestAnimationFrame(() => confirmBtn.focus());
}

async function doDelete(id) {
    try {
        await api(`/api/events/${id}`, { method: 'DELETE' });
        await loadEvents();
        state.selectedId = null;
        state.deleteConfirmId = null;
        renderTimeline();
        toast('Event deleted');
    } catch (e) {
        toast(e.message || 'Failed to delete event');
    }
}

function cancelDeleteEvent(id) {
    state.selectedId = id;
    state.deleteConfirmId = null;
    renderDetailDrawer();
}

function reReflect(id) {
    deselectEvent();
    openEventModal(id);
    setTimeout(() => startReflection(), 100);
}

// ── Reflection flow ─────────────────────────────────────────────────────────────

async function startReflection() {
    const data = getFormData();
    if (!data.headline) { toast('Please enter a headline first'); return; }

    showStep('step-loading');
    try {
        const result = await api('/reflect/probe', {
            method: 'POST',
            body: { headline: data.headline, date: data.date, sentiment_score: data.sentiment_score },
        });
        state.reflectionData = { questions: result.questions, answers: ['', '', ''], currentQ: 0 };
        showQuestion(0);
    } catch (e) {
        toast('Could not reach the AI. Check Settings.');
        showStep('step-form');
    }
}

function showQuestion(idx) {
    showStep('step-question');
    document.getElementById('q-counter').textContent = idx + 1;
    document.getElementById('q-text').textContent = state.reflectionData.questions[idx];
    document.getElementById('q-answer').value = state.reflectionData.answers[idx] || '';
    document.getElementById('q-answer').focus();
}

function skipQuestion() {
    state.reflectionData.answers[state.reflectionData.currentQ] = '';
    advanceQuestion();
}

function nextQuestion() {
    state.reflectionData.answers[state.reflectionData.currentQ] = document.getElementById('q-answer').value;
    advanceQuestion();
}

function advanceQuestion() {
    state.reflectionData.currentQ++;
    if (state.reflectionData.currentQ < 3) {
        showQuestion(state.reflectionData.currentQ);
    } else {
        synthesize();
    }
}

async function synthesize() {
    showStep('step-synthesizing');
    const data = getFormData();
    try {
        const result = await api('/reflect/synthesize', {
            method: 'POST',
            body: {
                headline: data.headline,
                date: data.date,
                sentiment_score: data.sentiment_score,
                questions: state.reflectionData.questions,
                answers: state.reflectionData.answers,
            },
        });
        document.getElementById('reflection-result').value = result.reflection;
        showStep('step-review');
    } catch (e) {
        toast('Synthesis failed. Check Settings.');
        showStep('step-form');
    }
}

async function saveWithReflection() {
    const data = getFormData();
    data.explanation = document.getElementById('reflection-result').value;
    data.reflection_qa = {
        questions: state.reflectionData.questions,
        answers: state.reflectionData.answers,
    };

    const saveBtn = document.getElementById('btn-save-reflection');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }

    const wasEditing = !!state.editingId;
    try {
        let result;
        if (state.editingId) {
            result = await api(`/api/events/${state.editingId}`, { method: 'PUT', body: data });
        } else {
            result = await api('/api/events', { method: 'POST', body: data });
            state.newEventId = result.id;
        }
        await loadEvents();
        state.selectedId = result.id;
        state.deleteConfirmId = null;
        closeEventModal();
        renderTimeline();
        if (result) scrollToEvent(result.id);
        toast(wasEditing ? 'Reflection updated' : 'Saved with reflection');
    } catch (e) {
        console.error('Save with reflection failed:', e);
        toast(e.message || 'Failed to save event');
    } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save Event'; }
    }
}

function backToForm() {
    showStep('step-form');
}

// ── Era management ──────────────────────────────────────────────────────────────

function openEraModal() {
    if (state.auth.status !== 'authenticated') return;
    renderEraList();
    renderEraColorPicker();
    openModal(document.getElementById('era-modal'));
}

function closeEraModal() {
    deleteEraConfirm = null;
    closeModal(document.getElementById('era-modal'));
}

function renderEraList() {
    const list = document.getElementById('era-list');
    if (state.eras.length === 0) {
        list.innerHTML = '<p class="text-sm text-ink-lighter italic">No eras yet.</p>';
        return;
    }
    list.innerHTML = state.eras.map(era => {
        const isConfirming = deleteEraConfirm === era.id;
        const deleteControls = isConfirming
            ? `<button type="button" class="era-item-delete" style="color:var(--status-danger);font-size:11px;padding:2px 6px;" data-action="confirm-delete-era" data-id="${era.id}" aria-label="Confirm delete ${esc(era.name)}">Delete?</button>
               <button type="button" class="era-item-delete" data-action="cancel-delete-era" aria-label="Cancel delete">
                   <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M6 18L18 6M6 6l12 12"/></svg>
               </button>`
            : `<button type="button" class="era-item-delete" data-action="start-delete-era" data-id="${era.id}" aria-label="Delete era ${esc(era.name)}">
                   <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M6 18L18 6M6 6l12 12"/></svg>
               </button>`;
        return `
            <div class="era-item">
                <div class="era-item-color" style="background:${era.color_hex}" aria-hidden="true"></div>
                <div class="era-item-name">${esc(era.name)}</div>
                <div class="era-item-dates">${formatDate(era.start_date, era.start_date_precision)} &ndash; ${formatDate(era.end_date, era.end_date_precision)}</div>
                ${deleteControls}
            </div>
        `;
    }).join('');
}

function renderEraColorPicker() {
    const picker = document.getElementById('era-color-picker');
    if (!picker) return;
    picker.setAttribute('role', 'radiogroup');
    picker.setAttribute('aria-label', 'Era color');
    picker.innerHTML = ERA_COLORS.map(c => {
        const name = ERA_COLOR_NAMES[c] || c;
        const selected = c === selectedEraColor;
        return `<button type="button" class="era-swatch${selected ? ' selected' : ''}"
            style="background:${c}"
            data-action="select-era-color"
            data-color="${c}"
            role="radio"
            aria-checked="${selected}"
            aria-label="${name}"></button>`;
    }).join('');
}

function selectEraColor(color) {
    selectedEraColor = color;
    renderEraColorPicker();
}

async function saveEra() {
    const name = document.getElementById('era-name').value.trim();
    const startYear = document.getElementById('era-start-year').value;
    const startMonth = document.getElementById('era-start-month').value;
    const startDay = document.getElementById('era-start-day').value;
    const endYear = document.getElementById('era-end-year').value;
    const endMonth = document.getElementById('era-end-month').value;
    const endDay = document.getElementById('era-end-day').value;

    if (!name || !startYear || !endYear) { toast('Enter a name and start/end years'); return; }

    const sy = String(startYear).padStart(4, '0');
    const sm = startMonth ? String(startMonth).padStart(2, '0') : '01';
    const sd = startDay ? String(startDay).padStart(2, '0') : '01';
    const ey = String(endYear).padStart(4, '0');
    const em = endMonth ? String(endMonth).padStart(2, '0') : '01';
    const ed = endDay ? String(endDay).padStart(2, '0') : '01';

    let startPrecision = 'year';
    if (startMonth) startPrecision = 'month';
    if (startMonth && startDay) startPrecision = 'day';
    let endPrecision = 'year';
    if (endMonth) endPrecision = 'month';
    if (endMonth && endDay) endPrecision = 'day';

    await api('/api/eras', {
        method: 'POST',
        body: {
            name,
            start_date: `${sy}-${sm}-${sd}`,
            end_date: `${ey}-${em}-${ed}`,
            start_date_precision: startPrecision,
            end_date_precision: endPrecision,
            color_hex: selectedEraColor,
        },
    });
    document.getElementById('era-name').value = '';
    document.getElementById('era-start-year').value = '';
    document.getElementById('era-start-month').value = '';
    document.getElementById('era-start-day').value = '';
    document.getElementById('era-end-year').value = '';
    document.getElementById('era-end-month').value = '';
    document.getElementById('era-end-day').value = '';
    await loadEras();
    renderEraList();
    renderTimeline();
    toast('Era created');
}

function setupEraDayListeners() {
    // Populate day dropdowns when year/month change for era Start
    document.getElementById('era-start-year').addEventListener('change', () => populateEraDays('start'));
    document.getElementById('era-start-month').addEventListener('change', () => populateEraDays('start'));
    // Same for era End
    document.getElementById('era-end-year').addEventListener('change', () => populateEraDays('end'));
    document.getElementById('era-end-month').addEventListener('change', () => populateEraDays('end'));
}

function populateEraDays(which) {
    const daySelect = document.getElementById(`era-${which}-day`);
    const currentDay = daySelect.value;
    const year = parseInt(document.getElementById(`era-${which}-year`).value) || 2000;
    const month = parseInt(document.getElementById(`era-${which}-month`).value);

    daySelect.innerHTML = '<option value="">\u2014</option>';

    if (!month) {
        daySelect.value = '';
        return;
    }

    const daysInMonth = new Date(year, month, 0).getDate();
    for (let d = 1; d <= daysInMonth; d++) {
        const opt = document.createElement('option');
        opt.value = d;
        opt.textContent = d;
        daySelect.appendChild(opt);
    }

    if (currentDay && parseInt(currentDay) <= daysInMonth) {
        daySelect.value = currentDay;
    }
}

function deleteEra(id) {
    deleteEraConfirm = id;
    renderEraList();
    setTimeout(() => {
        if (deleteEraConfirm === id) { deleteEraConfirm = null; renderEraList(); }
    }, 3000);
}

async function doDeleteEra(id) {
    deleteEraConfirm = null;
    try {
        await api(`/api/eras/${id}`, { method: 'DELETE' });
        await loadEras();
        renderEraList();
        renderTimeline();
    } catch (e) {
        toast(e.message || 'Failed to delete era');
        renderEraList();
    }
}

function cancelDeleteEra() {
    deleteEraConfirm = null;
    renderEraList();
}

// ── Export ───────────────────────────────────────────────────────────────────────

function openExportModal() {
    if (state.auth.status !== 'authenticated') return;
    const select = document.getElementById('export-range');
    // Remove old era options
    while (select.options.length > 2) select.remove(2);
    state.eras.forEach(era => {
        const opt = document.createElement('option');
        opt.value = 'era-' + era.id;
        opt.textContent = era.name;
        select.appendChild(opt);
    });
    openModal(document.getElementById('export-modal'));
}

function closeExportModal() {
    closeModal(document.getElementById('export-modal'));
}

async function doExport() {
    const title = document.getElementById('export-title').value || 'My Chronoscape';
    const range = document.getElementById('export-range').value;
    const btn = document.getElementById('btn-export-go');
    btn.textContent = 'Exporting...';
    btn.disabled = true;

    closeExportModal();
    const viewport = document.getElementById('timeline-viewport');
    const canvas = document.getElementById('timeline-canvas');
    const previousSelectedId = state.selectedId;
    const previousDeleteConfirmId = state.deleteConfirmId;
    const exportEvents = getEventsForRange(range);

    // Save originals
    const saved = {
        vpOverflow: viewport.style.overflow,
        vpOverflowX: viewport.style.overflowX,
        vpOverflowY: viewport.style.overflowY,
        vpPosition: viewport.style.position,
        vpTop: viewport.style.top,
        vpInset: viewport.style.inset,
        canvasWidth: canvas.style.width,
        canvasMinHeight: canvas.style.minHeight,
    };

    try {
        state.selectedId = null;
        state.deleteConfirmId = null;
        renderTimeline(exportEvents);
        prepareForExport(viewport, canvas, range);

        // Add footer
        const footer = document.createElement('div');
        footer.className = 'export-footer';
        const dateRange = getDateRange();
        footer.innerHTML = `
            <div>
                <div class="export-footer-title">${esc(title)}</div>
                <div class="export-footer-meta">${dateRange} &middot; ${state.events.length} event${state.events.length !== 1 ? 's' : ''}</div>
            </div>
            <div class="export-footer-wordmark">Chronoscape</div>
        `;
        canvas.appendChild(footer);

        void canvas.offsetWidth; // force reflow

        const captureTarget = range === 'visible' ? viewport : canvas;
        const captureWidth = captureTarget.scrollWidth || captureTarget.clientWidth;
        const captureHeight = captureTarget.scrollHeight || captureTarget.clientHeight;

        const img = await html2canvas(captureTarget, {
            scale: 2,
            backgroundColor: themeValue('--canvas'),
            scrollX: 0,
            scrollY: 0,
            windowWidth: captureWidth,
            windowHeight: captureHeight,
            useCORS: true,
        });

        canvas.removeChild(footer);

        const link = document.createElement('a');
        link.download = `chronoscape-${new Date().toISOString().split('T')[0]}.png`;
        link.href = img.toDataURL('image/png');
        link.click();
    } catch (e) {
        console.error('Export failed:', e);
        toast('Export failed');
    } finally {
        restoreAfterExport(viewport, canvas, saved);
        state.selectedId = previousSelectedId;
        state.deleteConfirmId = previousDeleteConfirmId;
        renderTimeline();
        btn.textContent = 'Export as PNG';
        btn.disabled = false;
    }
}

function prepareForExport(viewport, canvas, range) {
    document.body.classList.add('exporting');
    viewport.style.overflow = 'visible';
    viewport.style.overflowX = 'visible';
    viewport.style.overflowY = 'visible';
    viewport.style.position = 'static';
    viewport.style.top = 'auto';
    viewport.style.inset = 'auto';

    if (range !== 'visible') {
        canvas.style.width = `${canvas.scrollWidth}px`;
        canvas.style.minHeight = `${canvas.scrollHeight}px`;
    }
}

function restoreAfterExport(viewport, canvas, saved) {
    document.body.classList.remove('exporting');
    viewport.style.overflow = saved.vpOverflow;
    viewport.style.overflowX = saved.vpOverflowX;
    viewport.style.overflowY = saved.vpOverflowY;
    viewport.style.position = saved.vpPosition;
    viewport.style.top = saved.vpTop;
    viewport.style.inset = saved.vpInset;
    canvas.style.width = saved.canvasWidth;
    canvas.style.minHeight = saved.canvasMinHeight;
}

function getDateRange() {
    if (state.events.length === 0) return '';
    const dates = state.events.map(e => e.date).sort();
    return formatDate(dates[0]) + ' \u2013 ' + formatDate(dates[dates.length - 1]);
}

// ── Settings ────────────────────────────────────────────────────────────────────

function toggleSettings() {
    if (state.auth.status !== 'authenticated') return;
    const panel = document.getElementById('settings-panel');
    const backdrop = document.getElementById('settings-backdrop');
    const isOpen = panel.classList.contains('open');

    if (isOpen) {
        panel.classList.remove('open');
        backdrop.classList.add('hidden');
    } else {
        loadSettingsUI();
        panel.classList.add('open');
        backdrop.classList.remove('hidden');
    }
}

function renderApiKeyStatus(settings) {
    const status = document.getElementById('set-apikey-status');
    if (!settings.llm_api_key_set) {
        status.textContent = 'No API key saved.';
        return;
    }

    if (settings.llm_api_key_masked) {
        status.textContent = `Saved key on file: ${settings.llm_api_key_masked}`;
        return;
    }

    status.textContent = 'An API key is saved.';
}

async function loadSettingsUI() {
    try {
        const s = await api('/api/settings');
        document.getElementById('set-url').value = s.llm_base_url;
        document.getElementById('set-model').value = s.llm_model;
        document.getElementById('set-apikey').value = '';
        document.getElementById('set-apikey-clear').checked = false;
        renderApiKeyStatus(s);
    } catch (e) { /* ignore */ }
    resetRestoreUI(true);
}

async function testConnection() {
    const url = document.getElementById('set-url').value;
    const model = document.getElementById('set-model').value;
    const el = document.getElementById('connection-result');
    const btn = document.getElementById('btn-test');

    el.classList.remove('hidden');
    el.textContent = 'Testing...';
    el.style.color = themeValue('--status-neutral');
    btn.disabled = true;

    try {
        const r = await api('/health/llm', {
            method: 'POST',
            body: { llm_base_url: url, llm_model: model },
        });
        if (r.status === 'ok') {
            if (r.model_available) {
                el.textContent = `Connected. "${model}" is available.`;
                el.style.color = themeValue('--status-success');
            } else {
                el.textContent = `Connected, but "${model}" not found. Available: ${r.available_models.join(', ')}`;
                el.style.color = themeValue('--status-warning');
            }
        } else {
            el.textContent = `Error: ${r.error}`;
            el.style.color = themeValue('--status-danger');
        }
    } catch (e) {
        el.textContent = 'Connection failed';
        el.style.color = themeValue('--status-danger');
    } finally {
        btn.disabled = false;
    }
}

async function saveSettings() {
    const payload = {
        llm_base_url: document.getElementById('set-url').value,
        llm_model: document.getElementById('set-model').value,
        clear_llm_api_key: document.getElementById('set-apikey-clear').checked,
    };
    const newKey = document.getElementById('set-apikey').value.trim();
    if (newKey) {
        payload.llm_api_key = newKey;
        payload.clear_llm_api_key = false;
    }

    const saved = await api('/api/settings', {
        method: 'PUT',
        body: payload,
    });
    document.getElementById('set-apikey').value = '';
    document.getElementById('set-apikey-clear').checked = false;
    renderApiKeyStatus(saved);
    toast('Settings saved');
    toggleSettings();
}

async function downloadBackup() {
    const format = document.getElementById('backup-format').value;
    const btn = document.getElementById('btn-backup-download');
    btn.disabled = true;
    btn.textContent = 'Preparing…';

    try {
        const res = await api(`/api/backup?format=${encodeURIComponent(format)}`, { rawResponse: true });

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        const disposition = res.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="([^"]+)"/);
        link.href = url;
        link.download = match ? match[1] : `chronoscape-backup.${format}`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        toast(`Backup downloaded as ${format.toUpperCase()}`);
    } catch (e) {
        toast(e.message || 'Backup failed');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Download Backup';
    }
}

function handleRestoreFileChange() {
    const input = document.getElementById('restore-file');
    const label = document.getElementById('restore-file-name');
    const btn = document.getElementById('btn-restore-go');
    const file = input.files && input.files[0];

    label.textContent = file ? file.name : 'No backup selected.';
    btn.disabled = !file;
    clearRestoreStatus();
}

function clearRestoreStatus() {
    const status = document.getElementById('restore-status');
    status.classList.add('hidden');
    status.textContent = '';
    status.style.color = '';
}

function setRestoreStatus(message, tone = 'neutral') {
    const status = document.getElementById('restore-status');
    const colors = {
        neutral: themeValue('--status-neutral'),
        success: themeValue('--status-success'),
        error: themeValue('--status-danger'),
    };
    status.classList.remove('hidden');
    status.textContent = message;
    status.style.color = colors[tone] || colors.neutral;
}

function resetRestoreUI(clearFile = false) {
    const input = document.getElementById('restore-file');
    const label = document.getElementById('restore-file-name');
    const btn = document.getElementById('btn-restore-go');

    if (clearFile) input.value = '';
    label.textContent = 'No backup selected.';
    btn.disabled = true;
    btn.textContent = 'Restore Timeline';
    clearRestoreStatus();
}

async function restoreTimeline() {
    const input = document.getElementById('restore-file');
    const file = input.files && input.files[0];
    const btn = document.getElementById('btn-restore-go');

    if (!file) {
        toast('Choose a backup file first');
        return;
    }

    const confirmed = window.confirm(
        'Restore will replace all current events and eras with the contents of this backup. Continue?'
    );
    if (!confirmed) return;

    const formData = new FormData();
    formData.append('file', file);

    btn.disabled = true;
    btn.textContent = 'Restoring…';
    setRestoreStatus('Restoring backup…', 'neutral');

    try {
        const result = await api('/api/restore', { method: 'POST', body: formData });
        await Promise.all([loadEvents(), loadEras()]);
        state.selectedId = null;
        state.deleteConfirmId = null;
        renderTimeline();

        if (state.events.length > 0) {
            scrollToPreferredAnchor();
        } else {
            document.getElementById('timeline-viewport').scrollTo({ top: 0, behavior: 'smooth' });
        }

        resetRestoreUI(true);
        setRestoreStatus(
            `Restore complete. ${result.events_restored} ${result.events_restored === 1 ? 'event' : 'events'} and ${result.eras_restored} ${result.eras_restored === 1 ? 'era' : 'eras'} loaded.`,
            'success'
        );
        toast('Timeline restored');
    } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Restore Timeline';
        setRestoreStatus(e.message || 'Restore failed', 'error');
        toast(e.message || 'Restore failed');
    }
}

// ── Keyboard shortcuts ──────────────────────────────────────────────────────────

function setupKeyboard() {
    document.addEventListener('keydown', (e) => {
        const tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (state.auth.status !== 'authenticated') return;

        switch (e.key) {
            case 'n': case 'N': e.preventDefault(); openEventModal(); break;
            case 'e': case 'E': e.preventDefault(); openExportModal(); break;
            case 'Escape': closeAll(); break;
        }
    });
}

function closeAll() {
    closeEventModal();
    closeEraModal();
    closeExportModal();
    const panel = document.getElementById('settings-panel');
    if (panel.classList.contains('open')) toggleSettings();
    deselectEvent();
}

// ── Scroll helpers ──────────────────────────────────────────────────────────────

function getPreferredAnchorEventId() {
    if (state.events.length === 0) return null;
    if (isManualArrangement()) return getManualOrderedEvents(state.events)[0]?.id || null;
    const chronologicallySorted = sortEventsChronologically(state.events);
    return chronologicallySorted[chronologicallySorted.length - 1].id;
}

function scrollToPreferredAnchor() {
    const viewport = document.getElementById('timeline-viewport');
    if (isManualArrangement()) {
        viewport.scrollTo({ top: 0, behavior: 'smooth' });
        return;
    }
    if (isReverseChronological()) {
        viewport.scrollTo({ top: 0, behavior: 'smooth' });
        return;
    }

    const latestId = getPreferredAnchorEventId();
    if (latestId) {
        scrollToEvent(latestId);
    }
}

function scrollToEvent(id) {
    requestAnimationFrame(() => {
        const entry = document.querySelector(`.memory-row[data-id="${id}"]`);
        if (!entry) return;
        const viewport = document.getElementById('timeline-viewport');
        const viewportRect = viewport.getBoundingClientRect();
        const entryRect = entry.getBoundingClientRect();
        const offset = entryRect.top - viewportRect.top + viewport.scrollTop - (viewport.clientHeight * 0.32);
        viewport.scrollTo({ top: Math.max(0, offset), behavior: 'smooth' });
    });
}

// ── UI helpers ──────────────────────────────────────────────────────────────────

function setupSliderListeners() {
    const slider = document.getElementById('evt-sentiment');
    slider.addEventListener('input', () => updateSentimentLabel(parseInt(slider.value)));
    document.getElementById('evt-headline').addEventListener('input', updateHeadlineCount);

    // Re-populate day options when year or month change
    document.getElementById('evt-year').addEventListener('change', populateDays);
    document.getElementById('evt-month').addEventListener('change', populateDays);
}

function populateDays() {
    const daySelect = document.getElementById('evt-day');
    const currentDay = daySelect.value;
    const year = parseInt(document.getElementById('evt-year').value) || 2000;
    const month = parseInt(document.getElementById('evt-month').value);

    // Reset
    daySelect.innerHTML = '<option value="">\u2014</option>';

    if (!month) {
        // If no month, clear day as well
        daySelect.value = '';
        return;
    }

    // Figure out how many days in this month
    const daysInMonth = new Date(year, month, 0).getDate();
    for (let d = 1; d <= daysInMonth; d++) {
        const opt = document.createElement('option');
        opt.value = d;
        opt.textContent = d;
        daySelect.appendChild(opt);
    }

    // Restore previous selection if still valid
    if (currentDay && parseInt(currentDay) <= daysInMonth) {
        daySelect.value = currentDay;
    }
}

function updateSentimentLabel(val) {
    const sign = val > 0 ? '+' : '';
    const text = `${SENTIMENT_LABELS[String(val)]} (${sign}${val})`;
    document.getElementById('sentiment-label').textContent = text;
    const slider = document.getElementById('evt-sentiment');
    if (slider) {
        slider.setAttribute('aria-valuenow', val);
        slider.setAttribute('aria-valuetext', text);
    }
}

function updateHeadlineCount() {
    const len = document.getElementById('evt-headline').value.length;
    document.getElementById('headline-count').textContent = `${len} / 120`;
}

function formatSignedScore(score) {
    return `${score > 0 ? '+' : ''}${score}`;
}

function formatTimelineRowDate(dateStr, precision) {
    const d = new Date(dateStr + 'T00:00:00');
    if (precision === 'year') return '';
    if (precision === 'month') return d.toLocaleDateString('en-US', { month: 'short' });
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function formatDate(dateStr, precision) {
    const d = new Date(dateStr + 'T00:00:00');
    if (precision === 'year') return String(d.getFullYear());
    if (precision === 'month') return d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatDateLong(dateStr, precision) {
    const d = new Date(dateStr + 'T00:00:00');
    if (precision === 'year') return String(d.getFullYear());
    if (precision === 'month') return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
    return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
}

function esc(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function el(tag, className) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    return e;
}

let toastTimer;
function toast(msg) {
    let t = document.querySelector('.toast');
    if (!t) {
        t = document.createElement('div');
        t.className = 'toast';
        t.setAttribute('role', 'status');
        t.setAttribute('aria-live', 'polite');
        t.setAttribute('aria-atomic', 'true');
        document.body.appendChild(t);
    }
    clearTimeout(toastTimer);
    t.textContent = msg;
    requestAnimationFrame(() => t.classList.add('show'));
    toastTimer = setTimeout(() => t.classList.remove('show'), 2500);
}

// ── Focus trap utilities ─────────────────────────────────────────────────────────

function getFocusable(container) {
    return Array.from(container.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
    )).filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
}

function setupFocusTrap(container) {
    container._focusTrapHandler = (e) => {
        if (e.key !== 'Tab') return;
        const els = getFocusable(container);
        if (!els.length) return;
        const first = els[0], last = els[els.length - 1];
        if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
        } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
    };
    container.addEventListener('keydown', container._focusTrapHandler);
}

function teardownFocusTrap(container) {
    if (container._focusTrapHandler) {
        container.removeEventListener('keydown', container._focusTrapHandler);
        delete container._focusTrapHandler;
    }
}

function openModal(modalEl, autoFocusEl) {
    _returnFocus = document.activeElement;
    modalEl.classList.remove('hidden');
    setupFocusTrap(modalEl);
    const target = autoFocusEl || getFocusable(modalEl)[0];
    if (target) requestAnimationFrame(() => target.focus());
}

function closeModal(modalEl) {
    teardownFocusTrap(modalEl);
    modalEl.classList.add('hidden');
    if (_returnFocus) { _returnFocus.focus(); _returnFocus = null; }
}

// ── Resize handler ──────────────────────────────────────────────────────────────

let resizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderTimeline(), 150);
});
