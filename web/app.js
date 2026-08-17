/* 页面数据层。
 *
 * 每 1.5 秒把 /api/roles、/api/activity、/api/runtime、/api/town、/api/models 拉一遍，
 * 然后同一份 state 同时喂给：Phaser 小镇、五个侧栏页面、右下角聊天窗。切页只是
 * 切 CSS class，不重新拉数据。
 */

import {mountTown} from './game/town-scene.js';
import {ChatWindow} from './chat.js';

const state = {
  roles: [],
  events: [],
  runtime: {},
  town: {},
  models: {profiles: {}, roles: [], catalog: []},
  modelCatalogLoaded: false,
  requestPending: false,
  replay: {runId: '', step: 0},
};

/* 建筑名与图标。坐标交给 Phaser 场景，这里只留展示用的名字——两处各管一件事，
 * 免得同一个布局被写两遍还对不上。 */
const townLayout = {
  job_scout: {icon: '📡', place: 'Scout Outpost'},
  jd_analyst: {icon: '🔎', place: 'JD Lab'},
  job_knowledge_curator: {icon: '📚', place: 'Knowledge Library'},
  resume_strategist: {icon: '📝', place: 'Resume Workshop'},
  portfolio_coach: {icon: '🛠️', place: 'Portfolio Garage'},
  interview_coach: {icon: '🎙️', place: 'Interview Arena'},
  judge: {icon: '⚖️', place: 'Evidence Court'},
};

// Event kinds that mean a role is doing work, patrol shifts included.
const roleWorkKinds = [
  'agent_started', 'agent_completed',
  'daily_push_started', 'daily_push_completed',
  'patrol_started', 'patrol_completed',
];
const runningKinds = ['agent_started', 'daily_push_started', 'patrol_started'];
const completedKinds = ['agent_completed', 'daily_push_completed', 'patrol_completed'];
const statusLabels = {
  disabled: ['已暂停', 'DISABLED'], idle: ['待命', 'IDLE'],
  queued: ['等待调度', 'QUEUED'], running: ['工作中', 'RUNNING'],
  ok: ['已完成', 'DONE'], completed: ['已完成', 'DONE'],
  error: ['失败', 'ERROR'], timeout: ['超时', 'TIMEOUT'],
};
const statusLabel = (status, upper = false) =>
  (statusLabels[status] || [status, String(status).toUpperCase()])[upper ? 1 : 0];
const routeLabels = {dispatch: 'PLAZA 分派', handoff: '证据交接', review: '送审'};
const shiftLabels = {working: '巡检中', standby: '在岗待命', offline: '未在岗'};

let selectedTownRole = null;

const $ = selector => document.querySelector(selector);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
const short = (value, size = 160) =>
  value && value.length > size ? `${value.slice(0, size - 1)}…` : (value || '');
const fmtMs = ms => ms == null
  ? '—'
  : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
const fmtTownTime = value => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString('zh-CN', {hour12: false});
};
const nameOf = roleId => roleId === 'plaza'
  ? 'LangGraph Plaza'
  : (state.roles.find(role => role.role_id === roleId)?.display_name || roleId);

/* ── 侧栏页面切换 ──────────────────────────────────────── */

const PAGES = ['town', 'graph', 'roles', 'memory', 'patrol', 'replay'];
let currentPage = 'town';
let town = null;

function showPage(page) {
  if (!PAGES.includes(page)) page = 'town';
  currentPage = page;
  for (const item of document.querySelectorAll('.nav-item')) {
    item.classList.toggle('active', item.dataset.page === page);
  }
  for (const section of document.querySelectorAll('.page')) {
    section.classList.toggle('active', section.dataset.page === page);
  }
  // 画布在 display:none 里量不到尺寸，回到小镇页要让 Phaser 重新量一次，
  // 否则地图会停在切走前的宽高。
  //
  // `ready` 必须判：首屏这个函数（按 hash 决定初始页）跑在 Phaser boot 之前，
  // 那时 scene 还没有 cameras.main，直接调 fitCamera 会抛异常，把后面的
  // refresh() 一起带走——表现就是地图画出来了但一个角色都没有。
  if (page === 'town' && town?.scene?.ready) {
    town.game.scale.refresh();
    town.scene.fitCamera();
  }
}

for (const item of document.querySelectorAll('.nav-item')) {
  item.addEventListener('click', () => {
    // 页面记在 hash 里：演示时可以直接把「路由与任务图」的链接发给别人，刷新也
    // 不会被弹回小镇。
    location.hash = item.dataset.page;
  });
}
window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));

/* ── 运行分组与回放 ────────────────────────────────────── */

function groupRuns(events) {
  const runs = new Map();
  for (const event of events) {
    // A heartbeat is the watcher proving it is alive, not a run. Grouping it
    // would put a fake newest "run" in front of the real one every 30 s.
    if (event.kind === 'heartbeat') continue;
    if (!runs.has(event.run_id)) runs.set(event.run_id, []);
    runs.get(event.run_id).push(event);
  }
  return [...runs.entries()].map(([id, values]) => {
    values.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    return {id, events: values, first: values[0], last: values.at(-1)};
  }).sort((a, b) => b.last.timestamp.localeCompare(a.last.timestamp));
}

