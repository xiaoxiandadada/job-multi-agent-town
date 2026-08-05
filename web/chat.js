/* 右下角的浮动聊天窗。
 *
 * 后端没有 SSE：`POST /api/runs` 只在整场跑完后返回一次 RunReport。所以这里不假装
 * 有流式——对话是从 `/api/activity` 轮询到的真实 ActivityEvent 逐条翻译出来的，
 * 按 `event_id` 去重后只做增量追加。看到的每一行都能在 activity.jsonl 里查到。
 */

const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

const short = (value, size = 220) => {
  const text = String(value ?? '');
  return text.length > size ? `${text.slice(0, size - 1)}…` : text;
};

const fmtTime = value => {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString('zh-CN', {hour12: false});
};

const fmtMs = ms => ms == null
  ? '—'
  : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;

/* 只有这些事件在对话里有意义。
 *
 * `phase_started` / `heartbeat` / `memory_retrieved` 之类是给时间线和班次看板用的，
 * 塞进对话只会把真正的角色发言冲掉，所以这里直接不认。
 */
const SPEAKERS = {
  run_started: 'route',
  route_completed: 'route',
  dispatch: 'route',
  intake_completed: 'chief',
  attachment_read: 'chief',
  closing_created: 'chief',
  agent_started: 'agent',
  agent_completed: 'agent',
  handoff: 'handoff',
  handoff_created: 'handoff',
  review: 'review',
  task_graph_created: 'route',
  patrol_started: 'agent',
  patrol_completed: 'agent',
  daily_push_started: 'agent',
  daily_push_completed: 'agent',
  reflection: 'agent',
  reflection_created: 'agent',
  run_completed: 'judge',
  run_failed: 'error',
};

