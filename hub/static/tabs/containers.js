// Containers: every Docker container on the box (or the labelled ones), grouped
// by the agent it belongs to (label hmc.source), with health, restarts and resources.
// Fed by collector/docker_monitor.py; a "global" tab, so it shows whichever agent is picked.
import { el, card, tile, pill, table, num, ago, duration, tip, empty } from '../lib.js';

const SVG = 'http://www.w3.org/2000/svg';

export function bytes(v) {
  if (v === null || v === undefined) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? String(Math.round(v)) : v.toFixed(v >= 100 ? 0 : 1)) + ' ' + units[i];
}

function pct(v) {
  if (v === null || v === undefined) return '–';
  return (v >= 10 ? Math.round(v) : v.toFixed(1)) + '%';
}

// Tiny line chart of the recent samples (CPU or memory), drawn as SVG.
function spark(points, max, label) {
  const vals = points.filter(p => p !== null && p !== undefined);
  if (vals.length < 2) return null;
  const w = 72, h = 18;
  const top = Math.max(max || 0, ...vals) || 1;
  const step = w / (points.length - 1);
  let d = '';
  points.forEach((v, i) => {
    if (v === null || v === undefined) return;
    d += (d ? 'L' : 'M') + (i * step).toFixed(1) + ',' + (h - 1 - (v / top) * (h - 2)).toFixed(1);
  });
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('class', 'spark');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', label);
  const path = document.createElementNS(SVG, 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}

function statusCell(c, now) {
  let p;
  if (c.restarting) p = pill('failed', 'Restarting');
  else if (c.state === 'running' && c.health === 'unhealthy') p = pill('critical', 'Unhealthy');
  else if (c.state === 'running' && c.health === 'starting') p = pill('queued', 'Starting');
  else if (c.state === 'running') p = pill('running', c.health === 'healthy' ? 'Healthy' : 'Running');
  else if (c.state === 'paused') p = pill('idle', 'Paused');
  else if (c.state === 'exited' || c.state === 'dead') p = c.exit_code ? pill('failed', `Exited (${c.exit_code})`) : pill('idle', 'Stopped');
  else p = pill('unknown', c.state || 'unknown');
  const sub = c.state === 'running'
    ? `up ${duration(now - c.started_at)}`
    : c.finished_at ? `stopped ${ago(c.finished_at, now)}` : '';
  return el('div', { class: 'cell-stack' }, p, sub ? el('span', { class: 'muted small', text: sub }) : null);
}

function nameCell(c, now) {
  const node = el('div', { class: 'cell-stack', tabindex: '0' },
    el('span', { class: 'strong', text: c.name }),
    el('span', { class: 'muted small ellipsis', text: c.role ? `${c.role} · ${c.image}` : c.image }));
  tip(node, () => [c.name, `id ${c.id}`, `image ${c.image}`,
    c.project ? `compose ${c.project} / ${c.service}` : null,
    `restart policy: ${c.restart_policy}`,
    c.ports?.length ? `ports ${c.ports.join(', ')}` : null,
    c.health ? `health: ${c.health}${c.health_failing_streak ? ` (failed ${c.health_failing_streak}× in a row)` : ''}` : 'no health check',
    c.health_output ? `last check: ${c.health_output}` : null,
    c.oom_killed ? 'was killed for running out of memory' : null,
    c.error ? `error: ${c.error}` : null,
    c.created_at ? `created ${ago(c.created_at, now)}` : null,
  ].filter(Boolean));
  return node;
}

function cpuCell(c) {
  if (c.state !== 'running') return el('span', { class: 'muted', text: '–' });
  const limit = c.cpu_limit ? ` of ${c.cpu_limit} CPU` : '';
  return el('div', { class: 'cell-spark' },
    el('span', { text: c.cpu_pct === null || c.cpu_pct === undefined ? '…' : pct(c.cpu_pct) + limit }),
    spark((c.history || []).map(h => h[1]), 100 * (c.cpu_limit || 1), `CPU history of ${c.name}`));
}

function memCell(c) {
  if (c.state !== 'running') return el('span', { class: 'muted', text: '–' });
  const of = c.mem_limit ? ` / ${bytes(c.mem_limit)}` : '';
  return el('div', { class: 'cell-spark' },
    el('span', { text: bytes(c.mem_bytes) + of }),
    spark((c.history || []).map(h => h[2]), c.mem_limit, `Memory history of ${c.name}`));
}

function netCell(c) {
  if (c.state !== 'running') return el('span', { class: 'muted', text: '–' });
  if (c.rx_rate === null || c.rx_rate === undefined) return el('span', { class: 'muted', text: '…' });
  const node = el('span', { tabindex: '0', text: `↓ ${bytes(c.rx_rate)}/s ↑ ${bytes(c.tx_rate)}/s` });
  tip(node, () => [c.name, `received ${bytes(c.net_rx)} since start`, `sent ${bytes(c.net_tx)} since start`]);
  return node;
}

function restartsCell(c) {
  const n = c.restart_count || 0;
  const recent = c.restarts_last_hour || 0;
  return el('div', { class: 'cell-stack' },
    el('span', { text: num(n) }),
    recent ? el('span', { class: 'small tone-text-warning', text: `${recent} in last hour` }) : null);
}

const COLUMNS = now => [
  { label: 'Container', cell: c => nameCell(c, now), class: 'grow' },
  { label: 'Status', cell: c => statusCell(c, now) },
  { label: 'CPU', cell: cpuCell },
  { label: 'Memory', cell: memCell },
  { label: 'Network', cell: netCell },
  { label: 'Processes', cell: c => c.pids === null || c.pids === undefined ? '–' : num(c.pids), class: 'num' },
  { label: 'Restarts', cell: restartsCell, class: 'num' },
];

function problemsStrip(list) {
  if (!list?.length) return el('div', { class: 'strip ok' }, pill('done', 'All containers healthy'));
  return el('div', { class: 'warnings' }, list.map(x => el('div', { class: 'warning-row' },
    pill(x.level, x.level === 'critical' ? 'Problem' : 'Check'), el('span', { text: x.text }))));
}

function agentStatus(agent, sources, now) {
  const s = sources.find(x => x.id === agent && (x.kind || 'hermes') === 'hermes');
  if (!s) return el('span', { class: 'muted small', text: 'no collector reporting under this name' });
  const gws = s.report.data?.overview?.gateways;
  const up = Array.isArray(gws) ? gws.some(g => g.alive) : null;
  return el('span', { class: 'strip-item' },
    up === true ? pill('alive', 'Gateway up') : up === false ? pill('stale', 'Gateway down') : null,
    el('span', { class: 'muted small', text: `collector last reported ${ago(now - s.age_s, now)}` }));
}

export function render(root, { data, sources, now }) {
  const eng = data.engine || {};
  if (!eng.ok) {
    root.append(el('div', { class: 'error', text: `The container monitor can't read Docker: ${eng.error || 'unknown error'}` }));
    return;
  }
  const rows = data.containers || [];
  const s = data.summary || {};
  root.append(
    el('div', { class: 'tiles four' },
      tile('Containers', `${num(s.running)} / ${num(s.total)}`, s.stopped ? `${s.stopped} stopped` : 'all running'),
      tile('Health', s.unhealthy ? `${s.unhealthy} unhealthy` : s.healthy ? 'OK' : '–',
        `${num(s.healthy)} passing their health check`, s.unhealthy ? 'critical' : null),
      tile('CPU', pct(s.cpu_pct), 'all containers, % of one core'),
      tile('Memory', bytes(s.mem_bytes), `Docker ${eng.version || '?'}`),
    ),
    problemsStrip(data.problems),
  );
  if (!rows.length) { root.append(card('Containers', empty('No containers to show'), { wide: true })); return; }

  // One card per agent, then everything else.
  const groups = new Map();
  for (const c of rows) {
    const key = c.agent || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  for (const [agent, list] of groups) {
    const title = agent ? `Agent: ${agent}` : groups.size > 1 ? 'Other containers' : 'Containers';
    root.append(card(title, el('div', {},
      agent ? el('div', { class: 'toolbar' }, agentStatus(agent, sources || [], now)) : null,
      table(COLUMNS(now), list, { rowClass: c => c.health === 'unhealthy' || c.restarting ? 'row-bad' : null })),
    { wide: true, note: `${list.filter(c => c.state === 'running').length} of ${list.length} running` }));
  }
  root.append(el('p', { class: 'muted small', text: 'CPU and network are averaged since the previous reading (every few seconds while this page is open, about once a minute otherwise). Hover a container name for details.' }));
}