function runForDisplay(runs) {
  const source = state.replay.runId
    ? runs.find(run => run.id === state.replay.runId)
    : liveRun(runs);
  if (!source || !state.replay.runId) return source;
  const step = Math.max(
    1,
    Math.min(
      state.town.replay?.step || state.replay.step || source.events.length,
      source.events.length,
    ),
  );
  const events = source.events.slice(0, step);
  return {...source, events, first: events[0], last: events.at(-1)};
}

/* 后台跑的 run 是背景噪音，不是主舞台。巡检每几分钟起一个单角色 run，日报兜底生成
 * 会连着起七个，而页面只有一个「当前 run」的位置：按时间排序的话，用户的协作 run 跑
 * 到一半就会被顶掉——已完成任务从 2/7 跳回 1/1、模型调用清零、小镇上只剩一个角色亮
 * 着。心跳早就因为同一个理由被挡在 groupRuns 之外，这些 run 只是换了个形状的同一个
 * bug。巡检有自己的「常驻巡检」页，所以这里只在还没有任何真实任务时才让它上台，否则
 * 页面会是空的。 */
function isBackgroundRun(run) {
  return run.events.some(event =>
    event.orchestrator === 'always_on'
    || event.metrics?.origin === 'patrol'
    || event.metrics?.origin === 'schedule'
    || event.kind.startsWith('patrol_'));
}

function liveRun(runs) {
  return runs.find(run => !isBackgroundRun(run)) || runs[0];
}

/* 两种后台 run 的说法不能混：一个是每几分钟一次的岗位巡检，一个是早上没人写日报时
 * 角色自己动笔。都写成「常驻巡检」会让人以为定时推送没跑。 */
function backgroundRunLabel(run, anyCompleted) {
  const writingBrief = run.events.some(event => event.metrics?.origin === 'schedule');
  if (writingBrief) {
    return anyCompleted ? '日报已写好一部分' : '正在现场撰写今天的日报';
  }
  return anyCompleted ? '常驻巡检已完成一轮' : '常驻巡检进行中';
}

function renderReplayControls(runs) {
  const select = $('#town-run-select');
  const selectedExists = runs.some(run => run.id === state.replay.runId);
  if (state.replay.runId && !selectedExists) {
    state.replay.runId = '';
    state.replay.step = 0;
  }
  select.innerHTML = '<option value="">LIVE · 实时小镇</option>' + runs.slice(0, 12).map(run =>
    `<option value="${escapeHtml(run.id)}">REPLAY · ${escapeHtml(run.id.slice(0, 8))} · ${run.events.length}步</option>`
  ).join('');
  select.value = state.replay.runId;
  const activeRun = runs.find(run => run.id === state.replay.runId);
  const slider = $('#town-replay-slider');
  const step = activeRun
    ? Math.max(1, Math.min(state.town.replay?.step || state.replay.step || activeRun.events.length, activeRun.events.length))
    : 1;
  slider.disabled = !activeRun;
  slider.max = String(activeRun?.events.length || 1);
  slider.value = String(step);
  $('#town-replay-step').textContent = activeRun ? `${step}/${activeRun.events.length}` : 'LIVE';
  $('#town-live-button').disabled = !activeRun;
}

/* ── 角色状态 ──────────────────────────────────────────── */

function roleState(role, run) {
  if (!role.enabled) {
    return {status: 'disabled', phase: '—', model: '—', latency: null, output: '角色已暂停，不参与自动路由'};
  }
  if (!run) return {status: 'idle', phase: '—', model: '—', latency: null, output: role.goal};
  const events = run.events.filter(event =>
    event.role_id === role.role_id && roleWorkKinds.includes(event.kind)
  );
  const last = events.at(-1);
  if (!last) {
    const selected = run.events.some(event =>
      (event.selected_role_ids || []).includes(role.role_id)
    );
    return {status: selected ? 'queued' : 'idle', phase: '—', model: '—', latency: null, output: role.goal};
  }
  const running = runningKinds.includes(last.kind);
  return {
    status: running ? 'running' : last.status,
    phase: last.phase,
    model: last.model || 'pending',
    latency: last.latency_ms,
    output: last.kind === 'patrol_started'
      ? `常驻巡检中：${short(last.query_excerpt || '核验真实岗位与链接', 90)}`
      : (last.output_excerpt || last.error || role.goal),
  };
}

function layoutFor(role) {
  const fallback = townLayout[role.role_id] || {icon: '🏠', place: role.display_name};
  return {
    icon: role.town_icon || fallback.icon,
    place: role.town_place || fallback.place,
  };
}

function activePhase(run) {
  if (!run) return 'idle';
  if (run.events.some(event => event.kind === 'run_completed')) return 'completed';
  const started = run.events.filter(event => event.kind === 'phase_started').at(-1);
  if (started) return started.phase;
  if (run.events.some(event => event.kind.startsWith('patrol_'))) return 'patrol';
  return run.events.some(event => event.kind === 'route_completed') ? 'discovery' : 'route';
}

function resolvedModelFor(role) {
  return state.models.roles?.find(item => item.role_id === role.role_id)?.resolved_model
    || role.model
    || role.model_profile
    || 'default';
}

// Reasoning depth, per role. Separate from the model because a strong model at
// "medium" often beats a weaker one at "high" — and because leaving it empty is
// meaningful: Haiku rejects the parameter, so "继承" has to stay reachable.
const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];

