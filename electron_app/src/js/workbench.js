/**
 * workbench.js — 跨页面照片工作台
 *
 * 解决旧版“五套选中”问题：
 *   - photos:    当前工作台照片集（跨 AI 选片 / 文件浏览 / 照片编辑 / 导出）
 *   - suggested: AI 建议入选的照片（只是建议，不是用户选择）
 *   - selection: 用户显式选择（唯一批量作用域）
 *   - active:    当前照片
 *
 * 所有页面共享同一个对象；页面切换不会丢上下文。
 * 不依赖任何框架，也不自动写 localStorage（避免大缩略图数据）。
 */

const Workbench = {
  photos: [],          // [{path, name, thumb, ext, source}]
  suggested: new Set(),// AI 建议（path）
  selection: new Set(),// 用户选择（path）——批量命令只能用它
  candidates: new Set(),// 用户候选（path）——评审元数据，不是批量作用域
  ratings: {},         // path -> 0..5 星（0 表示未评分）
  active: '',          // 当前照片 path
  source: '',          // 最近一次载入来源
  _listeners: [],
  _lastClicked: -1,
  _metaLoaded: false,
  _metaKey: 'lp_workbench_meta_v1',

  _norm(item) {
    if (!item) return null;
    if (typeof item === 'string') {
      const path = item;
      return { path, name: path.split(/[\\/]/).pop() || path, thumb: '', ext: (path.split('.').pop() || '').toLowerCase(), source: '' };
    }
    const path = item.path || item.file_path || '';
    if (!path) return null;
    return {
      path,
      name: item.name || item.filename || path.split(/[\\/]/).pop() || path,
      thumb: item.thumb || item.thumb_path || '',
      ext: (item.ext || path.split('.').pop() || '').toLowerCase(),
      source: item.source || '',
    };
  },

  _unique(list) {
    const seen = new Set();
    const out = [];
    (list || []).forEach(item => {
      const p = this._norm(item);
      if (!p || seen.has(p.path)) return;
      seen.add(p.path);
      out.push(p);
    });
    return out;
  },

  // 评分/候选是评审元数据，持久化到本机；工作台照片集本身不持久化。
  loadMeta() {
    if (this._metaLoaded) return;
    this._metaLoaded = true;
    try {
      const raw = localStorage.getItem(this._metaKey);
      if (!raw) return;
      const s = JSON.parse(raw);
      if (s && typeof s.ratings === 'object') {
        this.ratings = {};
        Object.entries(s.ratings).forEach(([p, v]) => {
          const n = Number(v);
          if (p && n >= 1 && n <= 5) this.ratings[p] = Math.round(n);
        });
      }
      if (s && Array.isArray(s.candidates)) this.candidates = new Set(s.candidates.filter(Boolean));
    } catch (_) { /* corrupt metadata must not break the workbench */ }
  },

  _saveMeta() {
    try {
      localStorage.setItem(this._metaKey, JSON.stringify({
        ratings: this.ratings,
        candidates: [...this.candidates],
      }));
    } catch (_) { /* ignore quota errors */ }
  },

  _pruneMeta(valid) {
    Object.keys(this.ratings).forEach(p => { if (!valid.has(p)) delete this.ratings[p]; });
    for (const p of [...this.candidates]) if (!valid.has(p)) this.candidates.delete(p);
  },

  setPhotos(photos, opts = {}) {
    this.loadMeta();
    const list = this._unique(photos);
    this.photos = list;
    if (opts.source) this.source = opts.source;
    const valid = new Set(list.map(p => p.path));
    for (const p of [...this.selection]) if (!valid.has(p)) this.selection.delete(p);
    for (const p of [...this.suggested]) if (!valid.has(p)) this.suggested.delete(p);
    if (opts.suggested) {
      this.suggested.clear();
      (opts.suggested || []).forEach(p => { if (valid.has(p)) this.suggested.add(p); });
    }
    if (opts.selection) {
      this.selection.clear();
      (opts.selection || []).forEach(p => { if (valid.has(p)) this.selection.add(p); });
    }
    const want = opts.active || this.active;
    this.active = valid.has(want) ? want : (list[0] ? list[0].path : '');
    this._lastClicked = -1;
    this.emit();
  },

  addPhotos(photos, opts = {}) {
    this.loadMeta();
    if (opts.source) this.source = opts.source;
    const merged = this._unique([...this.photos, ...(photos || [])]);
    const keepSelection = new Set(this.selection);
    const keepSuggested = new Set(this.suggested);
    this.photos = merged;
    this.selection = keepSelection;
    this.suggested = keepSuggested;
    if (opts.active) this.active = opts.active;
    if (opts.selectAdded) {
      this._unique(photos).forEach(p => this.selection.add(p.path));
    }
    if (!this.active && merged[0]) this.active = merged[0].path;
    this.emit();
  },

  removePhotos(paths) {
    const drop = new Set(paths || []);
    this.photos = this.photos.filter(p => !drop.has(p.path));
    drop.forEach(p => {
      this.selection.delete(p);
      this.suggested.delete(p);
      this.candidates.delete(p);
      delete this.ratings[p];
    });
    this._saveMeta();
    if (drop.has(this.active)) this.active = this.photos[0] ? this.photos[0].path : '';
    this.emit();
  },

  clear() {
    // Ratings/candidates are persistent review metadata; clearing the current
    // workbench must not erase them for photos loaded again later.
    this.photos = [];
    this.suggested.clear();
    this.selection.clear();
    this.active = '';
    this.source = '';
    this._lastClicked = -1;
    this.emit();
  },

  setSuggested(paths) {
    const valid = new Set(this.photos.map(p => p.path));
    this.suggested.clear();
    (paths || []).forEach(p => { if (valid.has(p)) this.suggested.add(p); });
    this.emit();
  },

  has(path) {
    return this.photos.some(p => p.path === path);
  },

  getPhotos() {
    return this.photos.slice();
  },

  getSelected() {
    return [...this.selection];
  },

  getSelectedPhotos() {
    return this.photos.filter(p => this.selection.has(p.path));
  },

  // ── 评审元数据：候选 / 星级。只影响评审视图，绝不改变 selection。 ──
  setRating(path, n) {
    if (!path) return;
    const v = Math.max(0, Math.min(5, Math.round(Number(n) || 0)));
    if (v) this.ratings[path] = v;
    else delete this.ratings[path];
    this._saveMeta();
    this.emit();
  },

  getRating(path) {
    return Number(this.ratings[path]) || 0;
  },

  toggleCandidate(path) {
    if (!path) return false;
    if (this.candidates.has(path)) this.candidates.delete(path);
    else this.candidates.add(path);
    this._saveMeta();
    this.emit();
    return this.candidates.has(path);
  },

  setCandidate(path, on) {
    if (!path) return;
    if (on) this.candidates.add(path);
    else this.candidates.delete(path);
    this._saveMeta();
    this.emit();
  },

  isCandidate(path) {
    return this.candidates.has(path);
  },

  getCandidates() {
    return this.photos.filter(p => this.candidates.has(p.path));
  },

  getActive() {
    return this.active;
  },

  isSelected(path) {
    return this.selection.has(path);
  },

  setActive(path) {
    if (this.has(path)) {
      this.active = path;
      this.emit();
    }
  },

  select(path, mode = 'replace') {
    if (!path) return;
    if (mode === 'toggle') {
      if (this.selection.has(path)) this.selection.delete(path);
      else this.selection.add(path);
    } else if (mode === 'add') {
      this.selection.add(path);
    } else {
      this.selection.clear();
      this.selection.add(path);
    }
    this.active = path;
    this._lastClicked = this.photos.findIndex(p => p.path === path);
    this.emit();
  },

  deselect(path) {
    this.selection.delete(path);
    this.emit();
  },

  setSelection(paths) {
    const valid = new Set(this.photos.map(p => p.path));
    this.selection.clear();
    (paths || []).forEach(p => { if (valid.has(p)) this.selection.add(p); });
    this.emit();
  },

  selectRange(toPath, add = true) {
    const to = this.photos.findIndex(p => p.path === toPath);
    if (to < 0) return;
    const from = this._lastClicked >= 0 ? this._lastClicked : to;
    const a = Math.min(from, to);
    const b = Math.max(from, to);
    if (!add) this.selection.clear();
    for (let i = a; i <= b; i++) this.selection.add(this.photos[i].path);
    this.active = toPath;
    this.emit();
  },

  selectAll(emit = true) {
    this.selection = new Set(this.photos.map(p => p.path));
    if (emit) this.emit();
  },

  clearSelection(emit = true) {
    this.selection.clear();
    if (emit) this.emit();
  },

  count() {
    return {
      photos: this.photos.length,
      selected: this.selection.size,
      suggested: this.suggested.size,
      candidates: this.candidates.size,
    };
  },

  snapshot() {
    return {
      photos: this.photos.slice(),
      suggested: [...this.suggested],
      selection: [...this.selection],
      candidates: [...this.candidates],
      ratings: { ...this.ratings },
      active: this.active,
      source: this.source,
    };
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
    const snap = this.snapshot();
    this._listeners.slice().forEach(fn => {
      try { fn(snap); } catch (_) { /* a listener must not break the workbench */ }
    });
    try {
      window.dispatchEvent(new CustomEvent('workbench:change', { detail: snap }));
    } catch (_) { /* ignore */ }
  },
};

// 便捷构造器：AI 选片结果 / 文件路径列表 → 工作台照片
Workbench.fromResults = function (results) {
  return (results || []).map(r => ({
    path: r.path,
    name: r.filename || (r.path || '').split(/[\\/]/).pop(),
    thumb: r.thumb_path || r.thumb || '',
    ext: ((r.path || '').split('.').pop() || '').toLowerCase(),
    source: 'ai-select',
  }));
};

Workbench.fromPaths = function (paths) {
  return (paths || []).map(p => (typeof p === 'string' ? { path: p } : p));
};
