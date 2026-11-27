// Scheduled: Hermes's cron jobs, their recent runs, and anything that went wrong.
import { el, card, tile, pill, num, ago, duration, tip, sectionError, empty, turnText } from '../lib.js';

const ui = { open: new Set() };

const JOB_STATUS = {
  running: ['running', 'Running'],
  failing: ['failed', 'Failing'],
  overdue: ['warning', 'Overdue'],
  ok: ['done', 'OK'],
  paused: ['idle', 'Paused'],
  done: ['done', 'Finished'],
};
const RUN_TONE = { ok: 'good', failed: 'critical', running: 'good', unknown: 'neutral' };
const RUN_ICON = { ok: '✓', failed: '✕', running: '●', unknown: '?' };

// "08:30", "tomorrow 08:30", "Mon 09:00" or "Dec 3 09:00".
function when(t, now) {
  if (!t) return '–';
  const d = new Date(t * 1000);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const day = new Date(now * 1000); day.setHours(0, 0, 0, 0);
  const diffDays = Math.floor((d - day) / 86400000);
  if (diffDays === 0) return 'today ' + time;
  if (diffDays === 1) return 'tomorrow ' + time;
  if (diffDays === -1) return 'yesterday ' + time;
  if (diffDays > 1 && diffDays < 7) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + time;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
}

function until(t, now) {
  if (!t) return '';
  return t >= now ? 'in ' + duration(t - now).replace(/ \d+s$/, '') : duration(now - t).replace(/ \d+s$/, '') + ' late';
}

export function render(root, { data, now, rerender }) {
  const jobsData = data.jobs;
  const err = sectionError(jobsData);
  if (err) { root.append(err); return; }
  const jobs = jobsData.jobs;
  root.append(
    tiles(data.scheduler, jobs, data.today, now),
    problems(data.problems, data.scheduler),
    data.running?.length ? runningCard(data.running, now) : null,
    jobsCard(jobsData, now, rerender),
  );
}

function tiles(sched, jobs, today, now) {
  const enabled = jobs.filter(j => j.enabled);
  const next = enabled.filter(j => j.next_run_at).sort((a, b) => a.next_run_at - b.next_run_at)[0];
  const needs = jobs.filter(j => j.status === 'failing' || j.status === 'overdue').length;
  let schedTile;
  if (!sched || sched._error || sched.ticking === null || sched.ticking === undefined) {
    schedTile = tile('Scheduler', '–', "Hermes doesn't report scheduler ticks");
  } else {
    schedTile = el('div', { class: 'tile' + (sched.ticking ? '' : ' tone-warning') },
      el('div', { class: 'tile-label', text: 'Scheduler' }),
      el('div', { class: 'tile-pill' }, sched.ticking ? pill('running', 'Ticking') : pill('stale', 'Stalled')),
      el('div', { class: 'tile-sub', text: `last tick ${ago(sched.heartbeat_at, now)}` }));
  }
  const t = today && !today._error ? today : { ok: 0, failed: 0, running: 0 };
  return el('div', { class: 'tiles four' },
    schedTile,
    tile('Next run', next ? until(next.next_run_at, now) : '–', next ? `${next.name || next.id} · ${when(next.next_run_at, now)}` : 'nothing scheduled'),
    tile('Runs today', num(t.ok + t.failed + t.running), [`${t.ok} ok`, t.failed ? `${t.failed} failed` : null, t.running ? `${t.running} running` : null].filter(Boolean).join(' · '), t.failed ? 'warning' : null),
    tile('Jobs', num(enabled.length), [`${jobs.length - enabled.length} paused or finished`, needs ? `${needs} need attention` : null].filter(Boolean).join(' · '), needs ? 'warning' : null),
  );
}

function problems(list, sched) {
  const items = Array.isArray(list) ? [...list] : [];
  if (sched && sched.ticking === false) {
    items.unshift({ level: 'critical', text: `Hermes's scheduler hasn't ticked for ${duration(sched.age_s)}; jobs won't run until it does` });
  }
  if (!items.length) return el('div', { class: 'strip ok' }, pill('done', 'All jobs healthy'));
  return el('div', { class: 'warnings' }, items.map(x => el('div', { class: 'warning-row' },
    pill(x.level, x.level === 'critical' ? 'Problem' : 'Check'), el('span', { text: x.text }))));
}

function runningCard(rows, now) {
  return card('Running now', el('div', { class: 'convs' }, rows.map(r => el('div', { class: 'conv has-running' },
    el('div', { class: 'conv-head static' }, pill('running'),
      el('span', { class: 'conv-title' }, el('span', { class: 'strong', text: r.name || r.id }),
        el('span', { class: 'muted small', text: `started ${ago(r.started_at, now)}` }))),
    el('div', { class: 'conv-activity small' }, el('span', { class: 'live-label', text: '↳ ' }),
      [turnText(r.turn, now), r.activity].filter(Boolean).join(' · ') || 'starting…')))));
}