function effortOptions(current) {
  return [['', '继承'], ...EFFORT_LEVELS.map(level => [level, level])]
    .map(([value, label]) =>
      `<option value="${escapeHtml(value)}"${(current || '') === value ? ' selected' : ''}>${escapeHtml(label)}</option>`
    ).join('');
}

/* ── 路由 / 班次 / 任务图 ──────────────────────────────── */

function routesFor(run) {
  return (state.town.routes || []).filter(route =>
    !run || !route.run_id || route.run_id === run.id
  );
}

function renderRouting(routes) {
  const feed = $('#town-routing');
  $('#town-routing-count').textContent = `${routes.length} 条`;
  feed.innerHTML = routes.length ? routes.slice(-14).reverse().map(route =>
    `<div class="town-entry"><time>${escapeHtml(fmtTownTime(route.timestamp))}</time>
      <code class="route-kind ${escapeHtml(route.kind)}">${escapeHtml(routeLabels[route.kind] || route.kind)}</code>
      <span>${escapeHtml(nameOf(route.source_role_id))} → ${escapeHtml(nameOf(route.target_role_id))}${
        route.summary ? '：' + escapeHtml(short(route.summary, 96)) : ''
      }</span></div>`
  ).join('') : `<div class="town-memory-empty">${
    // 常驻巡检是单角色定时任务，本来就不经过 Plaza 分派，写「还没有路由」会让人
    // 以为是页面坏了。
    state.town.current_phase === 'patrol'
      ? '本轮是常驻巡检（单角色定时任务），没有多角色路由。启动一次团队任务就会有。'
      : '还没有真实发生的路由。'
  }</div>`;
}

