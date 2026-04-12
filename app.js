// ── State ───────────────────────────────────────────────────────────────────────

const state = {
    events: [],
    eras: [],
    zoom: 1,
    selectedId: null,
    editingId: null,
    reflectionData: { questions: [], answers: [], currentQ: 0 },
    newEventId: null,
};

const ZOOM_LEVELS = [
    { label: 'Decade', pxPerYear: 80 },
    { label: 'Year',   pxPerYear: 300 },
    { label: 'Season', pxPerYear: 600 },
    { label: 'Month',  pxPerYear: 1400 },
];

const ERA_COLORS = [
    '#B8C4D4', '#C4B8A8', '#A8C4B8', '#C4A8B8',
    '#B8B8C4', '#C4C4A8', '#A8B8C4', '#C4B8B8',
];

const SENTIMENT_LABELS = {
    '-5': 'Deeply painful',  '-4': 'Very difficult',     '-3': 'Difficult',
    '-2': 'Somewhat difficult', '-1': 'Slightly negative', '0': 'Neutral',
    '1': 'Slightly positive', '2': 'Somewhat positive',  '3': 'Good',
    '4': 'Very good',        '5': 'Profoundly meaningful',
};

let selectedEraColor = ERA_COLORS[0];

// ── Init ────────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', init);

async function init() {
    await Promise.all([loadEvents(), loadEras()]);
    renderTimeline();
    scrollToLatest();
    setupKeyboard();
    setupSliderListeners();
    renderEraColorPicker();

    document.getElementById('timeline-canvas').addEventListener('click', (e) => {
        if (!e.target.closest('.event-node')) deselectEvent();
    });

    document.getElementById('timeline-viewport').addEventListener('scroll', () => {
        if (state.selectedId) deselectEvent();
    });
}

// ── API helpers ─────────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
    const fetchOpts = { ...opts };
    if (opts.body) {
        fetchOpts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
        fetchOpts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, fetchOpts);
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || 'Request failed');
    }
    if (res.status === 204) return null;
    return res.json();
}

async function loadEvents() {
    state.events = await api('/api/events');
}

async function loadEras() {
    state.eras = await api('/api/eras');
}

// ── Color & sizing utilities ────────────────────────────────────────────────────

function sentimentColor(score) {
    const neg = { r: 99, g: 102, b: 160 };  // #6366A0
    const neu = { r: 168, g: 159, b: 145 };  // #A89F91
    const pos = { r: 196, g: 151, b: 59 };   // #C4973B
    let t, from, to;
    if (score <= 0) {
        t = (score + 5) / 5;
        from = neg; to = neu;
    } else {
        t = score / 5;
        from = neu; to = pos;
    }
    const r = Math.round(from.r + (to.r - from.r) * t);
    const g = Math.round(from.g + (to.g - from.g) * t);
    const b = Math.round(from.b + (to.b - from.b) * t);
    return `rgb(${r},${g},${b})`;
}

function nodeSize(score) {
    return 12 + (Math.abs(score) / 5) * 12;
}

// ── Timeline rendering ─────────────────────────────────────────────────────────

