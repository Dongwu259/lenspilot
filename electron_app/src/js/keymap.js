/**
 * keymap.js — 分层可重映射快捷键
 *
 * 三层优先级（从高到低）：
 *   1. 浮层/上下文层：幻灯片、Compare/Survey、编辑器
 *   2. 页面层：AI 选片、文件浏览、照片编辑页
 *   3. 全局层：命令面板、撤销/重做
 *
 * 同一个物理键允许出现在不同层；高优先级层会覆盖低层。例如编辑器里 C 是
 * 裁剪，照片编辑页里 C 可以是对比，而全局快捷键在页面层没有同名绑定时继续生效。
 * 绑定保存在 localStorage（本机偏好），同一层内冲突会被拒绝。
 */
const Keymap = {
  STORAGE: 'lp_keymap_v1',
  _map: {},
  _bound: false,
  _captureId: '',
  _listener: null,
  _activeLayer: 'global',

  LAYERS: [
    { id: 'global', i18n: 'kmLayerGlobal', descI18n: 'kmLayerGlobalDesc' },
    { id: 'photo-select', i18n: 'kmLayerPhotoSelect', descI18n: 'kmLayerPhotoSelectDesc' },
    { id: 'file-browser', i18n: 'kmLayerFileBrowser', descI18n: 'kmLayerFileBrowserDesc' },
    { id: 'photo-edit', i18n: 'kmLayerPhotoEdit', descI18n: 'kmLayerPhotoEditDesc' },
    { id: 'editor', i18n: 'kmLayerEditor', descI18n: 'kmLayerEditorDesc' },
    { id: 'lightbox', i18n: 'kmLayerLightbox', descI18n: 'kmLayerLightboxDesc' },
    { id: 'compare', i18n: 'kmLayerCompare', descI18n: 'kmLayerCompareDesc' },
    { id: 'slideshow', i18n: 'kmLayerSlideshow', descI18n: 'kmLayerSlideshowDesc' },
  ],

  ACTIONS: [
    // ── 全局 ──
    { id: 'commandPalette', layer: 'global', i18n: 'kmActionCommandPalette', def: ['Ctrl+K'] },
    { id: 'undo', layer: 'global', i18n: 'kmActionUndo', def: ['Ctrl+Z'] },
    { id: 'redo', layer: 'global', i18n: 'kmActionRedo', def: ['Ctrl+Shift+Z', 'Ctrl+Y'] },
    // ── AI 选片页 / 灯箱 ──
    { id: 'psKeepNext', layer: 'photo-select', i18n: 'kmActionPsKeep', def: ['P', 'K'] },
    { id: 'psRejectPrev', layer: 'photo-select', i18n: 'kmActionPsReject', def: ['X'] },
    { id: 'psClearMark', layer: 'photo-select', i18n: 'kmActionPsClear', def: ['U', '0'] },
    { id: 'psPrev', layer: 'photo-select', i18n: 'kmActionPsPrev', def: ['ArrowLeft'] },
    { id: 'psNext', layer: 'photo-select', i18n: 'kmActionPsNext', def: ['ArrowRight'] },
    { id: 'psClose', layer: 'photo-select', i18n: 'kmActionPsClose', def: ['Escape'] },
    // ── 文件浏览页 ──
    { id: 'fbSelectAll', layer: 'file-browser', i18n: 'kmActionFbSelectAll', def: ['Ctrl+A'] },
    { id: 'fbDelete', layer: 'file-browser', i18n: 'kmActionFbDelete', def: ['Delete'] },
    { id: 'fbClearSelection', layer: 'file-browser', i18n: 'kmActionFbClear', def: ['Escape'] },
    // ── 照片编辑页（网格） ──
    { id: 'pepSelectAll', layer: 'photo-edit', i18n: 'kmActionPepSelectAll', def: ['Ctrl+A'] },
    { id: 'pepOpenEditor', layer: 'photo-edit', i18n: 'kmActionPepOpen', def: ['Enter'] },
    { id: 'pepCompare', layer: 'photo-edit', i18n: 'kmActionPepCompare', def: ['C'] },
    { id: 'pepSurvey', layer: 'photo-edit', i18n: 'kmActionPepSurvey', def: ['V'] },
    // ── 编辑器 ──
    { id: 'toolEdit', layer: 'editor', i18n: 'kmActionToolEdit', def: ['E'] },
    { id: 'toolCrop', layer: 'editor', i18n: 'kmActionToolCrop', def: ['C'] },
    { id: 'toolMask', layer: 'editor', i18n: 'kmActionToolMask', def: ['M'] },
    { id: 'toolSpot', layer: 'editor', i18n: 'kmActionToolSpot', def: ['H'] },
    { id: 'toolHistory', layer: 'editor', i18n: 'kmActionToolHistory', def: ['Y'] },
    { id: 'toolVersion', layer: 'editor', i18n: 'kmActionToolVersion', def: ['V'] },
    { id: 'prev', layer: 'editor', i18n: 'kmActionPrev', def: ['ArrowLeft'] },
    { id: 'next', layer: 'editor', i18n: 'kmActionNext', def: ['ArrowRight'] },
    { id: 'original', layer: 'editor', i18n: 'kmActionOriginal', def: ['\\'] },
    { id: 'close', layer: 'editor', i18n: 'kmActionClose', def: ['Escape'] },
    // ── 共享大图灯箱 ──
    { id: 'lbPrev', layer: 'lightbox', i18n: 'kmActionLbPrev', def: ['ArrowLeft'] },
    { id: 'lbNext', layer: 'lightbox', i18n: 'kmActionLbNext', def: ['ArrowRight'] },
    { id: 'lbClose', layer: 'lightbox', i18n: 'kmActionLbClose', def: ['Escape'] },
    // ── Compare / Survey ──
    { id: 'pcMakeSelect', layer: 'compare', i18n: 'kmActionPcMakeSelect', def: ['S'] },
    { id: 'pcAutoAdvance', layer: 'compare', i18n: 'kmActionPcAutoAdvance', def: ['A'] },
    { id: 'pcPrev', layer: 'compare', i18n: 'kmActionPcPrev', def: ['ArrowLeft', 'ArrowUp'] },
    { id: 'pcNext', layer: 'compare', i18n: 'kmActionPcNext', def: ['ArrowRight', 'ArrowDown'] },
    { id: 'pcOpenEditor', layer: 'compare', i18n: 'kmActionPcOpen', def: ['Enter'] },
    { id: 'pcClose', layer: 'compare', i18n: 'kmActionPcClose', def: ['Escape'] },
    // ── 幻灯片 ──
    { id: 'ssPrev', layer: 'slideshow', i18n: 'kmActionSsPrev', def: ['ArrowLeft'] },
    { id: 'ssNext', layer: 'slideshow', i18n: 'kmActionSsNext', def: ['ArrowRight'] },
    { id: 'ssPlayPause', layer: 'slideshow', i18n: 'kmActionSsPlay', def: ['Space'] },
    { id: 'ssClose', layer: 'slideshow', i18n: 'kmActionSsClose', def: ['Escape'] },
  ],

  init() {
    this._load();
    if (this._bound) return;
    this._bound = true;
    this._bindSettings();
    this._buildSettings();
  },

  _defaults() {
    const out = {};
    this.ACTIONS.forEach(a => { out[a.id] = a.def.slice(); });
    return out;
  },

  _load() {
    this._map = this._defaults();
    try {
      const raw = localStorage.getItem(this.STORAGE);
      if (!raw) return;
      const saved = JSON.parse(raw);
      this.ACTIONS.forEach(a => {
        const v = saved && saved[a.id];
        if (Array.isArray(v) && v.length) {
          const clean = v.map(x => this._normalizeCombo(x)).filter(Boolean);
          if (clean.length) this._map[a.id] = clean;
        } else if (typeof v === 'string' && this._normalizeCombo(v)) {
          this._map[a.id] = [this._normalizeCombo(v)];
        }
      });
    } catch (_) { /* corrupt keymap falls back to defaults */ }
  },

  _save() {
    try { localStorage.setItem(this.STORAGE, JSON.stringify(this._map)); } catch (_) { /* ignore */ }
  },

  /** Overlay layers first, then the active page, then global. */
  activeLayers() {
    const open = (id) => {
      const el = document.getElementById(id);
      return !!el && !el.classList.contains('hidden');
    };
    const layers = [];
    if (open('slideshow-overlay')) layers.push('slideshow');
    if (open('photo-compare-overlay')) layers.push('compare');
    if (open('photo-edit-overlay')) layers.push('editor');
    if (open('photo-lightbox')) layers.push('lightbox');
    let page = '';
    try { page = (typeof App !== 'undefined' && App.currentPage) || ''; } catch (_) { /* ignore */ }
    if (page === 'photo-select') layers.push('photo-select');
    else if (page === 'file-browser') layers.push('file-browser');
    else if (page === 'photo-edit') layers.push('photo-edit');
    layers.push('global');
    return layers;
  },

  /** Resolve the winning action for an event, honoring layer precedence. */
  resolve(e) {
    const layers = this.activeLayers();
    for (const layer of layers) {
      for (const action of this.ACTIONS) {
        if (action.layer !== layer) continue;
        const combos = this._map[action.id] || [];
        if (combos.some(combo => this._eventMatches(combo, e))) return action.id;
      }
    }
    return '';
  },

  /** Layer-aware check used by every feature handler. */
  is(e, id) {
    const action = this.ACTIONS.find(a => a.id === id);
    if (!action) return false;
    if (!this.activeLayers().includes(action.layer)) return false;
    return this.resolve(e) === id;
  },

  /** Raw binding test (ignores layer precedence; prefer is()). */
  match(e, id) {
    const list = this._map[id];
    if (!list || !list.length) return false;
    return list.some(combo => this._eventMatches(combo, e));
  },

  get(id) {
    return (this._map[id] || []).slice();
  },

  getLabel(id) {
    const list = this.get(id);
    return list.length ? list.map(c => this._displayCombo(c)).join(' / ') : '';
  },

  _displayCombo(combo) {
    const MAP = {
      ArrowLeft: '←', ArrowRight: '→', ArrowUp: '↑', ArrowDown: '↓',
      Escape: 'Esc', Space: 'Space', Enter: 'Enter', Backspace: '⌫', Delete: 'Del',
    };
    return String(combo).replace(/(ArrowLeft|ArrowRight|ArrowUp|ArrowDown|Escape|Space|Enter|Backspace|Delete)$/,
      m => MAP[m] || m);
  },

  set(id, combos) {
    if (!this.ACTIONS.some(a => a.id === id)) return false;
    const list = (Array.isArray(combos) ? combos : [combos])
      .map(c => this._normalizeCombo(c)).filter(Boolean);
    if (!list.length) return false;
    this._map[id] = list;
    this._save();
    this._buildSettings();
    return true;
  },

  reset(id) {
    const action = this.ACTIONS.find(a => a.id === id);
    if (action) this._map[id] = action.def.slice();
    else this._map = this._defaults();
    this._save();
    this._buildSettings();
  },

  resetLayer(layer) {
    this.ACTIONS.filter(a => a.layer === layer).forEach(a => { this._map[a.id] = a.def.slice(); });
    this._save();
    this._buildSettings();
  },

  conflict(combo, exceptId) {
    const norm = this._normalizeCombo(combo);
    if (!norm) return null;
    const except = this.ACTIONS.find(a => a.id === exceptId);
    const layer = except ? except.layer : 'global';
    for (const action of this.ACTIONS) {
      if (action.id === exceptId || action.layer !== layer) continue;
      const hit = (this._map[action.id] || []).some(c => this._normalizeCombo(c) === norm);
      if (hit) return action;
    }
    return null;
  },

  /** True when the event is a modifier-only key press (not a valid binding). */
  isModifierKey(key) {
    return ['Control', 'Shift', 'Alt', 'Meta', 'CapsLock', 'Dead'].includes(key);
  },

  _eventMatches(combo, e) {
    const c = this._parse(combo);
    if (!c) return false;
    const mod = !!(e.ctrlKey || e.metaKey);
    if (c.ctrl !== mod) return false;
    if (c.alt !== !!e.altKey) return false;
    if (c.shift !== !!e.shiftKey) return false;
    return this._normalizeKey(e.key) === c.key;
  },

  _parse(combo) {
    if (!combo) return null;
    const parts = String(combo).split('+').map(s => s.trim());
    if (!parts.length) return null;
    const key = this._normalizeKey(parts[parts.length - 1]);
    if (!key) return null;
    const c = { ctrl: false, alt: false, shift: false, key };
    parts.slice(0, -1).forEach(p => {
      const k = p.toLowerCase();
      if (k === 'ctrl' || k === 'cmd' || k === 'meta') c.ctrl = true;
      else if (k === 'alt' || k === 'option') c.alt = true;
      else if (k === 'shift') c.shift = true;
    });
    return c;
  },

  _normalizeKey(key) {
    if (!key) return '';
    if (key === ' ') return 'Space';
    if (/^[a-z]$/i.test(key)) return key.toUpperCase();
    return String(key);
  },

  _normalizeCombo(combo) {
    if (!combo) return '';
    if (Array.isArray(combo)) return '';
    const parts = String(combo).split('+').map(s => s.trim()).filter(Boolean);
    if (!parts.length) return '';
    const key = this._normalizeKey(parts.pop());
    if (!key) return '';
    const flags = { ctrl: false, alt: false, shift: false };
    parts.forEach(p => {
      const k = p.toLowerCase();
      if (k === 'ctrl' || k === 'cmd' || k === 'meta') flags.ctrl = true;
      else if (k === 'alt' || k === 'option') flags.alt = true;
      else if (k === 'shift') flags.shift = true;
    });
    return this._format(flags, key);
  },

  _format(flags, key) {
    const out = [];
    if (flags.ctrl) out.push('Ctrl');
    if (flags.alt) out.push('Alt');
    if (flags.shift) out.push('Shift');
    out.push(key);
    return out.join('+');
  },

  fromEvent(e) {
    if (!e || this.isModifierKey(e.key)) return '';
    return this._format(
      { ctrl: !!(e.ctrlKey || e.metaKey), alt: !!e.altKey, shift: !!e.shiftKey },
      this._normalizeKey(e.key)
    );
  },

  _bindSettings() {
    // Capture phase: while recording, no other shortcut may fire.
    this._listener = (e) => {
      if (!this._captureId) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.key === 'Escape') { this._stopCapture(); return; }
      const combo = this.fromEvent(e);
      if (!combo) return;
      const conflict = this.conflict(combo, this._captureId);
      if (conflict) {
        const name = t(conflict.i18n) || conflict.id;
        if (typeof App !== 'undefined' && App.toast) App.toast(t('kmConflict', { name }), 'warning');
        return;
      }
      this._map[this._captureId] = [combo];
      this._save();
      const id = this._captureId;
      this._captureId = '';
      this._buildSettings();
      if (typeof App !== 'undefined' && App.toast) App.toast(t('kmSaved'), 'success');
      const btn = document.querySelector(`[data-keymap-edit="${id}"]`);
      if (btn) btn.focus();
    };
    window.addEventListener('keydown', this._listener, true);
  },

  _layerDef(id) {
    return this.LAYERS.find(l => l.id === id) || this.LAYERS[0];
  },

  _buildSettings() {
    const tabs = document.getElementById('keymap-layer-tabs');
    const host = document.getElementById('keymap-list');
    const layers = this.LAYERS.filter(l => this.ACTIONS.some(a => a.layer === l.id));
    if (!layers.some(l => l.id === this._activeLayer)) this._activeLayer = 'global';

    if (tabs) {
      tabs.innerHTML = layers.map(l => {
        const n = this.ACTIONS.filter(a => a.layer === l.id).length;
        return `<button class="keymap-layer-tab${l.id === this._activeLayer ? ' active' : ''}" data-keymap-layer="${l.id}">
          ${this._esc(t(l.i18n) || l.id)} <span class="keymap-layer-count">${n}</span>
        </button>`;
      }).join('');
      tabs.querySelectorAll('[data-keymap-layer]').forEach(btn => {
        btn.addEventListener('click', () => {
          this._activeLayer = btn.dataset.keymapLayer;
          this._buildSettings();
        });
      });
    }
    if (!host) return;
    const layer = this._layerDef(this._activeLayer);
    const actions = this.ACTIONS.filter(a => a.layer === this._activeLayer);
    host.innerHTML = `<div class="keymap-layer-desc">${t(layer.descI18n)}</div>` + actions.map(a => {
      const capturing = this._captureId === a.id;
      const keys = this.get(a.id).map(k => `<kbd>${this._esc(this._displayCombo(k))}</kbd>`).join('');
      return `<div class="setting-row keymap-row">
        <div><div class="setting-label">${this._esc(t(a.i18n) || a.id)}</div></div>
        <div class="keymap-actions">
          <span class="keymap-keys">${keys || `<kbd>${t('kmUnbound')}</kbd>`}</span>
          <button class="btn btn-outline btn-sm${capturing ? ' capturing' : ''}" data-keymap-edit="${a.id}">
            ${capturing ? t('kmCapture') : t('kmChange')}
          </button>
          <button class="btn btn-ghost btn-sm" data-keymap-reset="${a.id}" title="${t('kmResetOne')}"><i class="ph ph-arrow-counter-clockwise"></i></button>
        </div>
      </div>`;
    }).join('');
    host.querySelectorAll('[data-keymap-edit]').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.keymapEdit;
        if (this._captureId === id) { this._stopCapture(); return; }
        this._captureId = id;
        this._buildSettings();
      });
    });
    host.querySelectorAll('[data-keymap-reset]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.reset(btn.dataset.keymapReset);
        if (typeof App !== 'undefined' && App.toast) App.toast(t('kmResetDone'), 'success');
      });
    });
  },

  _stopCapture() {
    this._captureId = '';
    this._buildSettings();
  },

  _esc(s) {
    const d = document.createElement('div');
    d.textContent = String(s ?? '');
    return d.innerHTML;
  },
};