function fmtAgo(seconds) {
  if (seconds == null) return '—';
  if (seconds < 90) return `${Math.round(seconds)} 秒`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟`;
  return `${(seconds / 3600).toFixed(1)} 小时`;
}

function renderShifts() {
  const shifts = state.town.shifts || [];
  const banner = $('#town-shift');
  if (!shifts.length) {
    banner.innerHTML = '<span class="shift-empty">还没有常驻班次记录；启动 job-agent-watch 或打开 API 服务即可开工。</span>';
    return;
  }
  banner.innerHTML = shifts.map(shift => {
    const role = state.roles.find(item => item.role_id === shift.role_id);
    const icon = role?.town_icon || townLayout[shift.role_id]?.icon || '🛰️';
    const parts = [`已巡检 ${shift.patrols} 轮`];
    if (shift.state === 'working') parts.push('本轮进行中');
    else if (shift.seconds_since_last_patrol != null) {
      parts.push(`上一轮 ${fmtAgo(shift.seconds_since_last_patrol)}前`);
    }
    if (shift.state !== 'working' && shift.next_patrol_in_seconds != null) {
      parts.push(`下一轮约 ${fmtAgo(shift.next_patrol_in_seconds)}后`);
    }
    if (shift.state === 'offline' && shift.heartbeat_age_seconds != null) {
      parts.push(`心跳停了 ${fmtAgo(shift.heartbeat_age_seconds)}`);
    }
    if (shift.consecutive_failures) parts.push(`连续失败 ${shift.consecutive_failures} 次`);
    return `<span class="shift-card ${escapeHtml(shift.state)}" title="${escapeHtml(short(shift.last_summary, 180))}">
      <i class="shift-dot"></i><b>${escapeHtml(icon)} ${escapeHtml(shift.display_name)}</b>
      ${escapeHtml(shiftLabels[shift.state] || shift.state)} · ${escapeHtml(parts.join(' · '))}</span>`;
  }).join('');
}

/* 巡检页的事件日志。
 *
 * 只认 patrol_* 和 daily_push_*：班次卡只有一行汇总，看不出上一轮到底核了什么。
 * 这些 kind 本来就写在 activity.jsonl 里，所以这一页不需要新的后端接口。
 */
const patrolKinds = {
  patrol_started: '巡检开始',
  patrol_completed: '巡检完成',
  daily_push_started: '日报开始',
  daily_push_completed: '日报完成',
};

function renderPatrolLog() {
  const rows = state.events.filter(event => patrolKinds[event.kind]).slice(-40).reverse();
  $('#patrol-log').innerHTML = rows.length ? rows.map(event => {
    const text = event.kind.endsWith('_started')
      ? (event.query_excerpt || '本轮开始')
      : (event.output_excerpt || event.error || '—');
    const label = patrolKinds[event.kind]
      + (event.kind.endsWith('_completed') && event.status && event.status !== 'ok'
        ? ` · ${event.status}` : '');
    return `<div class="town-entry"><time>${escapeHtml(fmtTownTime(event.timestamp))}</time>
      <code>${escapeHtml(label)}</code>
      <span>${escapeHtml(nameOf(event.role_id))}：${escapeHtml(short(text, 150))}</span></div>`;
  }).join('') : '<div class="town-memory-empty">还没有巡检事件；启动 job-agent-watch 就会有。</div>';
}

function renderTaskGraph(graph) {
  if (!graph) {
    $('#task-graph-title').textContent = '等待任务图';
    $('#task-graph-query').textContent = '启动一次团队任务或等待每日求职日报。';
    $('#task-graph-status').textContent = 'QUEUED';
    $('#task-graph-progress-label').textContent = '0%';
    $('#task-graph-progress').style.width = '0%';
    $('#task-lanes').innerHTML = '<div class="empty">还没有任务依赖图。</div>';
    return;
  }
  $('#task-graph-title').textContent = `${graph.title} · ${graph.run_id.slice(0, 8)}`;
  $('#task-graph-query').textContent = short(graph.query, 220);
  $('#task-graph-status').textContent = `${String(graph.status).toUpperCase()} · ${graph.source}`;
  $('#task-graph-progress-label').textContent = `${graph.progress}%`;
  $('#task-graph-progress').style.width = `${graph.progress}%`;
  const order = ['delivery', 'discovery', 'analysis', 'action', 'judge'];
  const labels = {
    delivery: 'DAILY DELIVERY',
    discovery: 'DISCOVERY',
    analysis: 'ANALYSIS',
    action: 'ACTION',
    judge: 'JUDGE',
  };
  const phases = order.filter(phase => graph.tasks.some(task => task.phase === phase));
  $('#task-lanes').innerHTML = phases.map(phase => {
    const tasks = graph.tasks.filter(task => task.phase === phase);
    return `<article class="task-lane"><h3>${labels[phase] || phase}</h3>${tasks.map(task => {
      const dependencies = task.depends_on?.length
        ? `<div class="task-deps"><code>DEPENDS ←</code>${task.depends_on.map(item => `<code>${escapeHtml(item)}</code>`).join('')}</div>`
        : '<div class="task-deps"><code>NO DEPENDENCY</code></div>';
      const criteria = (task.acceptance_criteria || []).slice(0, 3).map(item =>
        `<li>${escapeHtml(item)}</li>`
      ).join('');
      return `<section class="task-card ${escapeHtml(task.status)}">
        <div class="task-card-head"><h4>${escapeHtml(task.title)}</h4><span class="pill ${escapeHtml(task.status)}">${escapeHtml(task.status)}</span></div>
        <p>${escapeHtml(short(task.description, 180))}</p>
        ${dependencies}
        <ul class="task-criteria">${criteria}</ul>
        <div class="mini-progress"><i style="width:${Number(task.progress) || 0}%"></i></div>
        <footer><span>${escapeHtml(task.role_id)}</span><span>${escapeHtml(task.model || 'pending')}</span><span>${Number(task.progress) || 0}%</span></footer>
      </section>`;
    }).join('')}</article>`;
  }).join('');
}

/* ── 小镇 ──────────────────────────────────────────────── */

function renderTown(run) {
  const activeRole = state.roles.find(role => roleState(role, run).status === 'running');
  const currentHandoff = state.town.handoffs
    ?.filter(handoff => handoff.run_id === run?.id)
    .at(-1);
  const latestRunRoleId = run?.events.filter(event => event.role_id).at(-1)?.role_id;
  const focusRoleId = selectedTownRole
    || activeRole?.role_id
    || currentHandoff?.target_role_ids?.at(-1)
    || latestRunRoleId
    || state.roles[0]?.role_id;

  const routes = routesFor(run);
  renderRouting(routes);
  renderShifts();
  renderPatrolLog();

  // 场景只认「谁在什么状态、说什么」；位置、瓦片、动画都由场景自己决定。
  town?.scene.applySnapshot({
    roles: state.roles,
    agents: state.roles.map(role => {
      const value = roleState(role, run);
      return {
        role_id: role.role_id,
        status: value.status,
        speech: value.status === 'running'
          ? `正在处理：${short(value.output, 82)}`
          : short(value.output, 82),
      };
    }),
    routes,
    focusRoleId,
  });

  const focusRole = state.roles.find(role => role.role_id === focusRoleId);
  if (!focusRole) return;
  const focus = roleState(focusRole, run);
  const townAgent = state.town.agents?.find(agent => agent.role_id === focusRoleId);
  const place = townAgent?.place || layoutFor(focusRole).place;
  $('#town-detail').innerHTML = `<strong>${escapeHtml(place)} · ${escapeHtml(focusRole.display_name)}</strong>
    <span>${escapeHtml(short(townAgent?.current_action || focus.output, 210))}</span>
    <code>${escapeHtml(statusLabel(focus.status))} · ${escapeHtml(focus.model)} · ${fmtMs(focus.latency)}</code>`;
  $('#town-clock').textContent = state.town.town_time || 'RUNTIME VIEW';
  $('#town-memory-role').textContent = focusRole.role_id;

  const task = state.town.task_graph?.tasks?.find(item => item.role_id === focusRole.role_id);
  $('#town-task-summary').style.display = task ? 'block' : 'none';
  $('#town-task-summary').innerHTML = task
    ? `<strong>${escapeHtml(task.title)} · ${Number(task.progress) || 0}%</strong>
       <span>${escapeHtml(short(task.description, 260))}</span>
       <div class="task-deps">${task.depends_on?.length
         ? `<code>DEPENDS ←</code>${task.depends_on.map(item => `<code>${escapeHtml(item)}</code>`).join('')}`
         : '<code>NO DEPENDENCY</code>'}</div>`
    : '';
  $('#town-schedule').innerHTML = (townAgent?.schedule || []).map(item =>
    `<li>${escapeHtml(item)}</li>`
  ).join('');
  const reflection = townAgent?.reflection || '';
  $('#town-reflection').style.display = reflection ? 'block' : 'none';
  $('#town-reflection').textContent = reflection ? `反思：${reflection}` : '';
  const memories = townAgent?.memory_stream?.length
    ? townAgent.memory_stream
    : (townAgent?.memories || []);
  $('#town-memory').innerHTML = memories.length ? memories.slice().reverse().map(memory =>
    `<div class="town-entry"><time>${escapeHtml(fmtTownTime(memory.timestamp))}</time>
      <code>${escapeHtml(memory.kind)}${memory.importance == null ? '' : ' · ' + memory.importance.toFixed(2)}</code><span>${escapeHtml(short(memory.text, 145))}</span></div>`
  ).join('') : '<div class="town-memory-empty">该角色还没有形成运行记忆。</div>';

  const timeline = state.town.timeline || [];
  $('#town-timeline').innerHTML = timeline.length ? timeline.slice(-30).reverse().map(memory =>
    `<div class="town-entry"><time>${escapeHtml(fmtTownTime(memory.timestamp))}</time>
      <code>${escapeHtml(memory.phase)}</code><span>${escapeHtml(short(memory.text, 145))}</span></div>`
  ).join('') : '<div class="town-memory-empty">还没有运行事件。</div>';
}

/* ── 总渲染 ────────────────────────────────────────────── */

function render() {
  const runs = groupRuns(state.events);
  renderReplayControls(runs);
  const current = runForDisplay(runs);
  const completeEvent = current?.events.findLast(event => event.kind === 'run_completed');
  const failedEvent = current?.events.findLast(event => event.kind === 'run_failed');
  const completedAgents = current?.events.filter(event =>
    completedKinds.includes(event.kind)
  ) || [];
  const partialFailure = completedAgents.some(event => !['ok', 'completed'].includes(event.status));
  const phase = failedEvent ? 'failed' : activePhase(current);
  const okCount = completedAgents.filter(event => event.status === 'ok').length;
  const expectedRoleIds = new Set(
    current?.events.flatMap(event => event.selected_role_ids || []) || []
  );
  completedAgents.forEach(event => expectedRoleIds.add(event.role_id));
  const taskGraph = state.town.task_graph;
  const completedTasks = taskGraph?.tasks?.filter(task => task.status === 'completed').length || 0;
  const metrics = completeEvent?.metrics || {};
  const firstTime = current ? Date.parse(current.first.timestamp) : 0;
  const lastTime = current ? Date.parse((completeEvent || failedEvent || current.last).timestamp) : 0;
  const elapsed = current ? lastTime - firstTime : null;

  const runtimeMode = state.replay.runId ? 'replay' : 'live';
  $('#runtime-name').textContent = `${state.runtime.orchestrator || 'Runtime'} · ${state.roles.length} roles · ${runtimeMode}`;
  $('#graph-mermaid').textContent = state.runtime.graph_mermaid
    || '当前编排器没有导出图定义（asyncio baseline 不是状态图）。';
  $('#run-id').textContent = current ? `RUN ${current.id.slice(0, 8).toUpperCase()}` : 'NO RUN YET';
  $('#run-state').textContent = state.replay.runId
    ? `轨迹回放 ${state.town.replay?.step || state.replay.step}/${state.town.replay?.total_steps || current?.events.length || 0}`
    : failedEvent ? '运行失败'
      // 后台 run 只有在没有任何真实任务时才会显示（见 liveRun）。认 run 而不是认
      // phase：巡检派下去的那个 run 走的是 action 阶段，phase 里看不出它是后台的。
      : current && isBackgroundRun(current)
        ? backgroundRunLabel(current, completedAgents.length > 0)
      : completeEvent ? (partialFailure ? '运行完成（含失败）' : '运行完成')
      : current ? 'Agent 正在协作' : '等待任务';
  $('#run-query').textContent = current?.first.query_excerpt || '从飞书 @角色，或用右下角聊天窗启动一次团队任务。';
  $('#m-phase').textContent = phase.toUpperCase();
  $('#m-complete').textContent = taskGraph
    ? `${completedTasks} / ${taskGraph.tasks.length}`
    : `${okCount} / ${expectedRoleIds.size || state.roles.filter(role => role.enabled).length}`;
  $('#m-calls').textContent = metrics.model_calls ?? completedAgents.length;
  $('#m-latency').textContent = fmtMs(metrics.wall_latency_ms ?? elapsed);
  $('#m-speedup').textContent = metrics.parallel_speedup_estimate
    ? `${metrics.parallel_speedup_estimate.toFixed(2)}×` : '—';
  renderTown(current);
  renderTaskGraph(taskGraph);
  chat.sync(current?.events || [], current?.id || '');

  const touchedPhases = new Set(
    current?.events
      .filter(event => event.kind === 'phase_started')
      .map(event => event.phase) || []
  );
  if (current?.events.some(event => event.kind === 'route_completed')) touchedPhases.add('route');
  for (const element of document.querySelectorAll('.phase')) {
    element.classList.remove('active', 'done');
    const target = element.dataset.phase;
    if (phase === 'completed' && touchedPhases.has(target)) element.classList.add('done');
    else if (target !== phase && touchedPhases.has(target)) element.classList.add('done');
    else if (target === phase) element.classList.add('active');
  }

  $('#agent-caption').textContent = state.replay.runId
    ? `${state.roles.length} 个逻辑角色 · 历史事件回放`
    : `${state.roles.length} 个逻辑角色 · 状态每 1.5 秒刷新`;
  $('#agents').innerHTML = state.roles.map((role, index) => {
    const value = roleState(role, current);
    const layout = layoutFor(role);
    return `<article class="agent ${escapeHtml(value.status)}">
      <div class="agent-top"><span class="agent-index">AGENT ${String(index + 1).padStart(2, '0')}</span><span class="pill ${escapeHtml(value.status)}">${escapeHtml(statusLabel(value.status, true))}</span></div>
      <h3>${escapeHtml(layout.icon)} ${escapeHtml(role.display_name)}</h3>
      <span class="role-id">${escapeHtml(role.role_id)} · ${escapeHtml(layout.place)}</span>
      <p class="agent-output">${escapeHtml(short(value.output))}</p>
      <div class="agent-model-config">
        <input id="model-${escapeHtml(role.role_id)}" list="model-options" value="${escapeHtml(role.model || '')}" placeholder="${escapeHtml(resolvedModelFor(role))}" aria-label="${escapeHtml(role.display_name)}模型">
        <select id="effort-${escapeHtml(role.role_id)}" aria-label="${escapeHtml(role.display_name)}思考深度">
          ${effortOptions(role.effort)}
        </select>
        <button class="secondary" data-model-role="${escapeHtml(role.role_id)}">应用</button>
      </div>
      <button class="secondary" data-toggle-role="${escapeHtml(role.role_id)}" data-enable="${role.enabled ? 'false' : 'true'}">${role.enabled ? '暂停角色' : '启用角色'}</button>
      <div class="agent-meta"><span>${escapeHtml(value.phase)}</span><span>${escapeHtml(value.model === '—' ? resolvedModelFor(role) : value.model)}</span><span>${fmtMs(value.latency)}</span></div>
    </article>`;
  }).join('');

  $('#runs').innerHTML = runs.length ? runs.slice(0, 12).map(run => {
    const done = run.events.find(event => event.kind === 'run_completed');
    const fail = run.events.find(event => event.kind === 'run_failed');
    const agents = run.events.filter(event => completedKinds.includes(event.kind));
    const partial = agents.some(event => !['ok', 'completed'].includes(event.status));
    const duration = done?.metrics?.wall_latency_ms
      ?? (Date.parse(run.last.timestamp) - Date.parse(run.first.timestamp));
    return `<tr>
      <td class="mono"><button class="secondary" data-replay-run="${escapeHtml(run.id)}">${escapeHtml(run.id.slice(0, 8))}</button></td>
      <td>${escapeHtml(run.first.orchestrator)}</td>
      <td>${escapeHtml(run.first.mode || '—')}</td>
      <td>${fail ? 'error' : done ? (partial ? 'partial' : 'completed') : 'running'}</td>
      <td>${agents.length}</td>
      <td class="mono">${fmtMs(duration)}</td>
      <td>${escapeHtml(short(run.first.query_excerpt, 70))}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="7" class="empty">暂无轨迹</td></tr>';
}

