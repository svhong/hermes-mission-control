// Overview: now, today, trends, and what needs attention.
import { el, card, tile, columns, bars, table, pill, money, num, ago, duration, dayLabel, shortId, sectionError, empty } from '../lib.js';

// Costs are only meaningful when the provider reports prices; on a
// subscription every cost is 0, so show tokens instead.
function isPriced(data) {
  const daily = Array.isArray(data.daily) ? data.daily : [];
  return Boolean(data.models?.priced) || daily.some(d => d.cost > 0);
}

function billingNote(models) {
  const b = (models?.billing || [])[0];
  if (!b) return "this provider doesn't report cost";
  const how = [b.billing_provider, b.billing_mode, b.cost_status].filter(Boolean).join(' · ');
  return how ? `cost not reported (${how})` : "this provider doesn't report cost";
}

const dayLabelFn = (d, short) => short ? dayLabel(d.day).replace(/^\w+ /, '') : dayLabel(d.day);

export function render(root, { data, now }) {
  const priced = isPriced(data);
  root.append(
    gatewayStrip(data.gateways, now),
    tiles(data.now, data.today, priced, data.models),
    warnings(data.warnings),
    el('div', { class: 'grid two' },
      priced
        ? card('Spend per day', sectionError(data.daily) || columns(data.daily, {
          series: [{ key: 'cost', label: 'Spend', slot: 1 }], format: money, title: 'Spend per day', label: dayLabelFn,
        }), { note: 'last 14 days, by the day a session started' })
        : card('Tokens per day', sectionError(data.daily) || columns(data.daily, {
          series: [{ key: 'tokens', label: 'Tokens in + out', slot: 1 }], title: 'Tokens per day', label: dayLabelFn,
        }), { note: `last 14 days · ${billingNote(data.models)}` }),
      card('Sessions per day', sectionError(data.daily) || columns(data.daily, {
        series: [{ key: 'conversations', label: 'Conversations', slot: 1 }, { key: 'subagents', label: 'Subagents', slot: 2 },
          { key: 'scheduled', label: 'Scheduled runs', slot: 3 }],
        title: 'Sessions per day', label: dayLabelFn,
      }), { note: 'last 14 days' })),
    el('div', { class: 'grid three' },
      modelsCard(data.models, priced), platformsCard(data.platforms, priced), toolsCard(data.tools)),
    recentCard(data.recent, now, priced),
  );
}

function gatewayStrip(gws, now) {
  const err = sectionError(gws);
  if (err) return err;
  if (!gws.length) return el('div', { class: 'strip' }, pill('unknown', 'No gateway has reported a heartbeat'));
  return el('div', { class: 'strip' }, gws.slice(0, 4).map(g => el('div', { class: 'strip-item' },
    g.alive ? pill('alive', 'Gateway running') : pill('stale', 'Gateway not responding'),
    el('span', { class: 'muted', text: [
      g.profile !== 'default' ? `profile ${g.profile}` : null,
      g.host || null,
      g.alive ? `up ${duration(g.uptime_s)}` : `last heartbeat ${ago(g.last_heartbeat, now)}`,
      `pid ${g.pid}`,
    ].filter(Boolean).join(' · ') }))));
}

function tiles(n, t, priced, models) {
  const errs = [sectionError(n), sectionError(t)].filter(Boolean);
  if (errs.length) return el('div', {}, errs);
  const workers = Object.entries(n.workers || {}).filter(([k]) => k !== 'child').map(([k, v]) => `${v} ${k}`).join(', ');
  return el('div', { class: 'tiles' },
    tile('Active conversations', num(n.active_conversations), 'did something in the last 10 min'),
    tile('Subagents working', num(n.active_subagents), workers ? `other workers: ${workers}` : 'delegated tasks running now'),
    tile('Queued requests', n.queued === null ? '–' : num(n.queued), 'waiting for a turn', n.queued > 5 ? 'warning' : null),
    priced ? tile('Spend today', money(t.cost), `${num(t.tokens)} tokens in + out`)
      : tile('Tokens today', num(t.tokens), billingNote(models)),
    tile('Messages today', num(t.messages), `${num(t.conversations)} conversations · ${num(t.scheduled || 0)} scheduled runs`),
    tile('Tool calls today', num(t.tool_calls), `${num(t.subagents)} subagents started`),
  );
}

