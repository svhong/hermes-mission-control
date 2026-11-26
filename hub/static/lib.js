// Shared helpers for tabs: DOM building, formatting, status labels, charts.
// Everything sets text with textContent, so data from Hermes can't inject HTML.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// ---- formatting -----------------------------------------------------------------

export function money(v) {
  if (v === null || v === undefined) return '–';
  if (v > 0 && v < 0.01) return '<$0.01';
  return '$' + v.toFixed(v >= 100 ? 0 : 2);
}

export function num(v) {
  if (v === null || v === undefined) return '–';
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M';
  if (a >= 1e4) return Math.round(v / 1e3) + 'k';
  if (a >= 1e3) return (v / 1e3).toFixed(1) + 'k';
  return String(Math.round(v));
}

export function duration(s) {
  if (s === null || s === undefined) return '–';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
}

export function ago(t, now) {
  if (!t) return '–';
  const s = Math.max(0, now - t);
  if (s < 10) return 'just now';
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}

export function clock(t) {
  if (!t) return '–';
  const d = new Date(t * 1000);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === today.toDateString() ? time : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
}

export function shortId(id) {
  if (!id) return '';
  return id.length > 14 ? id.slice(0, 6) + '…' + id.slice(-5) : id;
}

export function dayLabel(key) {
  const [y, m, d] = key.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// ---- status: color + icon + label, never color alone -----------------------------------

const STATUS = {
  running: ['good', '●', 'Running'],
  active: ['good', '●', 'Active'],
  alive: ['good', '●', 'Up'],
  done: ['neutral', '✓', 'Done'],
  idle: ['neutral', '◌', 'Idle'],
  queued: ['warning', '…', 'Queued'],
  warning: ['warning', '▲', 'Warning'],
  failed: ['critical', '✕', 'Failed'],
  critical: ['critical', '✕', 'Problem'],
  stale: ['critical', '✕', 'No heartbeat'],
  unknown: ['neutral', '?', 'Unknown'],
};

export function pill(status, label) {
  const [tone, icon, text] = STATUS[status] || ['neutral', '•', status || 'unknown'];
  return el('span', { class: `pill tone-${tone}${status === 'running' || status === 'active' ? ' live' : ''}` },
    el('span', { class: 'pill-icon', 'aria-hidden': 'true', text: icon }), label || text);
}

// A free-form state string from Hermes (e.g. async delegation state) → pill.
export function statePill(state) {
  const s = String(state || '').toLowerCase();
  if (/fail|error|crash|abort|timeout|expired|cancel/.test(s)) return pill('failed', state);
  if (/run|active|start|progress|dispatch|working/.test(s)) return pill('running', state);
  if (/pend|queue|wait|retry|paused|ready/.test(s)) return pill('queued', state);
  if (/done|complete|deliver|success|succeed|finish/.test(s)) return pill('done', state);
  return pill('unknown', state || 'unknown');
}

// ---- layout pieces ------------------------------------------------------------------

export function card(title, body, opts = {}) {
  return el('section', { class: 'card' + (opts.wide ? ' wide' : '') },
    title ? el('header', { class: 'card-head' }, el('h2', { text: title }), opts.note ? el('span', { class: 'note', text: opts.note }) : null) : null,
    body);
}

export function tile(label, value, sub, tone) {
  return el('div', { class: 'tile' + (tone ? ' tone-' + tone : '') },
    el('div', { class: 'tile-label', text: label }),
    el('div', { class: 'tile-value', text: value }),
    sub ? el('div', { class: 'tile-sub', text: sub }) : null);
}

export function sectionError(data) {
  if (data && typeof data === 'object' && !Array.isArray(data) && data._error) {
    return el('div', { class: 'error', text: "Couldn't read this from Hermes: " + data._error });
  }
  return null;
}

export function empty(text) {
  return el('div', { class: 'empty', text });
}

export function table(columns, rows, opts = {}) {
  // columns: [{label, cell: row => Node|string, class}]
  const t = el('table', { class: 'table' },
    el('thead', {}, el('tr', {}, columns.map(c => el('th', { class: c.class || null, text: c.label })))),
    el('tbody', {}, rows.map(r => el('tr', { class: opts.rowClass ? opts.rowClass(r) : null },
      columns.map(c => el('td', { class: c.class || null, 'data-label': c.label }, c.cell(r)))))));
  return el('div', { class: 'table-wrap' }, t);
}

// ---- tooltip ----------------------------------------------------------------------

let tipEl;
export function tip(node, lines) {
  const show = e => {
    if (!tipEl) { tipEl = el('div', { class: 'tooltip', role: 'tooltip' }); document.body.append(tipEl); }
    const content = typeof lines === 'function' ? lines() : lines;
    tipEl.replaceChildren(...content.map((l, i) => el('div', { class: i === 0 ? 'tip-title' : 'tip-line' }, l)));
    tipEl.hidden = false;
    move(e);
  };
  const move = e => {
    if (!tipEl) return;
    const pad = 12, w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
    tipEl.style.left = Math.max(8, x) + 'px';
    tipEl.style.top = Math.max(8, y) + 'px';
  };
  const hide = () => { if (tipEl) tipEl.hidden = true; };
  node.addEventListener('pointerenter', show);
  node.addEventListener('pointermove', move);
  node.addEventListener('pointerleave', hide);
  node.addEventListener('focus', e => { const r = node.getBoundingClientRect(); show({ clientX: r.right, clientY: r.top }); });
  node.addEventListener('blur', hide);
  return node;
}
export function hideTip() { if (tipEl) tipEl.hidden = true; }

// ---- charts -----------------------------------------------------------------------

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

// Vertical (stacked) columns, one per item. HTML so text stays readable on phones.
// series: [{key, label, slot}] ; slot = categorical color slot 1..8
export function columns(items, { series, label, format = num, height = 140, title }) {
  const totals = items.map(it => series.reduce((a, s) => a + (it[s.key] || 0), 0));
  const max = niceMax(Math.max(0, ...totals));
  const plot = el('div', { class: 'cols-plot' });
  plot.style.height = height + 'px';
  for (const f of [1, 0.5]) {
    const g = el('div', { class: 'grid-line' }, el('span', { text: format(max * f) }));
    g.style.bottom = (f * 100) + '%';
    plot.append(g);
  }
  const bars = el('div', { class: 'cols-bars' });
  items.forEach((it, i) => {
    const col = el('div', { class: 'col', tabindex: '0' });
    const stack = el('div', { class: 'col-stack' });
    stack.style.height = (totals[i] / max * 100) + '%';
    for (const s of series) {
      const v = it[s.key] || 0;
      if (!v) continue;
      const seg = el('div', { class: `seg slot-${s.slot}` });
      seg.style.flexGrow = v;
      stack.append(seg);
    }
    col.append(stack);
    tip(col, () => [label(it), ...series.map(s => `${s.label}: ${format(it[s.key] || 0)}`),
      ...(series.length > 1 ? [`Total: ${format(totals[i])}`] : [])]);
    bars.append(col);
  });
  plot.append(bars);
  const axis = el('div', { class: 'cols-axis' });
  const every = Math.ceil(items.length / 7);
  items.forEach((it, i) => axis.append(el('span', { text: i % every === 0 || i === items.length - 1 ? label(it, true) : '' })));
  const legend = series.length > 1
    ? el('div', { class: 'legend' }, series.map(s => el('span', { class: 'legend-item' }, el('i', { class: `swatch slot-${s.slot}` }), s.label)))
    : null;
  return el('figure', { class: 'chart', 'aria-label': title || '' }, legend, plot, axis);
}

// Horizontal bars, sorted by the caller. rows: [{label, value, sub}]
export function bars(rows, { format = num, slot = 1 } = {}) {
  if (!rows.length) return empty('Nothing yet');
  const max = Math.max(...rows.map(r => r.value), 0) || 1;
  return el('div', { class: 'bars' }, rows.map(r => {
    const fill = el('div', { class: `bar-fill slot-${slot}` });
    fill.style.width = Math.max(0.5, r.value / max * 100) + '%';
    const row = el('div', { class: 'bar-row', tabindex: '0' },
      el('div', { class: 'bar-text' }, el('span', { class: 'bar-label', text: r.label }), el('span', { class: 'bar-value', text: format(r.value) })),
      el('div', { class: 'bar-track' }, fill),
      r.sub ? el('div', { class: 'bar-sub', text: r.sub }) : null);
    if (r.tip) tip(row, r.tip);
    return row;
  }));
}