function renderTimeline() {
    removeExpandedCard();
    const canvas = document.getElementById('timeline-canvas');
    const viewport = document.getElementById('timeline-viewport');
    const emptyEl = document.getElementById('empty-state');

    canvas.innerHTML = '';

    if (state.events.length === 0) {
        emptyEl.classList.remove('hidden');
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        return;
    }
    emptyEl.classList.add('hidden');

    const pxPerYear = ZOOM_LEVELS[state.zoom].pxPerYear;
    const pxPerDay = pxPerYear / 365.25;
    const padding = 120;
    const viewportH = viewport.clientHeight || 500;
    const axisY = viewportH * 0.48;
    const canvasH = viewportH;

    // Date bounds with padding
    const dates = state.events.map(e => new Date(e.date + 'T00:00:00'));
    let minDate = new Date(Math.min(...dates));
    let maxDate = new Date(Math.max(...dates));
    minDate = new Date(minDate.getFullYear() - 1, 0, 1);
    maxDate = new Date(maxDate.getFullYear() + 2, 0, 1);

    state.eras.forEach(era => {
        const s = new Date(era.start_date + 'T00:00:00');
        const e = new Date(era.end_date + 'T00:00:00');
        if (s < minDate) minDate = new Date(s.getFullYear() - 1, 0, 1);
        if (e > maxDate) maxDate = new Date(e.getFullYear() + 2, 0, 1);
    });

    const totalDays = (maxDate - minDate) / 86400000;
    const canvasW = totalDays * pxPerDay + padding * 2;

    canvas.style.width = canvasW + 'px';
    canvas.style.height = canvasH + 'px';

    const dateToX = (d) => {
        const dt = d instanceof Date ? d : new Date(d + 'T00:00:00');
        return ((dt - minDate) / 86400000) * pxPerDay + padding;
    };

    // Store for export/card use
    canvas._dateToX = dateToX;
    canvas._axisY = axisY;
    canvas._canvasH = canvasH;

    // Era bands
    state.eras.forEach(era => {
        const x1 = dateToX(era.start_date);
        const x2 = dateToX(era.end_date);
        const band = el('div', 'era-band');
        band.style.left = x1 + 'px';
        band.style.width = (x2 - x1) + 'px';
        band.style.backgroundColor = era.color_hex;
        canvas.appendChild(band);

        const label = el('div', 'era-label');
        label.style.left = (x1 + 10) + 'px';
        label.textContent = era.name;
        canvas.appendChild(label);
    });

    // Axis
    const axis = el('div', 'timeline-axis');
    axis.style.cssText = `position:absolute;top:${axisY}px;left:0;right:0;height:1px;`;
    canvas.appendChild(axis);

    // Year labels + ticks
    const verticalScale = Math.min(axisY - 60, 130);

    for (let y = minDate.getFullYear(); y <= maxDate.getFullYear(); y++) {
        const x = dateToX(new Date(y, 0, 1));

        const label = el('span', 'year-label');
        label.style.left = x + 'px';
        label.style.top = (axisY + 14) + 'px';
        label.textContent = y;
        canvas.appendChild(label);

        const tick = el('div', 'year-tick');
        tick.style.left = (x - 0.5) + 'px';
        tick.style.top = (axisY - 4) + 'px';
        canvas.appendChild(tick);
    }

    // Event nodes
    state.events.forEach(event => {
        const x = dateToX(event.date);
        const yOffset = (event.sentiment_score / 5) * verticalScale;
        const y = axisY - yOffset;
        const size = nodeSize(event.sentiment_score);
        const color = sentimentColor(event.sentiment_score);

        // Connector line
        const lineTop = Math.min(y, axisY);
        const lineBot = Math.max(y, axisY);
        const lineH = lineBot - lineTop;
        if (lineH > 1) {
            const conn = el('div', 'connector-line');
            conn.style.left = x + 'px';
            conn.style.top = lineTop + 'px';
            conn.style.height = lineH + 'px';
            conn.style.backgroundColor = color;
            canvas.appendChild(conn);
        }

        // Node
        const node = el('div', 'event-node');
        if (state.selectedId === event.id) node.classList.add('selected');
        if (state.newEventId === event.id) {
            node.classList.add('just-added');
            setTimeout(() => { state.newEventId = null; }, 900);
        }
        node.style.left = x + 'px';
        node.style.top = y + 'px';
        node.style.width = size + 'px';
        node.style.height = size + 'px';
        node.style.backgroundColor = color;
        node.dataset.id = event.id;
        node.addEventListener('click', (e) => {
            e.stopPropagation();
            selectEvent(event.id);
        });

        // Hover label show/hide
        node.addEventListener('mouseenter', () => {
            const lbl = canvas.querySelector(`.node-label[data-for="${event.id}"]`);
            if (lbl) lbl.classList.add('visible');
        });
        node.addEventListener('mouseleave', () => {
            if (state.selectedId === event.id) return;
            const lbl = canvas.querySelector(`.node-label[data-for="${event.id}"]`);
            if (lbl) lbl.classList.remove('visible');
        });

        canvas.appendChild(node);

        // Label
        const lbl = el('div', 'node-label');
        lbl.dataset.for = event.id;
        if (state.selectedId === event.id) lbl.classList.add('visible');
        const labelY = event.sentiment_score >= 0 ? y - size / 2 - 34 : y + size / 2 + 8;
        lbl.style.left = x + 'px';
        lbl.style.top = labelY + 'px';
        lbl.innerHTML = `
            <div class="node-label-date">${formatDate(event.date, event.date_precision)}</div>
            <div class="node-label-headline">${esc(event.headline)}</div>
        `;
        canvas.appendChild(lbl);
    });

    // Show expanded card if selected
    if (state.selectedId) {
        const ev = state.events.find(e => e.id === state.selectedId);
        if (ev) {
            requestAnimationFrame(() => renderExpandedCard(ev));
        }
    }
}