function dots(job, now) {
  const runs = job.runs;
  if (!runs.length) return el('span', { class: 'muted small', text: 'no runs yet' });
  const failed = runs.filter(r => r.status === 'failed').length;
  const live = runs.filter(r => r.status === 'running').length;
  const done = runs.length - live;
  const strip = el('span', { class: 'run-dots', role: 'img', 'aria-label': `last ${runs.length} runs: ${failed ? failed + ' failed' : 'all ok'}` },
    runs.map(r => tip(el('span', { class: `run-dot tone-${RUN_TONE[r.status] || 'neutral'}${r.status === 'running' ? ' live' : ''}`, text: RUN_ICON[r.status] || '?' }),
      () => [when(r.started_at, now), `${r.status === 'ok' ? 'Completed' : r.status === 'failed' ? 'Failed' : r.status === 'running' ? 'Running' : 'Unknown'}${r.end_reason && r.status === 'failed' ? ': ' + r.end_reason : ''}`,
        `Took ${duration(r.duration_s)}`, `${num(r.tokens)} tokens · ${num(r.tool_calls)} tool calls`])));
  return el('span', { class: 'run-strip' }, strip,
    el('span', { class: 'muted small', text: [live ? `${live} running` : null,
      done ? (failed ? `${failed} of last ${done} failed` : `last ${done} ok`) : null].filter(Boolean).join(' · ') }));
}

function jobsCard(jobsData, now, rerender) {
  const jobs = jobsData.jobs;
  if (!jobsData.jobs_file) return card('Jobs', empty("No jobs file found (cron/jobs.json next to Hermes's database)."), { wide: true });
  if (!jobs.length) return card('Jobs', empty('No scheduled jobs.'), { wide: true });
  const head = el('div', { class: 'job-row job-head', 'aria-hidden': 'true' },
    ['Status', 'Job', 'Schedule', 'Next run', 'Last run', 'Recent runs'].map(t => el('span', { text: t })));
  const rows = jobs.map(j => {
    const open = ui.open.has(j.id);
    const [tone, label] = JOB_STATUS[j.status] || ['unknown', j.status];
    // Last finished run: a run in progress is already shown by the status and "Running now".
    const last = [...j.runs].reverse().find(r => r.status !== 'running');
    const row = el('button', {
      type: 'button', class: 'job-row', 'aria-expanded': open ? 'true' : 'false',
      onclick: () => { open ? ui.open.delete(j.id) : ui.open.add(j.id); rerender(); },
    },
    el('span', {}, pill(tone, label)),
    el('span', { class: 'job-name' }, el('span', { class: 'strong', text: j.name || j.id }),
      el('span', { class: 'muted small', text: [j.deliver ? `→ ${j.deliver}` : null,
        j.repeat_times ? `run ${j.repeat_completed || 0} of ${j.repeat_times}` : null].filter(Boolean).join(' · ') })),
    el('span', { 'data-label': 'Schedule', text: j.schedule || j.expr }),
    el('span', { 'data-label': 'Next run' }, j.next_run_at
      ? [el('span', { text: when(j.next_run_at, now) }), el('span', { class: 'muted small', text: until(j.next_run_at, now) })]
      : el('span', { class: 'muted', text: j.status === 'paused' ? 'paused' : '–' })),
    el('span', { 'data-label': 'Last run' }, last
      ? [el('span', { text: ago(last.started_at, now) }), el('span', { class: 'muted small', text: `${duration(last.duration_s)} · ${num(last.tokens)} tok` })]
      : el('span', { class: 'muted', text: '–' })),
    el('span', { 'data-label': 'Recent runs' }, dots(j, now)));
    const item = el('div', { class: 'job' + (j.status === 'failing' ? ' is-failing' : '') }, row);
    if (open) item.append(details(j, now));
    return item;
  });
  const note = jobsData.other_runs ? `${jobsData.other_runs} runs in the last 30 days belong to jobs that no longer exist` : 'click a job for details';
  return card('Jobs', el('div', { class: 'jobs' }, head, rows), { wide: true, note });
}

function details(j, now) {
  const rows = [
    ['Schedule', `${j.schedule}${j.schedule !== j.expr ? ` (${j.expr})` : ''}${j.timezone ? ' · ' + j.timezone : ''}`],
    ['State', j.state || '–'],
    ...(j.paused_at ? [['Paused', `${ago(j.paused_at, now)}${j.paused_reason ? ' · ' + j.paused_reason : ''}`]] : []),
    ['Delivers to', j.deliver || '–'],
    ['Last run', j.last_run_at ? `${when(j.last_run_at, now)}${j.lateness_s != null ? ` · started ${Math.round(j.lateness_s)} s after schedule (${j.dispatch_kind || 'n/a'})` : ''}` : '–'],
    ...(j.repeat_times ? [['Runs', `${j.repeat_completed || 0} of ${j.repeat_times}`]] : []),
    ...(j.failure_streak ? [['Failures in a row', String(j.failure_streak)]] : []),
    ...(j.last_error ? [['Last error', j.last_error]] : []),
    ...(j.last_delivery_error ? [['Delivery error', j.last_delivery_error]] : []),
    ['Skills', j.skills ? String(j.skills) : 'none'],
    ['Model', j.model || 'Hermes default'],
    ['Runs in last 30 days', String(j.runs_total)],
    ['Job id', j.id],
  ];
  return el('dl', { class: 'details' }, rows.map(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
}
