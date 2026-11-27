// Page shell: login, tab bar, source picker, polling. Each tab lives in
// /static/tabs/<id>.js and exports render(root, ctx).
import { el, ago, pill, hideTip } from './lib.js';

const POLL_MS = 5000;
const $ = id => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

let token = store.get('hmc_token') || '';
let state = null;
let loadedVersion = null; // dashboard code version this page was loaded with
let timer = null;
const modules = {};

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) throw Object.assign(new Error('login required'), { auth: true });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

// ---- login ------------------------------------------------------------------------

function showLogin(show) {
  $('login').hidden = !show;
  $('main').hidden = show;
  $('tabs').hidden = show;
  if (show) $('password').focus();
}

$('login').addEventListener('submit', async e => {
  e.preventDefault();
  $('login-error').hidden = true;
  try {
    const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: $('password').value }) });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || 'Login failed');
    token = body.token || '';
    store.set('hmc_token', token);
    $('password').value = '';
    showLogin(false);
    poll();
  } catch (err) {
    $('login-error').textContent = err.message;
    $('login-error').hidden = false;
  }
});

$('logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch { /* ignore */ }
  token = '';
  store.set('hmc_token', null);
  location.reload();
});

// ---- routing ------------------------------------------------------------------------

function currentTab() {
  const id = location.hash.replace(/^#/, '').split('/')[0];
  const tabs = state?.tabs || [];
  return tabs.find(t => t.id === id) ? id : tabs[0]?.id;
}

// Agents are the Hermes installs; other sources (the container monitor) only
// feed "global" tabs and don't appear in the agent picker.
function agentSources() {
  const sources = state?.sources || [];
  return sources.filter(s => (s.kind || 'hermes') === 'hermes');
}

function currentSource() {
  const sources = agentSources();
  const want = store.get('hmc_source');
  return sources.find(s => s.id === want) || sources[0] || null;
}

function tabMeta(id) {
  return (state?.tabs || []).find(t => t.id === id) || null;
}

// The source a global tab reads from: the first one that reports it.
function globalSource(tabId) {
  return (state?.sources || []).find(s => s.report.data?.[tabId]) || null;
}

function isStale(source) {
  const allowed = Math.max(30, (source.report.interval_s || 5) * 2 + 15);
  return source.age_s > allowed;
}

window.addEventListener('hashchange', () => render());
$('source-select').addEventListener('change', e => { store.set('hmc_source', e.target.value); render(); });

// ---- rendering ----------------------------------------------------------------------

function renderTabs(active) {
  const nav = $('tabs');
  nav.replaceChildren(...(state.tabs || []).map(t => el('a', {
    href: '#' + t.id, role: 'tab', class: 'tab' + (t.id === active ? ' active' : ''),
    'aria-selected': t.id === active ? 'true' : 'false', text: t.title,
  })));
}

function renderTop(source) {
  const status = $('top-status');
  const items = [];
  if (!source) items.push(pill('unknown', 'No collector connected'));
  else {
    const r = source.report;
    // A source is stale if the collector hasn't reported for a while.
    // The collector reports every few seconds while someone watches, about once a
    // minute otherwise; only warn when it's well past its own schedule.
    if (isStale(source)) items.push(pill('stale', `Collector silent ${ago(state.now - source.age_s, state.now)}`));
    else if (!r.db?.ok) items.push(pill('critical', 'Database unreadable'));
    const gws = r.data?.overview?.gateways;
    if (Array.isArray(gws)) {
      const alive = gws.filter(g => g.alive);
      items.push(alive.length ? pill('alive', `Gateway up ${ago(alive[0].started_at, state.now).replace(' ago', '')}`) : pill('stale', 'Gateway down'));
    }
  }
  const cs = globalSource('containers');
  if (cs) {
    const c = cs.report.data.containers;
    const crit = (c.problems || []).filter(p => p.level === 'critical').length;
    const link = node => el('a', { href: '#containers', class: 'pill-link' }, node);
    if (isStale(cs)) items.push(link(pill('stale', 'Container monitor silent')));
    else if (!c.engine?.ok) items.push(link(pill('critical', "Can't reach Docker")));
    else if (crit) items.push(link(pill('critical', `${crit} container problem${crit === 1 ? '' : 's'}`)));
    else if (c.summary) items.push(link(pill('alive', `${c.summary.running}/${c.summary.total} containers up`)));
  }
  status.replaceChildren(...items);

  const agents = agentSources();
  const pick = $('source-pick');
  const sel = $('source-select');
  pick.hidden = agents.length < 2 || !source;
  if (!pick.hidden) {
    sel.replaceChildren(...agents.map(s => el('option', { value: s.id, text: s.id, selected: s.id === source.id })));
  }
  const g = $('garage-link');
  g.hidden = !state.garage_url;
  if (state.garage_url) g.href = state.garage_url;
}

function renderFoot(source) {
  const r = source?.report;
  if (r && r.kind === 'docker') {
    const e = r.data?.containers?.engine || {};
    $('foot').textContent = `${r.source} on ${r.host} · Docker ${e.version || '?'} via ${e.url || '?'} · collected in ${r.collect_ms} ms · updated ${ago(state.now - source.age_s, state.now)}`;
    return;
  }
  $('foot').textContent = r
    ? `${r.source} on ${r.host} · Hermes schema v${r.db?.schema_version ?? '?'} · collected in ${r.collect_ms} ms · updated ${ago(state.now - source.age_s, state.now)}${r.db?.immutable ? ' · Hermes not running (snapshot)' : ''}`
    : '';
}

async function render() {
  if (!state) return;
  const tabId = currentTab();
  const agent = currentSource();
  const global = !!tabMeta(tabId)?.global;
  const source = global ? (globalSource(tabId) || agent) : agent;
  renderTabs(tabId);
  renderTop(agent);
  renderFoot(source);
  const main = $('main');
  if (!source) {
    main.replaceChildren(el('div', { class: 'empty', text: 'Waiting for the collector to send its first report…' }));
    return;
  }
  if (!global && source.report.db?.error) {
    main.replaceChildren(el('div', { class: 'error', text: "The collector can't read Hermes's database: " + source.report.db.error }));
    return;
  }
  if (!tabId) { main.replaceChildren(el('div', { class: 'empty', text: 'No tabs reported.' })); return; }
  try {
    modules[tabId] = modules[tabId] || await import(`./tabs/${tabId}.js`);
    const data = source.report.data?.[tabId];
    const root = el('div', { class: 'tab-root' });
    if (!data) root.append(el('div', { class: 'empty', text: 'This source has no data for this tab.' }));
    else if (data._error) root.append(el('div', { class: 'error', text: 'This tab failed: ' + data._error }));
    else modules[tabId].render(root, { data, source, sources: state.sources, ledger: source.ledger || {}, now: state.now, rerender: render });
    hideTip();
    main.replaceChildren(root);
  } catch (err) {
    main.replaceChildren(el('div', { class: 'error', text: `Couldn't show the ${tabId} tab: ${err.message}` }));
    console.error(err);
  }
}

async function poll() {
  clearTimeout(timer);
  // A hidden tab doesn't poll, so the collector can slow down; we poll again when it's shown.
  if (document.hidden) { timer = setTimeout(poll, POLL_MS); return; }
  try {
    state = await api('/api/state');
    // The dashboard was updated since this page loaded: reload to get the new code.
    if (loadedVersion && state.ui_version && state.ui_version !== loadedVersion) { location.reload(); return; }
    loadedVersion = loadedVersion || state.ui_version;
    $('logout').hidden = !token;
    await render();
  } catch (err) {
    if (err.auth) { token = ''; store.set('hmc_token', null); showLogin(true); return; }
    $('foot').textContent = "Can't reach the hub: " + err.message + ' (retrying)';
  }
  timer = setTimeout(poll, POLL_MS);
}

document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
poll();
