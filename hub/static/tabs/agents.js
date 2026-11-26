// Agents: profiles, and conversations with the subagents they delegated to.
import { el, card, table, pill, statePill, money, num, ago, duration, clock, shortId, sectionError, empty } from '../lib.js';

// Kept across re-renders (the page refreshes every few seconds).
// Conversations that are working start expanded, finished ones collapsed;
// clicking flips that default for the conversation.
const ui = { filter: 'all', open: new Set(), flipped: new Set(), showAll: new Set() };
const SHORT_LIST = 8;

// Long tables show the latest few, with a button for the rest.
function limited(id, rows, rerender) {
  if (rows.length <= SHORT_LIST || ui.showAll.has(id)) return { rows, more: null };
  const more = el('button', { type: 'button', class: 'link-btn more', onclick: () => { ui.showAll.add(id); rerender(); }, text: `Show all ${rows.length}` });
  return { rows: rows.slice(0, SHORT_LIST), more };
}

export function render(root, ctx) {
  const { data } = ctx;
  root.append(
    profilesRow(data.profiles, ctx.now),
    treeCard(data.tree, data.window_h, ctx),
    el('div', { class: 'grid two' },
      delegationsCard(data.delegations, ctx),
      resumableCard(data.resumable, ctx)),
  );
}

function profilesRow(profiles, now) {
  const err = sectionError(profiles);
  if (err) return err;
  if (!profiles.length) return null;
  return el('div', { class: 'profiles' }, profiles.map(p => el('div', { class: 'profile' },
    el('div', { class: 'profile-head' },
      el('span', { class: 'strong', text: p.profile }),
      p.gateway_alive === true ? pill('alive', 'Gateway up') : p.gateway_alive === false ? pill('stale', 'Gateway down') : null),
    el('div', { class: 'muted small', text: `${num(p.conversations)} conversations · ${num(p.subagents)} subagents${p.cost > 0 ? ' · ' + money(p.cost) : ''} · 7 days` }),
    el('div', { class: 'muted small', text: `last active ${ago(p.last_activity_at, now)}` }))));
}

function treeCard(tree, windowH, ctx) {
  const err = sectionError(tree);
  if (err) return card('Conversations & subagents', err, { wide: true });
  let convs = tree.conversations;
  if (ui.filter === 'running') convs = convs.filter(c => c.subtree.running > 0 || c.status === 'running');
  const filterBtn = (id, label) => el('button', {
    type: 'button', class: 'seg-btn' + (ui.filter === id ? ' on' : ''), 'aria-pressed': ui.filter === id ? 'true' : 'false',
    onclick: () => { ui.filter = id; ctx.rerender(); }, text: label,
  });
  const body = el('div', {},
    el('div', { class: 'toolbar' },
      el('div', { class: 'seg-group', role: 'group', 'aria-label': 'Filter' }, filterBtn('all', 'All'), filterBtn('running', 'Working now')),
      el('span', { class: 'muted small', text: `Active conversations, and any with subagents in the last ${windowH} h. Click a row for details.` })),
    convs.length ? el('div', { class: 'convs' }, convs.map(c => conversation(c, ctx))) : empty(ui.filter === 'running' ? 'No subagents are working right now' : 'No subagent activity in this window'),
  );
  return card('Conversations & subagents', body, { wide: true, note: tree.total_conversations > convs.length ? `showing ${convs.length} of ${tree.total_conversations}` : null });
}

function conversation(c, ctx) {
  const key = c.id || 'orphans';
  const st = c.subtree;
  const busy = st.running > 0 || c.status === 'running' || c.status === 'active';
  const collapsed = busy === ui.flipped.has(key);
  const head = el('button', {
    type: 'button', class: 'conv-head', 'aria-expanded': collapsed ? 'false' : 'true',
    onclick: () => { ui.flipped.has(key) ? ui.flipped.delete(key) : ui.flipped.add(key); ctx.rerender(); },
  },
  el('span', { class: 'chev', 'aria-hidden': 'true', text: collapsed ? '▸' : '▾' }),
  pill(c.status),
  el('span', { class: 'conv-title' },
    el('span', { class: 'strong', text: c.title || shortId(c.id) || 'Conversation' }),
    el('span', { class: 'muted small', text: [c.source, c.profile !== 'default' ? c.profile : null, c.model, c.continuations ? `continued ${c.continuations}×` : null].filter(Boolean).join(' · ') })),
  el('span', { class: 'conv-stats small' },
    el('span', { text: `${st.subagents} subagent${st.subagents === 1 ? '' : 's'}${st.running ? ` · ${st.running} working` : ''}` }),
    el('span', { class: 'muted', text: `${num((c.tokens_in || 0) + (c.tokens_out || 0) + st.tokens)} tok${(c.cost || 0) + st.cost > 0 ? ' · ' + money((c.cost || 0) + st.cost) : ''} · ${ago(st.last_activity_at, ctx.now)}` })));
  const wrap = el('div', { class: 'conv' + (st.running ? ' has-running' : '') }, head);
  if (!collapsed) {
    if (c.activity && (c.status === 'active' || c.status === 'running')) wrap.append(el('div', { class: 'conv-activity muted small', text: '↳ ' + c.activity }));
    if (c.children.length) wrap.append(el('div', { class: 'subs', role: 'list' }, c.children.map(s => subagent(s, 0, ctx))));
    else wrap.append(el('div', { class: 'muted small pad', text: 'No subagents' }));
  }
  return wrap;
}