function warnings(w) {
  const err = sectionError(w);
  if (err) return err;
  if (!w.length) return el('div', { class: 'strip ok' }, pill('done', 'Nothing needs attention'));
  return el('div', { class: 'warnings' }, w.map(x => el('div', { class: 'warning-row' }, pill(x.level, x.level === 'critical' ? 'Problem' : 'Check'), el('span', { text: x.text }))));
}

function modelsCard(m, priced) {
  const err = sectionError(m);
  if (err) return card('Models', err);
  return card('Models', bars(m.rows.map(r => ({
    label: r.model, value: r.tokens,
    sub: [priced ? money(r.cost) : null, `${num(r.api_calls)} API calls`, r.cache_read ? `${num(r.cache_read)} cached` : null].filter(Boolean).join(' · '),
    tip: [r.model, `Input: ${num(r.input)}`, `Output: ${num(r.output)}`, `Cache read: ${num(r.cache_read)}`, `Reasoning: ${num(r.reasoning)}`, `Cost: ${money(r.cost)}`],
  }))), { note: `tokens in + out, last ${m.days} days` });
}

function platformsCard(p, priced) {
  const err = sectionError(p);
  if (err) return card('Platforms', err);
  return card('Platforms', bars(p.rows.map(r => ({
    label: r.platform === 'cron' ? 'cron (scheduled)' : r.platform, value: r.conversations, sub: priced ? money(r.cost) : null,
  }))), { note: `sessions, last ${p.days} days` });
}

function failures(byDisp) {
  // Hermes labels results itself; count anything that sounds like a failure.
  return Object.entries(byDisp).filter(([k]) => /fail|error|denied|reject|block/i.test(k)).reduce((a, [, v]) => a + v, 0);
}

function toolsCard(t) {
  const err = sectionError(t);
  if (err) return card('Tools', err);
  return card('Tools', bars(t.rows.map(r => {
    const f = failures(r.by_disposition);
    return {
      label: r.tool, value: r.calls,
      sub: !t.labelled ? null : f ? `${f} failed (${Math.round(f / r.calls * 100)}%)` : 'no failures',
      tip: [r.tool, ...Object.entries(r.by_disposition).map(([k, v]) => `${k}: ${num(v)}`)],
    };
  })), { note: `tool calls, last ${t.days} days${t.labelled ? '' : " · Hermes doesn't record which failed"}` });
}

function recentCard(rows, now, priced) {
  const err = sectionError(rows);
  if (err) return card('Recent conversations', err, { wide: true });
  if (!rows.length) return card('Recent conversations', empty('No conversations yet'), { wide: true });
  return card('Recent conversations', table([
    { label: 'Status', cell: r => r.active ? pill('active') : r.ended_at ? pill('done', r.scheduled ? 'Ran' : 'Ended') : pill('idle') },
    { label: 'Conversation', class: 'grow', cell: r => el('div', {},
      el('div', { class: 'strong', text: r.title || shortId(r.id) }),
      r.activity && r.active ? el('div', { class: 'muted small', text: r.activity }) : null) },
    { label: 'Platform', cell: r => (r.scheduled ? 'cron (scheduled)' : r.source) + (r.profile !== 'default' ? ` · ${r.profile}` : '') },
    { label: 'Model', class: 'hide-sm', cell: r => r.model || '–' },
    { label: 'Messages', class: 'num', cell: r => num(r.messages) },
    { label: 'Subagents', class: 'num', cell: r => num(r.subagents) },
    { label: 'Tokens', class: 'num', cell: r => num(r.tokens) },
    ...(priced ? [{ label: 'Cost', class: 'num', cell: r => money(r.cost) }] : []),
    { label: 'Last active', class: 'num', cell: r => ago(r.last_activity_at, now) },
  ], rows), { wide: true });
}