/* ── 拉数据 ────────────────────────────────────────────── */

async function refresh() {
  try {
    const townParams = new URLSearchParams();
    if (state.replay.runId) {
      townParams.set('run_id', state.replay.runId);
      if (state.replay.step) townParams.set('step', String(state.replay.step));
    }
    const townUrl = `/api/town${townParams.size ? `?${townParams}` : ''}`;
    const modelUrl = state.modelCatalogLoaded ? '/api/models' : '/api/models?catalog=true';
    const [roles, events, runtime, town_, models] = await Promise.all([
      fetch('/api/roles').then(response => response.json()),
      fetch('/api/activity?limit=1000').then(response => response.json()),
      fetch('/api/runtime').then(response => response.json()),
      fetch(townUrl).then(response => {
        if (!response.ok) throw new Error(`Town ${response.status}`);
        return response.json();
      }),
      fetch(modelUrl).then(response => response.json()),
    ]);
    state.roles = roles;
    state.events = events;
    state.runtime = runtime;
    state.town = town_;
    state.models = {
      ...models,
      catalog: models.catalog?.length ? models.catalog : state.models.catalog,
    };
    state.modelCatalogLoaded = true;
    $('#model-options').innerHTML = (state.models.catalog || []).map(model =>
      `<option value="${escapeHtml(model)}"></option>`
    ).join('');
    render();
  } catch (error) {
    $('#runtime-name').textContent = `Runtime disconnected · ${error.message}`;
  }
}