// ── Expanded card (fixed overlay near node) ─────────────────────────────────────

function renderExpandedCard(event) {
    removeExpandedCard();

    const node = document.querySelector(`.event-node[data-id="${event.id}"]`);
    if (!node) return;
    const rect = node.getBoundingClientRect();

    const card = document.createElement('div');
    card.id = 'expanded-card';
    card.className = 'event-card';
    card.style.position = 'fixed';
    card.style.zIndex = '35';

    const sentimentPct = ((event.sentiment_score + 5) / 10) * 100;
    const color = sentimentColor(event.sentiment_score);
    const eraName = event.era_id ? (state.eras.find(e => e.id === event.era_id)?.name || '') : '';

    card.innerHTML = `
        <div class="card-headline">${esc(event.headline)}</div>
        <div class="card-date">${formatDateLong(event.date, event.date_precision)}${eraName ? ' &middot; ' + esc(eraName) : ''}</div>
        ${event.explanation ? `<div class="card-explanation">${esc(event.explanation)}</div>` : ''}
        <div class="sentiment-bar">
            <div class="sentiment-bar-fill" style="width:${sentimentPct}%;background:${color}"></div>
        </div>
        <div class="card-actions">
            <button onclick="editEvent(${event.id})">Edit</button>
            <button onclick="reReflect(${event.id})">Re-reflect</button>
            <button class="btn-delete" onclick="confirmDelete(${event.id})" style="margin-left:auto">Delete</button>
        </div>
    `;

    document.body.appendChild(card);

    // Position after DOM insertion so we know actual height
    const cardRect = card.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - cardRect.width / 2;
    left = Math.max(12, Math.min(left, window.innerWidth - cardRect.width - 12));

    let top = rect.bottom + 12;
    if (top + cardRect.height > window.innerHeight - 20) {
        top = rect.top - cardRect.height - 12;
        if (top < 60) top = 60;
    }

    card.style.left = left + 'px';
    card.style.top = top + 'px';

    // Close on outside click
    card._outsideHandler = (e) => {
        if (!card.contains(e.target) && !e.target.closest('.event-node')) {
            deselectEvent();
        }
    };
    setTimeout(() => document.addEventListener('click', card._outsideHandler), 0);
}

function removeExpandedCard() {
    const card = document.getElementById('expanded-card');
    if (card) {
        if (card._outsideHandler) document.removeEventListener('click', card._outsideHandler);
        card.remove();
    }
}

// ── Selection ───────────────────────────────────────────────────────────────────

function selectEvent(id) {
    state.selectedId = state.selectedId === id ? null : id;
    renderTimeline();
}

function deselectEvent() {
    if (!state.selectedId) return;
    state.selectedId = null;
    removeExpandedCard();
    renderTimeline();
}

// ── Event CRUD ──────────────────────────────────────────────────────────────────

function openEventModal(editId = null) {
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
    document.getElementById('event-modal').classList.remove('hidden');
    document.getElementById('evt-headline').focus();
}

function closeEventModal() {
    document.getElementById('event-modal').classList.add('hidden');
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
    const year = document.getElementById('evt-year').value;
    if (!year) { toast('Please enter a year'); return; }

    let result;
    if (state.editingId) {
        result = await api(`/api/events/${state.editingId}`, { method: 'PUT', body: data });
    } else {
        result = await api('/api/events', { method: 'POST', body: data });
        state.newEventId = result.id;
    }
    await loadEvents();
    closeEventModal();
    renderTimeline();
    if (!state.editingId) scrollToEvent(result.id);
    toast(state.editingId ? 'Event updated' : 'Event saved');
}

function editEvent(id) {
    deselectEvent();
    openEventModal(id);
}

async function confirmDelete(id) {
    if (!confirm('Delete this event?')) return;
    await api(`/api/events/${id}`, { method: 'DELETE' });
    deselectEvent();
    await loadEvents();
    renderTimeline();
    toast('Event deleted');
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

    let result;
    if (state.editingId) {
        result = await api(`/api/events/${state.editingId}`, { method: 'PUT', body: data });
    } else {
        result = await api('/api/events', { method: 'POST', body: data });
        state.newEventId = result.id;
    }
    await loadEvents();
    closeEventModal();
    renderTimeline();
    if (result) scrollToEvent(result.id);
    toast('Saved with reflection');
}

function backToForm() {
    showStep('step-form');
}

// ── Era management ──────────────────────────────────────────────────────────────