export class ChatWindow {
  /**
   * @param {object} options
   * @param {(roleId: string) => string} options.nameOf 角色 id → 显示名
   * @param {() => Promise<void>} options.onSubmit 点「运行团队」时执行
   */
  constructor({nameOf, onSubmit}) {
    this.nameOf = nameOf;
    this.onSubmit = onSubmit;
    this.root = document.getElementById('chat');
    this.body = document.getElementById('chat-body');
    this.seen = new Set();
    this.runId = null;

    document.getElementById('chat-toggle').addEventListener('click', () => {
      this.root.classList.toggle('collapsed');
    });
    document.getElementById('run-button').addEventListener('click', () => {
      this.onSubmit();
    });
    // Cmd/Ctrl+Enter 提交：输入框是多行的，回车要留给换行。
    document.getElementById('query').addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        this.onSubmit();
      }
    });
  }

  /** 用户自己发出去的那条，本地回显——它不对应任何事件。 */
  echo(text) {
    this.append({
      cls: 'me',
      who: '我',
      text,
      time: new Date().toISOString(),
    });
  }

  note(text) {
    this.append({cls: '', who: '系统', text, time: new Date().toISOString()});
  }

  append({cls, who, text, time}) {
    const empty = this.body.querySelector('.chat-empty');
    if (empty) empty.remove();
    // 只有本来就贴着底部时才自动滚，免得把正在往上翻的人拽回去。
    const atBottom = this.body.scrollHeight - this.body.scrollTop
      - this.body.clientHeight < 40;
    const node = document.createElement('div');
    node.className = `chat-msg ${cls}`.trim();
    node.innerHTML = `<b>${escapeHtml(who)}</b><span>${escapeHtml(text)}</span>`
      + `<time>${escapeHtml(fmtTime(time))}</time>`;
    this.body.appendChild(node);
    if (atBottom) this.body.scrollTop = this.body.scrollHeight;
  }

  /** 事件 → 一句话。返回 null 表示这条事件不进对话。 */
  lineFor(event) {
    const cls = SPEAKERS[event.kind];
    if (!cls) return null;
    const name = event.role_id ? this.nameOf(event.role_id) : '';
    const selected = (event.selected_role_ids || []).map(id => this.nameOf(id));
    const targets = (event.target_role_ids || []).map(id => this.nameOf(id));
    const sources = (event.source_role_ids || []).map(id => this.nameOf(id));

    switch (event.kind) {
      case 'run_started':
        return {cls, who: 'LangGraph Plaza', text: `收到任务：${short(event.query_excerpt, 200)}`};
      // Chief of Staff 的三条：接单说明在角色开工之前就到，读图在中间，
      // 下一步在 Judge 之后——顺序由事件时间戳决定，这里只管翻译。
      case 'intake_completed':
        return {cls, who: 'Chief of Staff', text: short(event.output_excerpt, 420)};
      case 'attachment_read':
        return event.status === 'ok'
          ? {cls, who: 'Chief of Staff · 读图', text: short(event.output_excerpt, 420)}
          : {cls: 'error', who: 'Chief of Staff · 读图',
            text: `读图失败：${short(event.error, 220)}（图片改为直接交给角色）`};
      case 'closing_created':
        return event.status === 'ok'
          ? {cls, who: 'Chief of Staff · 下一步', text: short(event.output_excerpt, 420)}
          : {cls: 'error', who: 'Chief of Staff · 下一步', text: `收口失败：${short(event.error, 220)}`};
      case 'route_completed':
      case 'dispatch':
        return {
          cls,
          who: 'LangGraph Plaza',
          text: selected.length
            ? `分派给 ${selected.length} 个角色：${selected.join('、')}`
            : '没有匹配到角色，交给默认角色处理。',
        };
      case 'task_graph_created':
        return {cls, who: 'LangGraph Plaza', text: `任务图已生成：${short(event.task_title || event.query_excerpt, 160)}`};
      case 'agent_started':
        return {cls, who: name, text: `开始工作（${event.phase || '—'} · ${event.model || 'pending'}）`};
      case 'agent_completed':
        return event.status === 'ok'
          ? {cls, who: name, text: `${short(event.output_excerpt, 320)}\n—— ${fmtMs(event.latency_ms)}`}
          : {cls: 'error', who: name, text: `失败（${event.status}）：${short(event.error || event.output_excerpt, 220)}`};
      case 'handoff':
      case 'handoff_created':
        return {
          cls,
          who: '证据交接',
          text: `${sources.join('、') || name} → ${targets.join('、') || '下一阶段'}`
            + (event.output_excerpt ? `：${short(event.output_excerpt, 200)}` : ''),
        };
      case 'review':
        return {cls, who: '送审', text: `${sources.join('、') || name} → Judge${
          event.output_excerpt ? `：${short(event.output_excerpt, 200)}` : ''}`};
      case 'patrol_started':
        return {cls, who: name, text: `常驻巡检开始：${short(event.query_excerpt || '核验真实岗位与链接', 160)}`};
      case 'patrol_completed':
        return {cls: event.status === 'ok' ? cls : 'error', who: name,
          text: `巡检结束（${event.status}）：${short(event.output_excerpt || event.error, 220)}`};
      case 'daily_push_started':
        return {cls, who: name, text: '每日求职日报开始生成。'};
      case 'daily_push_completed':
        return {cls, who: name, text: `日报完成：${short(event.output_excerpt, 240)}`};
      case 'reflection':
      case 'reflection_created':
        return {cls, who: `${name} · 反思`, text: short(event.output_excerpt, 240)};
      case 'run_completed': {
        const metrics = event.metrics || {};
        const parts = [`端到端 ${fmtMs(metrics.wall_latency_ms ?? event.latency_ms)}`];
        if (metrics.model_calls != null) parts.push(`${metrics.model_calls} 次模型调用`);
        if (metrics.parallel_speedup_estimate) {
          parts.push(`并行加速约 ${metrics.parallel_speedup_estimate.toFixed(2)}×`);
        }
        return {cls, who: 'Judge / 运行收尾', text: `${short(event.output_excerpt, 300)}\n—— ${parts.join(' · ')}`};
      }
      case 'run_failed':
        return {cls, who: '运行失败', text: short(event.error || event.output_excerpt, 260)};
      default:
        return null;
    }
  }

  /**
   * @param {Array} events 当前展示的那次运行的事件（已按时间升序）
   * @param {string} runId 这些事件属于哪次运行
   */
  sync(events, runId) {
    // 换运行时插一条分隔，而不是清屏：清屏会把用户刚发出去的那条本地回显一起
    // 抹掉——它总是先于这次运行的第一个事件出现。
    if (runId !== this.runId) {
      this.runId = runId;
      this.seen.clear();
      if (runId) this.note(`—— RUN ${runId.slice(0, 8).toUpperCase()} ——`);
    }
    for (const event of events) {
      const key = event.event_id || `${event.timestamp}:${event.kind}:${event.role_id || ''}`;
      if (this.seen.has(key)) continue;
      this.seen.add(key);
      const line = this.lineFor(event);
      if (line) this.append({...line, time: event.timestamp});
    }
  }
}