/* ── 动作 ──────────────────────────────────────────────── */

async function selectTownRun(runId) {
  state.replay.runId = runId;
  const run = groupRuns(state.events).find(value => value.id === runId);
  state.replay.step = run?.events.length || 0;
  await refresh();
}

async function selectTownStep(value) {
  state.replay.step = Number(value);
  await refresh();
}

async function returnToLiveTown() {
  state.replay.runId = '';
  state.replay.step = 0;
  await refresh();
}

/* ── 图片附件 ──────────────────────────────────────────────
 *
 * 三个入口（粘贴 / 拖入 / 📎）都收敛到 `addFiles`，因为后端只认一种东西：
 * `RunRequest.images` 里的 `{media_type, data, source_name}`，data 是不带
 * `data:` 前缀的 base64。上限跟着后端走——9 张、单张 5 MB——在这里就拦掉，
 * 免得攒了半天再被 422 退回来。
 */
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
const MAX_IMAGES = 9;
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const attachments = [];

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取失败'));
    reader.onload = () => {
      // readAsDataURL 给的是 `data:image/png;base64,xxx`，后端只要逗号后面那截。
      const value = String(reader.result || '');
      const comma = value.indexOf(',');
      resolve(comma < 0 ? '' : value.slice(comma + 1));
    };
    reader.readAsDataURL(file);
  });
}