function subagent(s, depth, ctx) {
  const open = ui.open.has(s.id);
  const row = el('button', {
    type: 'button', class: 'sub-row', 'aria-expanded': open ? 'true' : 'false',
    onclick: () => { open ? ui.open.delete(s.id) : ui.open.add(s.id); ctx.rerender(); },
  },
  pill(s.status),
  el('span', { class: 'sub-main' },
    el('span', { class: 'strong', text: s.title || 'Subagent ' + shortId(s.id) }),
    el('span', { class: 'muted small', text: [s.model, s.status === 'running' || s.status === 'active' ? s.activity : s.end_reason].filter(Boolean).join(' · ') })),
  el('span', { class: 'sub-nums small' },
    el('span', { text: duration(s.duration_s) }),
    el('span', { class: 'muted', text: `${num(s.tool_calls)} tools · ${num(s.tokens_in + s.tokens_out)} tok${s.cost > 0 ? ' · ' + money(s.cost) : ''}` })));
  const item = el('div', { class: 'sub', role: 'listitem' }, row);
  item.style.setProperty('--depth', depth);
  if (open) item.append(details(s, ctx));
  if (s.children.length) item.append(el('div', { class: 'subs nested', role: 'list' }, s.children.map(k => subagent(k, depth + 1, ctx))));
  return item;
}

function details(s, ctx) {
  const rows = [
    ...(s.goal ? [['Goal', s.goal]] : []),
    ['Session', s.id],
    ['Started', `${clock(s.started_at)} (${ago(s.started_at, ctx.now)})`],
    ['Ended', s.ended_at ? `${clock(s.ended_at)} · ${s.end_reason || 'no reason recorded'}` : 'still open'],
    ['Last activity', `${ago(s.last_activity_at, ctx.now)}${s.activity ? ' · ' + s.activity : ''}`],
    ['Worker', s.worker || 'none recorded'],
    ['Model', s.model || '–'],
    ['Messages / API calls', `${num(s.messages)} / ${num(s.api_calls)}`],
    ['Tokens in / out', `${num(s.tokens_in)} / ${num(s.tokens_out)}`],
    ...(s.cost > 0 ? [['Cost', money(s.cost)]] : []),
    ...(s.tool_names.length ? [['Tools available', s.tool_names.join(', ')]] : []),
  ];
  return el('dl', { class: 'details' }, rows.map(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
}

function delegationsCard(all, { now, rerender }) {
  const err = sectionError(all);
  if (err) return card('Background delegations', err);
  if (!all.length) return card('Background delegations', empty('None in this window'));
  const { rows, more } = limited('delegations', all, rerender);
  return card('Background delegations', el('div', {}, table([
    { label: 'State', cell: r => statePill(r.state) },
    { label: 'Task', class: 'grow', cell: r => el('div', {}, el('div', { text: r.goal || shortId(r.id) }),
      el('div', { class: 'muted small', text: `result ${r.delivery_state || '–'}${r.delivery_attempts > 1 ? ` after ${r.delivery_attempts} tries` : ''}` })) },
    { label: 'Took', class: 'num', cell: r => duration(r.duration_s) },
    { label: 'Started', class: 'num', cell: r => ago(r.dispatched_at, now) },
  ], rows), more), { note: 'tasks handed off to run asynchronously' });
}

function resumableCard(all, { now, rerender }) {
  const err = sectionError(all);
  if (err) return card('Resumable subagents', err);
  if (!all.length) return card('Resumable subagents', empty('None'));
  const { rows, more } = limited('resumable', all, rerender);
  return card('Resumable subagents', el('div', {}, table([
    { label: 'State', cell: r => statePill(r.state) },
    { label: 'Subagent', class: 'grow', cell: r => el('div', {}, el('div', { text: (r.name || shortId(r.id)) + (r.pinned ? ' 📌' : '') }),
      r.stop_cause ? el('div', { class: 'muted small', text: `stopped: ${r.stop_cause}` }) : null) },
    { label: 'Updated', class: 'num', cell: r => ago(r.updated_at, now) },
    { label: 'Expires', class: 'num', cell: r => r.expires_at ? (r.expires_at > now ? 'in ' + duration(r.expires_at - now) : 'expired') : '–' },
  ], rows), more), { note: 'can be paused and picked up later' });
}
