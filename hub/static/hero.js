// The hero: health of the machine Hermes runs on, above every tab.
// Data comes from the collector's "machine" section (collector/machine.py).
import { el, duration, tip } from './lib.js';

const SVG = 'http://www.w3.org/2000/svg';
const HISTORY_S = 15 * 60;

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, v);
  node.append(...children);
  return node;
}

export function bytes(v, digits) {
  if (v === null || v === undefined) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(v) >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  const d = digits ?? (v >= 100 || i === 0 ? 0 : 1);
  return v.toFixed(d) + ' ' + units[i];
}

const rate = v => (v === null || v === undefined) ? '–' : bytes(v) + '/s';
const pct = v => (v === null || v === undefined) ? '–' : Math.round(v) + '%';
const deg = v => (v === null || v === undefined) ? '–' : Math.round(v) + '°';

function toneFor(value, warn, crit) {
  if (value === null || value === undefined) return 'idle';
  return value >= crit ? 'critical' : value >= warn ? 'warning' : 'ok';
}

// A 270° ring with the value in the middle and an optional sparkline underneath.
function gauge({ label, value, max = 100, text, sub, tone, spark, sparkMax, tipLines }) {
  const frac = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value / max));
  const ring = svg('svg', { viewBox: '0 0 100 100', class: 'ring', 'aria-hidden': 'true' },
    svg('circle', { cx: 50, cy: 50, r: 42, pathLength: 100, class: 'ring-track' }),
    svg('circle', { cx: 50, cy: 50, r: 42, pathLength: 100, class: 'ring-value', 'stroke-dasharray': `${(frac * 75).toFixed(2)} 100` }));
  const node = el('div', { class: 'gauge', 'data-tone': tone, tabindex: '0', role: 'group', 'aria-label': `${label}: ${text}${sub ? ', ' + sub : ''}` },
    el('div', { class: 'gauge-dial' }, ring,
      el('div', { class: 'gauge-center' },
        el('div', { class: 'gauge-value', text }),
        el('div', { class: 'gauge-label', text: label }))),
    el('div', { class: 'gauge-sub', text: sub || '' }),
    spark ? sparkline(spark, sparkMax) : el('div', { class: 'spark spark-empty' }));
  if (tipLines) tip(node, tipLines);
  return node;
}

// points: [[t, v], ...] over the last 15 minutes; max: fixed top of the scale.
function sparkline(points, max) {
  const pts = points.filter(p => p[1] !== null && p[1] !== undefined);
  if (pts.length < 2) return el('div', { class: 'spark spark-empty' });
  const end = pts[pts.length - 1][0];
  const start = Math.min(pts[0][0], end - 60);
  const top = max ?? (Math.max(...pts.map(p => p[1])) * 1.15 || 1);
  const xy = pts.map(([t, v]) => [((t - start) / (end - start)) * 100, 24 - Math.min(1, v / top) * 22]);
  const line = xy.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
  const s = svg('svg', { viewBox: '0 0 100 24', preserveAspectRatio: 'none', class: 'spark', 'aria-hidden': 'true' },
    svg('polygon', { points: `${xy[0][0].toFixed(2)},24 ${line} ${xy[xy.length - 1][0].toFixed(2)},24`, class: 'spark-area' }),
    svg('polyline', { points: line, class: 'spark-line' }));
  return s;
}

function stat(label, value, sub, tone) {
  return el('div', { class: 'hero-stat', 'data-tone': tone || 'ok' },
    el('span', { class: 'hero-stat-label', text: label }),
    el('span', { class: 'hero-stat-value', text: value }),
    sub ? el('span', { class: 'hero-stat-sub', text: sub }) : null);
}