function renderAttachments() {
  const box = $('#chat-attach');
  box.hidden = attachments.length === 0;
  box.innerHTML = attachments.map((image, index) => {
    const kb = Math.round((image.data.length * 3) / 4 / 1024);
    const name = image.source_name || `图片${index + 1}`;
    return `<span class="chip">🖼️ ${escapeHtml(name)} · ${kb}KB`
      + `<button type="button" data-drop="${index}" aria-label="移除">×</button></span>`;
  }).join('');
}

async function addFiles(files) {
  const problems = [];
  for (const file of files) {
    if (attachments.length >= MAX_IMAGES) {
      problems.push(`最多 ${MAX_IMAGES} 张，${file.name || '这张'}已跳过`);
      continue;
    }
    if (!IMAGE_TYPES.includes(file.type)) {
      problems.push(`${file.name || '附件'}：只支持 PNG/JPEG/GIF/WebP`);
      continue;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      problems.push(`${file.name || '附件'}：${(file.size / 1048576).toFixed(1)}MB 超过 5MB`);
      continue;
    }
    try {
      const data = await readAsBase64(file);
      if (!data) throw new Error('内容为空');
      attachments.push({
        media_type: file.type,
        data,
        source_name: (file.name || '粘贴的图片').slice(0, 200),
      });
    } catch (error) {
      problems.push(`${file.name || '附件'}：${error.message}`);
    }
  }
  renderAttachments();
  // 静默丢图是最坏的结果：宁可在对话里说清楚哪张没进去。
  if (problems.length) chat.note(`附件问题：${problems.join('；')}`);
}

function takeAttachments() {
  const images = attachments.slice();
  attachments.length = 0;
  renderAttachments();
  return images;
}

async function runAgents() {
  if (state.requestPending) return;
  const button = $('#run-button');
  const query = $('#query').value.trim();
  if (!query) return;
  // 图片在发出去的那一刻就从待发区取走：这次运行带走它们，下一句话不该重复带上。
  const images = takeAttachments();
  state.requestPending = true;
  button.disabled = true;
  button.textContent = '运行中…';
  chat.echo(images.length ? `${query}\n（附 ${images.length} 张图片）` : query);
  $('#output').textContent = '任务已经进入 LangGraph，实时状态见小镇与对话。';
  try {
    const allRoles = $('#all-roles').checked
      ? state.roles.filter(role => role.role_id !== 'judge' && role.enabled).map(role => role.role_id)
      : [];
    const response = await fetch('/api/runs', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({
        query,
        requested_roles: allRoles,
        mode: $('#mode').value,
        use_judge: true,
        images,
      }),
    });
    const report = await response.json();
    if (!response.ok) throw new Error(JSON.stringify(report));
    $('#output').textContent = report.final_output || JSON.stringify(report, null, 2);
  } catch (error) {
    $('#output').textContent = `运行失败：${error.message}`;
    chat.note(`运行失败：${error.message}`);
    // 失败就把图片放回待发区，否则用户得重新截一遍图才能重试。
    attachments.push(...images.slice(0, MAX_IMAGES - attachments.length));
    renderAttachments();
  } finally {
    state.requestPending = false;
    button.disabled = false;
    button.textContent = '运行团队';
    await refresh();
  }
}

const roleTemplates = {
  bioinformatics_coach: {
    role_id: 'bioinformatics_coach', display_name: '生信算法教练',
    goal: '把生信算法岗位要求映射到可验证的项目证据',
    system_prompt: '围绕生信算法岗位提供知识、项目和面试建议；区分已有证据与待核验推断。',
    trigger_keywords: ['生信', 'GWAS', '单细胞'], tools: ['local_docs'], model_profile: 'knowledge',
    workflow_stage: 'action', enabled: true, town_place: '生信实验室', town_icon: '🧬',
    schedule: ['解析生信任务', '映射分析流程', '形成可验证证据'],
  },
  data_science_coach: {
    role_id: 'data_science_coach', display_name: '数据科学教练',
    goal: '把数据科学岗位要求转成分析方案和作品证据',
    system_prompt: '围绕 SQL、统计推断、实验设计和数据产品输出可验证建议，不虚构数据。',
    trigger_keywords: ['数据科学', 'SQL', 'AB实验'], tools: ['local_docs'], model_profile: 'default',
    workflow_stage: 'action', enabled: true, town_place: '数据观测站', town_icon: '📊',
    schedule: ['提炼业务问题', '设计分析方法', '定义验收指标'],
  },
  agent_eval_engineer: {
    role_id: 'agent_eval_engineer', display_name: 'Agent 评测工程师',
    goal: '设计 Agent、RAG 与工具调用的可复现评测方案',
    system_prompt: '输出 golden set、指标、失败分类、回归门禁和成本延迟约束。',
    trigger_keywords: ['Agent评测', 'RAG评测', 'golden set'], tools: ['local_docs'], model_profile: 'reliable',
    workflow_stage: 'action', enabled: true, town_place: '评测控制室', town_icon: '🧪',
    schedule: ['建立样例集', '定义指标', '执行回归门禁'],
  },
};