function openEraModal() {
    document.getElementById('era-modal').classList.remove('hidden');
    renderEraList();
    renderEraColorPicker();
    setupEraDayListeners();
}

function closeEraModal() {
    document.getElementById('era-modal').classList.add('hidden');
}

function renderEraList() {
    const list = document.getElementById('era-list');
    if (state.eras.length === 0) {
        list.innerHTML = '<p class="text-sm text-ink-lighter italic">No eras yet.</p>';
        return;
    }
    list.innerHTML = state.eras.map(era => `
        <div class="era-item">
            <div class="era-item-color" style="background:${era.color_hex}"></div>
            <div class="era-item-name">${esc(era.name)}</div>
            <div class="era-item-dates">${formatDate(era.start_date, era.start_date_precision)} &ndash; ${formatDate(era.end_date, era.end_date_precision)}</div>
            <button class="era-item-delete" onclick="deleteEra(${era.id})" title="Delete">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M6 18L18 6M6 6l12 12"/></svg>
            </button>
        </div>
    `).join('');
}

function renderEraColorPicker() {
    const picker = document.getElementById('era-color-picker');
    if (!picker) return;
    picker.innerHTML = ERA_COLORS.map(c =>
        `<div class="era-swatch${c === selectedEraColor ? ' selected' : ''}" style="background:${c}" onclick="selectEraColor('${c}')"></div>`
    ).join('');
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

async function deleteEra(id) {
    if (!confirm('Delete this era?')) return;
    await api(`/api/eras/${id}`, { method: 'DELETE' });
    await loadEras();
    renderEraList();
    renderTimeline();
}

// ── Export ───────────────────────────────────────────────────────────────────────

function openExportModal() {
    const select = document.getElementById('export-range');
    // Remove old era options
    while (select.options.length > 2) select.remove(2);
    state.eras.forEach(era => {
        const opt = document.createElement('option');
        opt.value = 'era-' + era.id;
        opt.textContent = era.name;
        select.appendChild(opt);
    });
    document.getElementById('export-modal').classList.remove('hidden');
}

function closeExportModal() {
    document.getElementById('export-modal').classList.add('hidden');
}

async function doExport() {
    const title = document.getElementById('export-title').value || 'My Chronoscape';
    const range = document.getElementById('export-range').value;
    const btn = document.getElementById('btn-export-go');
    btn.textContent = 'Exporting...';
    btn.disabled = true;

    closeExportModal();
    deselectEvent();

    const viewport = document.getElementById('timeline-viewport');
    const canvas = document.getElementById('timeline-canvas');

    // Save originals
    const saved = {
        vpOverflow: viewport.style.overflow,
        vpOverflowX: viewport.style.overflowX,
        vpPosition: viewport.style.position,
        vpTop: viewport.style.top,
        vpInset: viewport.style.inset,
        canvasWidth: canvas.style.width,
    };

    try {
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

        const captureWidth = range === 'visible' ? viewport.clientWidth : parseInt(canvas.style.width);

        const img = await html2canvas(range === 'visible' ? viewport : canvas, {
            scale: 2,
            backgroundColor: '#FAF7F2',
            scrollX: 0,
            scrollY: 0,
            windowWidth: captureWidth,
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
        btn.textContent = 'Export as PNG';
        btn.disabled = false;
    }
}

function prepareForExport(viewport, canvas, range) {
    document.body.classList.add('exporting');
    viewport.style.overflow = 'visible';
    viewport.style.overflowX = 'visible';
    viewport.style.position = 'static';
    viewport.style.top = 'auto';
    viewport.style.inset = 'auto';

    if (range !== 'visible') {
        canvas.style.width = canvas.scrollWidth + 'px';
    }
}

function restoreAfterExport(viewport, canvas, saved) {
    document.body.classList.remove('exporting');
    viewport.style.overflow = saved.vpOverflow;
    viewport.style.overflowX = saved.vpOverflowX;
    viewport.style.position = saved.vpPosition;
    viewport.style.top = saved.vpTop;
    viewport.style.inset = saved.vpInset;
    canvas.style.width = saved.canvasWidth;
}

function getDateRange() {
    if (state.events.length === 0) return '';
    const dates = state.events.map(e => e.date).sort();
    return formatDate(dates[0]) + ' \u2013 ' + formatDate(dates[dates.length - 1]);
}

// ── Settings ────────────────────────────────────────────────────────────────────

function toggleSettings() {
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

async function loadSettingsUI() {
    try {
        const s = await api('/api/settings');
        document.getElementById('set-url').value = s.llm_base_url;
        document.getElementById('set-model').value = s.llm_model;
        document.getElementById('set-apikey').value = s.llm_api_key;
    } catch (e) { /* ignore */ }
}

async function testConnection() {
    const url = document.getElementById('set-url').value;
    const model = document.getElementById('set-model').value;
    const el = document.getElementById('connection-result');
    const btn = document.getElementById('btn-test');

    el.classList.remove('hidden');
    el.textContent = 'Testing...';
    el.style.color = '#6B6B6B';
    btn.disabled = true;

    try {
        const r = await api(`/health/llm?base_url=${encodeURIComponent(url)}&model=${encodeURIComponent(model)}`);
        if (r.status === 'ok') {
            if (r.model_available) {
                el.textContent = `Connected. "${model}" is available.`;
                el.style.color = '#15803D';
            } else {
                el.textContent = `Connected, but "${model}" not found. Available: ${r.available_models.join(', ')}`;
                el.style.color = '#B45309';
            }
        } else {
            el.textContent = `Error: ${r.error}`;
            el.style.color = '#B91C1C';
        }
    } catch (e) {
        el.textContent = 'Connection failed';
        el.style.color = '#B91C1C';
    } finally {
        btn.disabled = false;
    }
}

async function saveSettings() {
    await api('/api/settings', {
        method: 'PUT',
        body: {
            llm_base_url: document.getElementById('set-url').value,
            llm_model: document.getElementById('set-model').value,
            llm_api_key: document.getElementById('set-apikey').value,
        },
    });
    toast('Settings saved');
    toggleSettings();
}

// ── Zoom ────────────────────────────────────────────────────────────────────────

function zoomIn() {
    if (state.zoom < ZOOM_LEVELS.length - 1) { state.zoom++; applyZoom(); }
}

function zoomOut() {
    if (state.zoom > 0) { state.zoom--; applyZoom(); }
}

function applyZoom() {
    document.getElementById('zoom-label').textContent = ZOOM_LEVELS[state.zoom].label;
    const viewport = document.getElementById('timeline-viewport');
    const centerRatio = (viewport.scrollLeft + viewport.clientWidth / 2) /
        (parseInt(document.getElementById('timeline-canvas').style.width) || 1);
    renderTimeline();
    const newWidth = parseInt(document.getElementById('timeline-canvas').style.width) || 1;
    viewport.scrollLeft = centerRatio * newWidth - viewport.clientWidth / 2;
}

// ── Keyboard shortcuts ──────────────────────────────────────────────────────────

function setupKeyboard() {
    document.addEventListener('keydown', (e) => {
        const tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

        switch (e.key) {
            case 'n': case 'N': e.preventDefault(); openEventModal(); break;
            case 'e': case 'E': e.preventDefault(); openExportModal(); break;
            case 'Escape': closeAll(); break;
            case 'ArrowLeft': scrollTimeline(-250); break;
            case 'ArrowRight': scrollTimeline(250); break;
            case '+': case '=': zoomIn(); break;
            case '-': case '_': zoomOut(); break;
        }
    });
}

function scrollTimeline(delta) {
    document.getElementById('timeline-viewport').scrollBy({ left: delta, behavior: 'smooth' });
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

function scrollToLatest() {
    if (state.events.length === 0) return;
    const latest = state.events[state.events.length - 1];
    scrollToEvent(latest.id);
}

function scrollToEvent(id) {
    requestAnimationFrame(() => {
        const node = document.querySelector(`.event-node[data-id="${id}"]`);
        if (!node) return;
        const viewport = document.getElementById('timeline-viewport');
        const nodeLeft = parseFloat(node.style.left);
        viewport.scrollLeft = nodeLeft - viewport.clientWidth / 2;
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
    document.getElementById('sentiment-label').textContent =
        `${SENTIMENT_LABELS[String(val)]} (${sign}${val})`;
}

function updateHeadlineCount() {
    const len = document.getElementById('evt-headline').value.length;
    document.getElementById('headline-count').textContent = `${len} / 120`;
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
        document.body.appendChild(t);
    }
    clearTimeout(toastTimer);
    t.textContent = msg;
    requestAnimationFrame(() => t.classList.add('show'));
    toastTimer = setTimeout(() => t.classList.remove('show'), 2500);
}

// ── Resize handler ──────────────────────────────────────────────────────────────

let resizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderTimeline(), 150);
});