export function renderHero(root, m, { stale } = {}) {
  if (!m) { root.hidden = true; root.replaceChildren(); return; }
  root.hidden = false;
  if (m._error) {
    root.replaceChildren(el('div', { class: 'hero-inner' }, el('div', { class: 'error', text: "Couldn't read this machine's health: " + m._error })));
    return;
  }
  const hist = Array.isArray(m.history) ? m.history.filter(h => h[0] >= (m.history.at(-1)?.[0] ?? 0) - HISTORY_S) : [];
  const cpu = m.cpu || {}, mem = m.mem, root_disk = (m.disks || [])[0];

  // ---- identity line
  const meta = [m.os, m.uptime_s ? `up ${duration(m.uptime_s)}` : null, cpu.model ? cpu.model.replace(/\((R|TM)\)/g, '').replace(/ CPU| Processor/g, '').replace(/\s+@.*/, '') : null,
    `${cpu.threads} threads`].filter(Boolean).join(' · ');
  const worst = [toneFor(cpu.temp, cpu.temp_high ? cpu.temp_high - 10 : 80, cpu.temp_high || 90),
    toneFor(mem?.percent, 85, 95), ...(m.disks || []).map(d => toneFor(d.percent, 85, 95))];
  const overall = stale ? 'stale' : worst.includes('critical') ? 'critical' : worst.includes('warning') ? 'warning' : 'ok';
  const overallText = { ok: 'Healthy', warning: 'Needs a look', critical: 'Attention', stale: 'No recent reading' }[overall];

  const head = el('div', { class: 'hero-head' },
    el('div', { class: 'hero-host' },
      el('span', { class: 'hero-dot', 'data-tone': overall, 'aria-hidden': 'true' }),
      el('span', { class: 'hero-name', text: m.host }),
      el('span', { class: 'hero-state', 'data-tone': overall, text: overallText })),
    el('div', { class: 'hero-meta', text: meta }));

  // ---- gauges
  const coreSplit = cpu.p_percent !== undefined && cpu.p_percent !== null
    ? `P ${pct(cpu.p_percent)} · E ${pct(cpu.e_percent)}` : null;
  const gauges = el('div', { class: 'hero-gauges' },
    gauge({
      label: 'CPU', value: cpu.percent, text: pct(cpu.percent), tone: toneFor(cpu.percent, 85, 97),
      sub: [coreSplit, cpu.freq_mhz ? (cpu.freq_mhz / 1000).toFixed(1) + ' GHz' : null].filter(Boolean).join(' · ') || (cpu.load ? `load ${cpu.load[0]}` : ''),
      spark: hist.map(h => [h[0], h[1]]), sparkMax: 100,
      tipLines: () => ['CPU', `Average since last reading: ${pct(cpu.percent)}`,
        ...(coreSplit ? [`Performance cores: ${pct(cpu.p_percent)} (${cpu.p_threads} threads)`, `Efficiency cores: ${pct(cpu.e_percent)} (${cpu.e_threads} threads)`] : []),
        cpu.load ? `Load (1 / 5 / 15 min): ${cpu.load.join(' / ')}` : null,
        cpu.freq_mhz ? `Clock: ${cpu.freq_mhz} MHz average` : null, 'Chart: last 15 minutes'].filter(Boolean),
    }),
    gauge({
      label: 'Memory', value: mem?.percent, text: pct(mem?.percent), tone: toneFor(mem?.percent, 85, 95),
      sub: mem ? `${bytes(mem.used, 1)} of ${bytes(mem.total, 0)}` : '',
      spark: hist.map(h => [h[0], h[3]]), sparkMax: 100,
      tipLines: () => ['Memory', mem ? `In use: ${bytes(mem.used, 1)} of ${bytes(mem.total, 1)}` : 'Unavailable',
        mem?.swap_total ? `Swap: ${bytes(mem.swap_used, 1)} of ${bytes(mem.swap_total, 1)}` : 'No swap',
        mem?.temp !== null && mem?.temp !== undefined ? `RAM temperature: ${deg(mem.temp)}C` : null, 'Chart: last 15 minutes'].filter(Boolean),
    }),
    gauge({
      label: 'Disk', value: root_disk?.percent, text: pct(root_disk?.percent), tone: toneFor(root_disk?.percent, 85, 95),
      sub: root_disk ? `${bytes(root_disk.total - root_disk.used, 0)} free` : '',
      tipLines: () => ['Disks', ...(m.disks || []).map(d => `${d.mount}: ${bytes(d.used, 1)} of ${bytes(d.total, 1)} (${pct(d.percent)})`),
        m.disk_io ? `Reading ${rate(m.disk_io[0])}, writing ${rate(m.disk_io[1])}` : null,
        m.disk_temp !== null && m.disk_temp !== undefined ? `Drive temperature: ${deg(m.disk_temp)}C` : null].filter(Boolean),
    }),
    gauge({
      label: 'CPU temp', value: cpu.temp, max: 100, text: cpu.temp === null || cpu.temp === undefined ? '–' : deg(cpu.temp) + 'C',
      tone: toneFor(cpu.temp, cpu.temp_high ? cpu.temp_high - 10 : 80, cpu.temp_high || 90),
      sub: [m.disk_temp !== null && m.disk_temp !== undefined ? `SSD ${deg(m.disk_temp)}` : null,
        mem?.temp !== null && mem?.temp !== undefined ? `RAM ${deg(mem.temp)}` : null].filter(Boolean).join(' · ') || (cpu.temp === null || cpu.temp === undefined ? 'no sensor' : ''),
      spark: hist.map(h => [h[0], h[2]]), sparkMax: 100,
      tipLines: () => ['Temperatures', ...(m.temps || []).map(t => `${t.chip}${t.label ? ' ' + t.label : ''}: ${deg(t.c)}C`),
        ...((m.temps || []).length ? [] : ['No temperature sensors found'])],
    }));

  // ---- small stats
  const p = m.power, c = m.container;
  const stats = el('div', { class: 'hero-stats' },
    stat('Network', m.net ? `↓ ${rate(m.net[0])}` : '–', m.net ? `↑ ${rate(m.net[1])}` : null),
    stat('Disk I/O', m.disk_io ? `R ${rate(m.disk_io[0])}` : '–', m.disk_io ? `W ${rate(m.disk_io[1])}` : null),
    cpu.load ? stat('Load', String(cpu.load[0]), `${cpu.load[1]} · ${cpu.load[2]}`, toneFor(cpu.load[0] / (cpu.threads || 1), 1, 2)) : null,
    m.fan_rpm !== null && m.fan_rpm !== undefined ? stat('Fan', `${m.fan_rpm} rpm`, null) : null,
    p ? stat(p.battery !== null ? 'Battery' : 'Power', p.battery !== null ? `${p.battery}%` : (p.plugged ? 'On AC' : 'On battery'),
      [p.status && p.status !== 'Unknown' ? p.status : null, p.charger_w ? `${p.charger_w} W charger` : (p.plugged ? 'plugged in' : null),
        p.charge_limit && p.charge_limit < 100 ? `limit ${p.charge_limit}%` : null].filter(Boolean).join(' · '),
      p.plugged === false ? 'warning' : 'ok') : null,
    c ? stat('Hermes container', `${pct(c.cpu_percent)} CPU`,
      [`${bytes(c.mem, 1)}${c.mem_limit ? ' of ' + bytes(c.mem_limit, 0) : ''} RAM`, c.pids !== null ? `${c.pids} processes` : null,
        c.oom_kills ? `${c.oom_kills} out-of-memory kill${c.oom_kills === 1 ? '' : 's'} since it started` : null].filter(Boolean).join(' · '),
      c.oom_kills ? 'critical' : 'ok') : null);

  root.replaceChildren(el('div', { class: 'hero-inner' }, head, gauges, stats));
}