async function addRole() {
  return createRole({
    role_id: $('#role-id').value,
    display_name: $('#role-name').value,
    goal: $('#role-goal').value,
    system_prompt: $('#role-prompt').value,
    trigger_keywords: $('#role-keywords').value.split(',').map(value => value.trim()).filter(Boolean),
    tools: [],
    model_profile: 'default',
    model: $('#role-model').value.trim() || null,
    workflow_stage: $('#role-stage').value,
    enabled: true,
    town_place: $('#role-place').value || null,
    town_icon: $('#role-icon').value || '🏠',
    schedule: $('#role-schedule').value.split(',').map(value => value.trim()).filter(Boolean),
  });
}

async function addRoleFromTemplate() {
  const template = roleTemplates[$('#role-template').value];
  if (!template) {
    chat.note('请先选择一个角色模板。');
    return;
  }
  await createRole(template);
}

async function createRole(body) {
  const response = await fetch('/api/roles', {
    method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    chat.note(`添加角色失败：${JSON.stringify(payload)}`);
    return;
  }
  chat.note(`角色 ${body.display_name} 已启用，registry version=${payload.registry_version}`);
  await refresh();
}

async function toggleRole(roleId, enabled) {
  const response = await fetch(`/api/roles/${encodeURIComponent(roleId)}`, {
    method: 'PATCH', headers: {'content-type': 'application/json'}, body: JSON.stringify({enabled}),
  });
  const payload = await response.json();
  if (!response.ok) {
    chat.note(`更新角色失败：${JSON.stringify(payload)}`);
    return;
  }
  chat.note(`${payload.role.display_name} 已${enabled ? '启用' : '暂停'}，registry version=${payload.registry_version}`);
  await refresh();
}

async function configureRoleModel(roleId) {
  const input = document.getElementById(`model-${roleId}`);
  const model = input?.value.trim() || null;
  // Empty means "inherit the deployment default", which is a real setting, not
  // a missing one — send null rather than dropping the key.
  const effort = document.getElementById(`effort-${roleId}`)?.value || null;
  const response = await fetch(`/api/roles/${encodeURIComponent(roleId)}`, {
    method: 'PATCH',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({model, effort}),
  });
  const payload = await response.json();
  if (!response.ok) {
    chat.note(`模型配置失败：${JSON.stringify(payload)}`);
    return;
  }
  chat.note(`${payload.role.display_name} 已更新为 ${model || 'profile fallback'} · effort ${effort || '继承'}，下一次 @ 该角色立即生效。`);
  await refresh();
}

/* ── 事件绑定 ──────────────────────────────────────────── */

// 角色卡片和运行表格每 1.5 秒重建一次，所以按钮用委托绑定：直接 addEventListener
// 的话每次重建都会掉线，而 inline onclick 在 ES module 里拿不到函数。
document.addEventListener('click', event => {
  const target = event.target.closest('[data-model-role],[data-toggle-role],[data-replay-run]');
  if (!target) return;
  if (target.dataset.modelRole) configureRoleModel(target.dataset.modelRole);
  else if (target.dataset.toggleRole) {
    toggleRole(target.dataset.toggleRole, target.dataset.enable === 'true');
  } else if (target.dataset.replayRun) {
    selectTownRun(target.dataset.replayRun);
    showPage('town');
  }
});

$('#town-run-select').addEventListener('change', event => selectTownRun(event.target.value));
$('#town-replay-slider').addEventListener('change', event => selectTownStep(event.target.value));
$('#town-live-button').addEventListener('click', returnToLiveTown);
$('#btn-add-role').addEventListener('click', addRole);
$('#btn-add-template').addEventListener('click', addRoleFromTemplate);

const chat = new ChatWindow({nameOf, onSubmit: runAgents});

/* 三个图片入口。`addFiles` 里会 chat.note()，所以必须等 chat 建好再绑。 */
$('#chat-clip').addEventListener('click', () => $('#chat-files').click());
$('#chat-files').addEventListener('change', async event => {
  await addFiles(event.target.files || []);
  event.target.value = '';  // 同一个文件连选两次也要能触发 change。
});
$('#chat-attach').addEventListener('click', event => {
  const index = event.target.dataset?.drop;
  if (index == null) return;
  attachments.splice(Number(index), 1);
  renderAttachments();
});
$('#query').addEventListener('paste', event => {
  const files = [...(event.clipboardData?.items || [])]
    .filter(item => item.kind === 'file')
    .map(item => item.getAsFile())
    .filter(Boolean);
  // 只在真的截到图时拦下粘贴，否则复制一段 JD 文本会粘不进来。
  if (!files.length) return;
  event.preventDefault();
  addFiles(files);
});
// 整个聊天窗都是拖放目标：拖到输入框那一小条上太难瞄。
const chatPanel = $('#chat');
for (const type of ['dragover', 'dragenter']) {
  chatPanel.addEventListener(type, event => {
    if (!event.dataTransfer?.types?.includes('Files')) return;
    event.preventDefault();
    chatPanel.classList.add('dropping');
  });
}
for (const type of ['dragleave', 'drop']) {
  chatPanel.addEventListener(type, () => chatPanel.classList.remove('dropping'));
}
chatPanel.addEventListener('drop', event => {
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  addFiles(event.dataTransfer.files);
});

town = mountTown('town-canvas', roleId => {
  // 点建筑既是选中，也是把「记忆与反思」页切到那个角色。
  selectedTownRole = roleId;
  town.scene.focusOn(roleId);
  render();
});

showPage(location.hash.slice(1));
refresh();
setInterval(refresh, 1500);
