/**
 * activity-center.js — 全局后台任务中心
 *
 * 旧版把长任务的进度分散在各自页面（AI 评分、批量导出、上传……），切页后
 * 进度不可见。这里给它们一个统一的活动列表：标题、进度、状态、取消入口。
 *
 * 任务只是观测层：业务代码仍负责实际执行；只有后端确实提供取消命令时
 * （如评分/上传）才把 cancelable 设为 true。
 */
const Activity = {
  tasks: [],
  _listeners: [],
  _seq: 0,
  _bound: false,
  _timer: null,
  HISTORY_KEY: 'lp_activity_history_v1',
  HISTORY_MAX: 30,

  init() {
    if (this._bound) return;
    this._bound = true;
    this._loadHistory();
    const chip = document.getElementById('activity-chip');
    const panel = document.getElementById('activity-panel');
    chip?.addEventListener('click', () => this.toggle());
    document.getElementById('activity-close')?.addEventListener('click', () => this.close());
    document.getElementById('activity-clear')?.addEventListener('click', () => this.clearFinished());
    panel?.addEventListener('click', (e) => {
      if (e.target === panel) this.close();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (panel && !panel.classList.contains('hidden')) this.close();
    });
    this._timer = setInterval(() => {
      if (this.tasks.some(t => t.status === 'running')) this.render();
    }, 1000);
    this.render();
  },

  _loadHistory() {
    try {
      const raw = localStorage.getItem(this.HISTORY_KEY);
      if (!raw) return;
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return;
      this.tasks = arr
        .filter(x => x && x.id && x.status && x.status !== 'running')
        .slice(0, this.HISTORY_MAX)
        .map(x => ({
          id: String(x.id), title: String(x.title || ''), detail: String(x.detail || ''),
          current: Number(x.current) || 0, total: Number(x.total) || 0,
          status: x.status, startedAt: Number(x.startedAt) || Date.now(),
          endedAt: Number(x.endedAt) || Date.now(), cancelable: false, onCancel: null,
        }));
      for (const t of this.tasks) {
        const m = /^act-(\d+)$/.exec(t.id);
        if (m) this._seq = Math.max(this._seq, parseInt(m[1], 10) || 0);
      }
    } catch (_) { /* corrupt history must not block the app */ }
  },

  /** Persist completed tasks only; running tasks are process-bound and must not
   *  reappear as "running" after a reload. */
  _persist() {
    try {
      const finished = this.tasks
        .filter(t => t.status !== 'running')
        .slice(0, this.HISTORY_MAX)
        .map(({ onCancel, ...rest }) => rest);
      localStorage.setItem(this.HISTORY_KEY, JSON.stringify(finished));
    } catch (_) { /* ignore quota errors */ }
  },

  start(opts = {}) {
    const total = Math.max(0, Number(opts.total) || 0);
    const task = {
      id: opts.id || `act-${++this._seq}`,
      title: opts.title || '',
      detail: opts.detail || '',
      current: 0,
      total,
      status: 'running',
      startedAt: Date.now(),
      endedAt: 0,
      cancelable: !!opts.cancelable,
      onCancel: typeof opts.onCancel === 'function' ? opts.onCancel : null,
    };
    this.tasks.unshift(task);
    if (this.tasks.length > 50) this.tasks.length = 50;
    this.emit();
    return task.id;
  },

  update(id, patch = {}) {
    const task = this._find(id);
    if (!task || task.status !== 'running') return;
    if (patch.current != null) task.current = Math.max(0, Number(patch.current) || 0);
    if (patch.total != null) task.total = Math.max(0, Number(patch.total) || 0);
    if (patch.detail != null) task.detail = String(patch.detail);
    if (patch.title != null) task.title = String(patch.title);
    this.emit();
  },

  finish(id, opts = {}) {
    const task = this._find(id);
    if (!task || task.status !== 'running') return;
    task.status = 'done';
    task.endedAt = Date.now();
    if (opts.detail != null) task.detail = String(opts.detail);
    if (task.total) task.current = task.total;
    this._persist();
    this.emit();
  },

  fail(id, message) {
    const task = this._find(id);
    if (!task || task.status !== 'running') return;
    task.status = 'error';
    task.endedAt = Date.now();
    if (message) task.detail = String(message);
    this._persist();
    this.emit();
  },

  cancel(id) {
    const task = this._find(id);
    if (!task || task.status !== 'running') return;
    task.status = 'cancelled';
    task.endedAt = Date.now();
    try { if (task.onCancel) task.onCancel(); } catch (_) { /* task-level cancel must not throw */ }
    this._persist();
    this.emit();
  },

  /** Mark a task cancelled without invoking its cancel callback (backend already stopped). */
  markCancelled(id) {
    const task = this._find(id);
    if (!task || task.status !== 'running') return;
    task.status = 'cancelled';
    task.endedAt = Date.now();
    this._persist();
    this.emit();
  },

  /** 记录一个已经结束的事件型任务（例如一次保存失败）。 */
  note(opts = {}) {
    const id = this.start(opts);
    if (opts.status === 'error') this.fail(id, opts.detail);
    else this.finish(id, { detail: opts.detail });
    return id;
  },

  running() {
    return this.tasks.filter(t => t.status === 'running');
  },

  isBusy() {
    return this.tasks.some(t => t.status === 'running');
  },

  clearFinished() {
    this.tasks = this.tasks.filter(t => t.status === 'running');
    this._persist();
    this.emit();
  },

  toggle() {
    const panel = document.getElementById('activity-panel');
    if (!panel) return;
    if (panel.classList.contains('hidden')) this.open();
    else this.close();
  },

  open() {
    const panel = document.getElementById('activity-panel');
    if (!panel) return;
    panel.classList.remove('hidden');
    this.render();
  },

  close() {
    document.getElementById('activity-panel')?.classList.add('hidden');
  },

  _find(id) {
    return this.tasks.find(t => t.id === id);
  },

  onChange(fn) {
    if (typeof fn !== 'function') return () => {};
    this._listeners.push(fn);
    return () => {
      const i = this._listeners.indexOf(fn);
      if (i >= 0) this._listeners.splice(i, 1);
    };
  },

  emit() {
    this.render();
    const snap = { tasks: this.tasks.slice(), running: this.running().length };
    this._listeners.slice().forEach(fn => {
      try { fn(snap); } catch (_) { /* listener isolation */ }
    });
  },

  _elapsed(task) {
    const end = task.endedAt || Date.now();
    const sec = Math.max(0, Math.round((end - task.startedAt) / 1000));
    if (sec < 60) return `${sec}s`;
    return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  },

  render() {
    const chip = document.getElementById('activity-chip');
    const count = document.getElementById('activity-count');
    const list = document.getElementById('activity-list');
    const badge = document.getElementById('activity-running-badge');
    const running = this.running().length;
    if (chip) chip.classList.toggle('hidden', this.tasks.length === 0);
    if (chip) chip.classList.toggle('running', running > 0);
    if (count) {
      count.textContent = String(running);
      count.classList.toggle('hidden', running === 0);
      count.classList.toggle('running', running > 0);
    }
    if (badge) {
      badge.textContent = String(running);
      badge.classList.toggle('hidden', running === 0);
    }
    if (!list) return;
    if (!this.tasks.length) {
      list.innerHTML = `<div class="act-empty">${t('actEmpty')}</div>`;
      return;
    }
    const ICON = {
      running: 'ph-circle-notch',
      done: 'ph-check-circle',
      error: 'ph-warning-circle',
      cancelled: 'ph-prohibit',
    };
    list.innerHTML = this.tasks.map(task => {
      const pct = task.total ? Math.min(100, Math.round((task.current / task.total) * 100)) : 0;
      const progress = task.total
        ? `<div class="act-progress"><div class="act-progress-fill" style="width:${pct}%"></div></div>
           <span class="act-progress-text">${task.current} / ${task.total}</span>`
        : '';
      const cancel = (task.status === 'running' && task.cancelable)
        ? `<button class="btn btn-ghost btn-sm act-cancel" data-cancel="${this._esc(task.id)}">${t('actCancel')}</button>`
        : '';
      return `<div class="act-item ${task.status}">
        <i class="ph ${ICON[task.status] || 'ph-circle'} act-icon"></i>
        <div class="act-main">
          <div class="act-title-row">
            <span class="act-title">${this._esc(task.title)}</span>
            <span class="act-time">${this._elapsed(task)}</span>
          </div>
          ${task.detail ? `<div class="act-detail">${this._esc(task.detail)}</div>` : ''}
          ${progress}
        </div>
        ${cancel}
      </div>`;
    }).join('');
    list.querySelectorAll('[data-cancel]').forEach(btn => {
      btn.addEventListener('click', () => this.cancel(btn.dataset.cancel));
    });
  },

  _esc(s) {
    const d = document.createElement('div');
    d.textContent = String(s ?? '');
    return d.innerHTML;
  },
};
